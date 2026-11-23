"""API, WebSocket, AI fallback and Telegram command tests against a throwaway SQLite DB."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/test.db".replace("\\", "/")
os.environ["REDIS_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["WS_POLL_SECONDS"] = "0.2"
os.environ["WS_HEARTBEAT_SECONDS"] = "3600"
os.environ["SMART_ACTIVITY_CHECK_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Chain, ScannerRun  # noqa: E402
from app.scanner_worker import Scanner  # noqa: E402
from app.schemas import MarketMetrics, SecurityReport, SmartMoneyInfo  # noqa: E402
from app.scoring_engine import compute_alpha_score  # noqa: E402
from app.telegram_alerts import TelegramAlerts, fmt_usd, render_signal  # noqa: E402

SOL = "So1aNaTestMint1111111111111111111111111pump"
ETH = "0xAbCdEf0000000000000000000000000000000001"


def _metrics(chain: Chain, address: str, symbol: str, **kw) -> MarketMetrics:
    base = dict(chain=chain, address=address, symbol=symbol, name=f"{symbol} Token", pair_address=f"pair-{symbol}",
                liquidity_usd=100_000, volume_h1=80_000, volume_h24=900_000, buys_h1=300, sells_h1=150,
                price_change_m5=2.0, price_change_h1=12.0, price_change_h24=40.0, market_cap=1_000_000,
                price_usd=0.001, pair_created_at=datetime.now(timezone.utc) - timedelta(hours=3))
    base.update(kw)
    return MarketMetrics(**base)


def _safe(chain: Chain, address: str, **kw) -> SecurityReport:
    base = dict(chain=chain, address=address, providers=["test"], is_honeypot=False, is_mintable=False,
                is_freezable=False, lp_locked_pct=100, top10_holders_pct=18, buy_tax=0, sell_tax=0)
    base.update(kw)
    return SecurityReport(**base)


async def _ingest(m: MarketMetrics, sec: SecurityReport, sm: SmartMoneyInfo | None = None,
                  with_run: bool = False) -> int:
    """Same write path the worker uses: persist + maybe_signal."""
    scanner = Scanner(get_settings())
    try:
        sm = sm or SmartMoneyInfo()
        score = compute_alpha_score(m, sec, sm)
        async with SessionLocal() as s:
            token = await scanner.persist(s, m, sec, sm, score)
            await scanner.maybe_signal(s, token, m, score)
            if with_run:
                now = datetime.now(timezone.utc)
                s.add(ScannerRun(started_at=now, finished_at=now, scored=1, signals_emitted=1, ok=True))
            await s.commit()
        return score.total
    finally:
        await scanner.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        c.portal.call(_ingest, _metrics(Chain.solana, SOL, "ALPHA"), _safe(Chain.solana, SOL),
                      SmartMoneyInfo(tracked_wallet_hits=["Fund A"]))                       # 100 -> signal
        c.portal.call(_ingest, _metrics(Chain.ethereum, ETH.lower(), "MID", liquidity_usd=500_000),
                      _safe(Chain.ethereum, ETH.lower(), lp_locked_pct=10))                 # moderate
        c.portal.call(_ingest, _metrics(Chain.solana, "HoneyMint111", "TRAP"),
                      _safe(Chain.solana, "HoneyMint111", is_honeypot=True))                # 0
        yield c


def test_list_sorted_by_score(client):
    r = client.get("/api/tokens")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    scores = [t["alpha_score"] for t in body["items"]]
    assert scores == sorted(scores, reverse=True) and scores[0] == 100 and scores[-1] == 0
    top = body["items"][0]
    assert top["badge"] == "STRONG_ALPHA" and top["badges"]["lp_locked"] is True and top["badges"]["no_mint"] is True


def test_list_filters_and_pagination(client):
    assert client.get("/api/tokens", params={"chain": "ethereum"}).json()["total"] == 1
    assert client.get("/api/tokens", params={"min_score": 50}).json()["total"] == 2
    assert client.get("/api/tokens", params={"min_liquidity": 200_000}).json()["total"] == 1
    assert client.get("/api/tokens", params={"q": "trap"}).json()["items"][0]["symbol"] == "TRAP"
    page = client.get("/api/tokens", params={"page_size": 1, "page": 2}).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    assert client.get("/api/tokens", params={"sort": "liquidity"}).json()["items"][0]["symbol"] == "MID"
    assert client.get("/api/tokens", params={"min_score": 101}).status_code == 422


def test_detail_and_case_insensitive_evm(client):
    r = client.get(f"/api/tokens/ethereum/{ETH}")  # mixed-case input
    assert r.status_code == 200
    d = r.json()
    assert d["token"]["symbol"] == "MID"
    assert d["security"]["lp_locked_pct"] == 10
    assert d["score"]["security"]["components"][1]["passed"] is False
    assert len(d["score_history"]) == 1
    assert client.get("/api/tokens/solana/nope").status_code == 404
    assert client.get("/api/tokens/dogechain/x").status_code == 422


def test_honeypot_detail(client):
    d = client.get("/api/tokens/solana/HoneyMint111").json()
    assert d["score"]["hard_fail"] == "HONEYPOT" and d["token"]["alpha_score"] == 0


def test_ai_analyze_rules_fallback(client):
    r = client.post("/api/ai-analyze", json={"chain": "solana", "address": SOL})
    assert r.status_code == 200
    a = r.json()
    assert a["model"] == "rules-v1" and a["verdict"] == "BULLISH" and len(a["bullets"]) == 3
    trap = client.post("/api/ai-analyze", json={"chain": "solana", "address": "HoneyMint111"}).json()
    assert trap["verdict"] == "AVOID" and "honeypot" in trap["bullets"][0].lower()
    assert client.post("/api/ai-analyze", json={"chain": "base", "address": "0xdead"}).status_code == 404


def test_ai_analyze_inline_metrics(client):
    m = _metrics(Chain.base, "0x" + "1" * 40, "NEW").model_dump(mode="json")
    r = client.post("/api/ai-analyze", json={"chain": "base", "address": m["address"], "metrics": m})
    assert r.status_code == 200 and r.json()["verdict"] in {"AVOID", "NEUTRAL"}  # no security -> not bullish


def test_status_and_signals(client):
    st = client.get("/api/status").json()
    assert st["signals_24h"] == 1 and st["high_alpha_24h"] == 1 and st["live"] is False
    assert st["top_gainer"]["symbol"] == "ALPHA" and st["top_gainer"]["gain_pct"] == 0.0
    sigs = client.get("/api/signals").json()
    assert len(sigs) == 1 and sigs[0]["symbol"] == "ALPHA" and sigs[0]["payload"]["top_reasons"]


def test_chart_falls_back_to_snapshots(client):
    r = client.get(f"/api/tokens/solana/HoneyMint111/chart", params={"interval": "5m"})
    assert r.status_code == 200
    data = r.json()
    assert data["source"] in {"geckoterminal", "snapshots"}
    if data["source"] == "snapshots":
        assert len(data["candles"]) == 1 and data["candles"][0]["close"] == 0.001


def test_websocket_hello_filter_and_live_signal(client):
    with client.websocket_connect("/ws/live-signals") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["recent"][0]["symbol"] == "ALPHA"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "subscribe", "chains": ["base"], "min_score": 70})
        assert ws.receive_json()["filters"] == {"chains": ["base"], "min_score": 70}
        ws.send_json({"type": "subscribe", "chains": ["dogechain"]})
        assert ws.receive_json()["type"] == "error"

        addr = "0x" + "2" * 40
        client.portal.call(_ingest, _metrics(Chain.base, addr, "LIVE"), _safe(Chain.base, addr),
                           SmartMoneyInfo(tracked_wallet_hits=["Fund B"]))
        msg = ws.receive_json()
        assert msg["type"] == "signal" and msg["symbol"] == "LIVE" and msg["score"] == 100 and msg["signal_id"]


def test_cycle_signals_arrive_before_scan_complete(client):
    os.environ["WS_POLL_SECONDS"] = "0.2"
    with client.websocket_connect("/ws/live-signals") as ws:
        assert ws.receive_json()["type"] == "hello"
        addr = "0x" + "3" * 40
        client.portal.call(_ingest, _metrics(Chain.bsc, addr, "ORDER"), _safe(Chain.bsc, addr),
                           SmartMoneyInfo(tracked_wallet_hits=["Fund C"]), True)
        seen = []  # earlier fixture signals may still be in flight; only ORDER's position matters
        while "scan_complete" not in seen:
            msg = ws.receive_json()
            seen.append(msg.get("symbol") if msg["type"] == "signal" else msg["type"])
        assert "ORDER" in seen and seen.index("ORDER") < seen.index("scan_complete")


def test_signal_cooldown(client):
    before = len(client.get("/api/signals").json())
    client.portal.call(_ingest, _metrics(Chain.solana, SOL, "ALPHA"), _safe(Chain.solana, SOL),
                       SmartMoneyInfo(tracked_wallet_hits=["Fund A"]))
    assert len(client.get("/api/signals").json()) == before


def test_telegram_render_and_commands(client):
    bot = TelegramAlerts(get_settings().model_copy(update={"telegram_bot_token": "123:test"}))
    try:
        top = client.portal.call(bot.handle_command, "/top solana")
        assert "ALPHA" in top and "TRAP" in top
        card = client.portal.call(bot.handle_command, f"/token {ETH}")
        assert "MID" in card and "Alpha Score" in card
        assert "not found" in client.portal.call(bot.handle_command, "/token 0x" + "9" * 40)
        assert "Unknown chain" in client.portal.call(bot.handle_command, "/top doge")
        assert "Signals 24h" in client.portal.call(bot.handle_command, "/status")
    finally:
        client.portal.call(bot.close)
    sig = client.get("/api/signals").json()[0]["payload"]
    from app.schemas import LiveSignal

    text = render_signal(LiveSignal.model_validate(sig | {"name": "<b>x</b>"}), "https://dash.example")
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "/token/" in text
    assert fmt_usd(0.0000123) == "$0.00001230" and fmt_usd(1_500_000) == "$1.50M"


def test_smart_wallets_endpoint(client):
    body = client.get("/api/smart-wallets").json()
    assert body["items"] == [] and body["enabled_manual"] == 0 and body["enabled_auto"] == 0
    assert client.get("/api/smart-wallets", params={"limit": 0}).status_code == 422


def test_status_telemetry_and_volume_1h_sort(client):
    st = client.get("/api/status").json()
    assert st["security_coverage_pct"] == 100.0  # every seeded token has an audit
    assert "last_cycle_seconds" in st and "last_run_errors" in st
    items = client.get("/api/tokens", params={"sort": "volume_1h"}).json()["items"]
    vols = [t["volume_h1"] for t in items]
    assert vols == sorted(vols, reverse=True)


def test_telegram_track_commands_owner_only(client):
    owner = "4242"
    bot = TelegramAlerts(get_settings().model_copy(update={"telegram_bot_token": "123:test", "telegram_chat_id": owner}))
    wallet = "8vVrVudDd2DvL5jUGFZWDSh95VjhLpeETRAp1kmFnb61"
    try:
        assert "owner's chat" in client.portal.call(bot.handle_command, f"/track solana {wallet}", "999")
        assert "not a valid Solana" in client.portal.call(bot.handle_command, "/track solana 0xabc", owner)
        reply = client.portal.call(bot.handle_command, f"/track solana {wallet} Whale One", owner)
        assert "Tracking" in reply and "Whale One" in reply
        listing = client.portal.call(bot.handle_command, "/tracked", owner)
        assert "Whale One" in listing and "watch-only" in listing and "\n" in listing
        assert "/track" in client.portal.call(bot.handle_command, "/help", owner)
        wallets = client.get("/api/smart-wallets").json()["items"]
        assert any(w["address"] == wallet and w["track_buys"] for w in wallets)
        assert "off" in client.portal.call(bot.handle_command, f"/untrack solana {wallet}", owner)
        assert "No tracked wallets" in client.portal.call(bot.handle_command, "/tracked", owner)
    finally:
        client.portal.call(bot.close)


def test_render_trade_escapes_and_links():
    from app.models import Chain, WalletTrade
    from app.telegram_alerts import render_trade

    t = WalletTrade(chain=Chain.solana, wallet="8vVrVudDd2DvL5jUGFZWDSh95VjhLpeETRAp1kmFnb61", wallet_label="<Whale>",
                    token_address="Mint1", symbol="GEM", name="Gem & Co", tx_hash="sig9", amount=1000,
                    usd_value=500, price_usd=0.5, liquidity_usd=80_000, market_cap=900_000, alpha_score=78,
                    first_buy=True, pair_url="https://dexscreener.com/x")
    text = render_trade(t, "https://scanner.example")
    assert "&lt;Whale&gt;" in text and "Gem &amp; Co" in text and "first buy" in text
    assert "solscan.io/tx/sig9" in text and "/token/solana/Mint1" in text and "Alpha Score <b>78</b>" in text


def test_track_autodetect_deeplink_and_wallet_buttons(client):
    owner = "4242"
    bot = TelegramAlerts(get_settings().model_copy(update={"telegram_bot_token": "123:test", "telegram_chat_id": owner}))
    sol = "6DQAGJT7VZPVBsuG4kn3AvpyHCEi7B2RFFvMZdbqQqqP"
    evm = "0xb5002f252e8153edf930d13f7979fedbbb086401"
    calls = []

    async def fake_call(method, payload, timeout=None):
        calls.append((method, payload))
        return {"ok": True}

    bot._call = fake_call
    try:
        # chain auto-detected from the address; name after it
        r = client.portal.call(bot.handle_command, f"/track {sol} Sniper King", owner)
        assert "on solana" in r and "Sniper King" in r
        # dashboard deep link: /start track_evm_<addr> -> Ethereum + Base + BSC
        r = client.portal.call(bot.handle_command, f"/start track_evm_{evm}", owner)
        assert "ethereum, base, bsc" in r
        assert "owner's chat" in client.portal.call(bot.handle_command, f"/start track_sol_{sol}", "999")

        text, markup = client.portal.call(bot.wallets_menu)
        buttons = [b[0] for b in markup["inline_keyboard"]]
        sol_btn = next(b for b in buttons if "Sniper King" in b["text"])
        assert sol_btn["text"].startswith("✖ Untrack") and sol_btn["callback_data"].startswith("u:")
        # tap Untrack from the owner chat -> tracking off; a watch-only wallet then leaves the menu,
        # while the tracked EVM wallets stay listed with Untrack buttons
        client.portal.call(bot.handle_callback, {"id": "cb1", "data": sol_btn["callback_data"],
                                                 "message": {"chat": {"id": int(owner)}, "message_id": 7}})
        assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Stopped tracking Sniper King"}) in calls
        edited = next(p for m, p in calls if m == "editMessageText")
        labels = [row[0]["text"] for row in edited["reply_markup"]["inline_keyboard"]]
        assert not any("Sniper King" in t for t in labels) and sum(t.startswith("✖ Untrack") for t in labels) == 3
        # someone else tapping a button is refused
        client.portal.call(bot.handle_callback, {"id": "cb2", "data": "t:1", "message": {"chat": {"id": 999}}})
        assert ("answerCallbackQuery", {"callback_query_id": "cb2", "text": "Not allowed"}) in calls
    finally:
        client.portal.call(bot.handle_command, f"/untrack evm {evm}", owner)
        client.portal.call(bot.close)


def test_untrack_from_tracked_menu_and_deeplink(client):
    owner = "4242"
    bot = TelegramAlerts(get_settings().model_copy(update={"telegram_bot_token": "123:test", "telegram_chat_id": owner}))
    sol = "8vVrVudDd2DvL5jUGFZWDSh95VjhLpeETRAp1kmFnb61"
    evm = "0xb5002f252e8153edf930d13f7979fedbbb086401"
    calls = []

    async def fake_call(method, payload, timeout=None):
        calls.append((method, payload))
        return {"ok": True}

    bot._call = fake_call
    try:
        client.portal.call(bot.handle_command, f"/track {sol} Sol Whale", owner)
        client.portal.call(bot.handle_command, f"/track {evm} Evm Whale", owner)
        text, markup = client.portal.call(bot.tracked_menu)
        rows = [r[0] for r in markup["inline_keyboard"]]
        assert len(rows) == 2 and "(base, bsc, ethereum" in text     # EVM address grouped into one button
        evm_btn = next(b for b in rows if "Evm Whale" in b["text"])
        assert evm_btn["text"].startswith("✖ Untrack") and evm_btn["callback_data"].startswith("x:")
        # one tap untracks the EVM address on all three chains and re-renders the tracked list
        client.portal.call(bot.handle_callback, {"id": "c1", "data": evm_btn["callback_data"],
                                                 "message": {"chat": {"id": int(owner)}, "message_id": 3}})
        edited = next(p for m, p in calls if m == "editMessageText")
        assert "Evm Whale" not in edited["text"] and "Sol Whale" in edited["text"]
        # dashboard deep link: /start untrack_sol_<addr>
        r = client.portal.call(bot.handle_command, f"/start untrack_sol_{sol}", owner)
        assert "Buy alerts off" in r
        assert "No tracked wallets" in client.portal.call(bot.tracked_menu)[0]
        assert "owner's chat" in client.portal.call(bot.handle_command, f"/start untrack_sol_{sol}", "999")
    finally:
        client.portal.call(bot.close)


def test_performance_endpoint_and_telegram_text(client):
    body = client.get("/api/performance", params={"hours": 24}).json()
    assert {"overall", "by_score", "by_chain", "recent", "measured", "signals_in_window"} <= set(body)
    bot = TelegramAlerts(get_settings().model_copy(update={"telegram_bot_token": "123:test", "telegram_chat_id": "1"}))
    try:
        text = client.portal.call(bot.handle_command, "/performance 7d", "1")
        assert "Signal performance · last 7d" in text
    finally:
        client.portal.call(bot.close)


def test_signal_alerts_off_sends_only_wallet_alerts(client):
    from sqlalchemy import select as _select

    from app.models import Chain, Signal, WalletTrade

    async def seed():
        async with SessionLocal() as s:
            s.add(WalletTrade(chain=Chain.solana, wallet="W", wallet_label="Whale", token_address="Mint9",
                              symbol="GEM", tx_hash="tx-only-wallet", amount=10, usd_value=500, side="buy",
                              kind="buy", telegram_sent=False))
            await s.commit()
            return (await s.execute(_select(Signal).where(Signal.telegram_sent.is_(False)))).scalars().all()

    unsent_signals = client.portal.call(seed)
    assert unsent_signals  # fixture signals are still pending
    bot = TelegramAlerts(get_settings().model_copy(update={
        "telegram_bot_token": "123:test", "telegram_chat_id": "1", "telegram_signal_alerts": False,
        "telegram_trade_group_seconds": 0}))
    sent = []

    async def fake_send(chat, text, markup=None):
        sent.append(text)
        return True

    bot.send = fake_send
    try:
        client.portal.call(bot.deliver_pending)
        assert sent and all("WALLET" in t for t in sent) and not any("ALPHA" in t and "/100" in t for t in sent)
    finally:
        client.portal.call(bot.close)



def test_wallet_trades_grouped_into_one_alert(client):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from app.models import Chain, WalletTrade

    old = _dt.now(_tz.utc) - _td(seconds=90)
    bt = _dt(2026, 11, 18, 23, 12, 16, tzinfo=_tz.utc)

    async def seed():
        async with SessionLocal() as s:
            for i, w in enumerate(("WalletA", "WalletB", "WalletC")):
                s.add(WalletTrade(chain=Chain.solana, wallet=w, wallet_label=f"auto {i}", token_address="GrpMint",
                                  symbol="DCTY", name="", tx_hash=f"grp-{i}", amount=100, usd_value=380.0 + i,
                                  price_usd=0.01, liquidity_usd=50_000, side="buy", kind="buy", first_buy=True,
                                  block_time=bt, detected_at=old, telegram_sent=False))
            s.add(WalletTrade(chain=Chain.solana, wallet="WalletD", wallet_label="fresh", token_address="NewMint",
                              symbol="NEW", name="", tx_hash="fresh-1", amount=1, usd_value=200, side="buy",
                              kind="buy", detected_at=_dt.now(_tz.utc), telegram_sent=False))
            await s.commit()

    client.portal.call(seed)
    bot = TelegramAlerts(get_settings().model_copy(update={
        "telegram_bot_token": "123:test", "telegram_chat_id": "1", "telegram_trade_group_seconds": 45}))
    sent = []

    async def fake_send(chat, text, markup=None):
        sent.append(text)
        return True

    bot.send = fake_send
    try:
        client.portal.call(bot.deliver_trades, "1")
        grouped = [t for t in sent if "DCTY" in t]
        assert len(grouped) == 1 and "3 TRACKED WALLETS BOUGHT" in grouped[0] and "$1.1K" in grouped[0]
        assert "Same second" in grouped[0] and grouped[0].count("first buy") == 3
        assert not any("NEW" in t for t in sent)          # 0s old: still waiting for other wallets
    finally:
        client.portal.call(bot.close)


def test_live_dashboard_requires_password(monkeypatch):
    from app.api import live as live_api

    s = get_settings()
    live_api._fails.clear()
    with TestClient(app) as c:
        monkeypatch.setattr(s, "live_dashboard_password", None)
        assert c.get("/api/live").status_code == 404           # page disabled until a password is set
        monkeypatch.setattr(s, "live_dashboard_password", "s3cret-pass")
        assert c.get("/api/live").status_code == 401
        assert c.get("/api/live", headers={"X-Live-Password": "nope"}).status_code == 401
        r = c.get("/api/live", headers={"X-Live-Password": "s3cret-pass"})
        assert r.status_code == 200 and r.json()["paused"] is False and r.json()["accounts"][0]["open"] == []
        r = c.post("/api/live/pause", json={"paused": True}, headers={"X-Live-Password": "s3cret-pass"})
        assert r.status_code == 200
        assert c.get("/api/live", headers={"X-Live-Password": "s3cret-pass"}).json()["paused"] is True
        assert c.post("/api/live/pause", json={"paused": False}).status_code == 401
        for _ in range(10):
            c.get("/api/live", headers={"X-Live-Password": "wrong"})
        assert c.get("/api/live", headers={"X-Live-Password": "s3cret-pass"}).status_code == 429   # locked out
    live_api._fails.clear()


def test_live_manual_sell_endpoint(monkeypatch):
    from app.api import live as live_api
    from app.models import LivePosition

    s = get_settings()
    live_api._fails.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "pw-123")
    monkeypatch.setattr(s, "live_mode", "dry")

    async def mk():
        async with SessionLocal() as ses:
            p = LivePosition(mode="dry", token_address="T1", symbol="T1", leader_wallet="L", leader_trade_id=1,
                             status="open", cost_usd=20.0, tokens_raw="100")
            ses.add(p)
            await ses.commit()
            return p.id

    with TestClient(app) as c:
        pid = c.portal.call(mk)
        h = {"X-Live-Password": "pw-123"}
        assert c.post(f"/api/live/positions/{pid}/sell", json={"share": 1.0}).status_code == 401
        r = c.post(f"/api/live/positions/{pid}/sell", json={"share": 1.0}, headers=h)
        assert r.status_code == 200 and r.json()["queued"]
        assert c.post(f"/api/live/positions/{pid}/sell", json={"share": 0.5}, headers=h).status_code == 409
        opened = c.get("/api/live", headers=h).json()["accounts"][0]["open"]
        assert any(p["id"] == pid and p["manual_sell_frac"] == 1.0 for p in opened)
    live_api._fails.clear()


def test_live_values_open_positions_at_live_prices(monkeypatch):
    from app.api import live as live_api
    from app.live import SOL_MINT
    from app.models import LivePosition, TrackerCursor

    s = get_settings()
    live_api._fails.clear()
    live_api._cache.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "pw-9")
    monkeypatch.setattr(s, "live_mode", "dry")

    async def prices(http, mints):
        return {"VAL1": {"usd": 0.002, "decimals": 6}, SOL_MINT: {"usd": 100.0, "decimals": 9}}

    async def balances(http, pubkeys):
        return {pk: 1.5 for pk in pubkeys}

    monkeypatch.setattr(live_api, "jupiter_prices", prices)
    monkeypatch.setattr(live_api, "sol_balances", balances)

    async def mk():
        async with SessionLocal() as ses:
            # 20,000 tokens left (6 decimals) at $0.002 = $40 now; $10 already sold; cost $20 -> +$30
            ses.add(LivePosition(mode="dry", token_address="VAL1", symbol="VAL", leader_wallet="L",
                                 leader_trade_id=1, status="open", cost_usd=20.0, proceeds_usd=10.0,
                                 tokens_raw=str(20_000 * 10**6)))
            row = await ses.get(TrackerCursor, "live:pubkey")
            if row is None:
                ses.add(TrackerCursor(key="live:pubkey", value="PK1"))
            await ses.commit()

    with TestClient(app) as c:
        c.portal.call(mk)
        d = c.get("/api/live", headers={"X-Live-Password": "pw-9"}).json()
    main = d["accounts"][0]
    p = next(x for x in main["open"] if x["token_address"] == "VAL1")
    assert abs(p["value_usd"] - 40.0) < 1e-6 and abs(p["unrealized_usd"] - 30.0) < 1e-6
    assert abs(p["unrealized_pct"] - 150.0) < 1e-6
    assert d["sol_usd"] == 100.0 and main["sol_balance"] == 1.5 and main["sol_balance_usd"] == 150.0
    assert "leader_activity" in d and "updated_at" in d
    live_api._fails.clear()
    live_api._cache.clear()


def test_accounts_page_create_edit_and_guards(monkeypatch):
    from cryptography.fernet import Fernet

    from app.api import live as live_api

    s = get_settings()
    live_api._fails.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "s3cret-pass")
    h = {"X-Live-Password": "s3cret-pass"}
    leader = "BCyNgSB1njYWrjHubBc56UXqDqzc4ixk8UHPhGbTQ5aQ"
    new = {"name": "Alpha", "size_usd": 25, "max_open_usd": 200, "max_daily_loss_usd": 80,
           "take_capital_at_pct": 100, "leaders": [{"wallet": leader, "unconditional": True}]}
    with TestClient(app) as c:
        assert c.get("/api/live/accounts").status_code == 401
        r = c.get("/api/live/accounts", headers=h)
        assert r.status_code == 200 and r.json()["can_store_keys"] is False
        assert any(a["name"] == "main" for a in r.json()["accounts"])
        monkeypatch.setattr(s, "live_key_encryption_key", None)
        assert c.post("/api/live/accounts", json=new, headers=h).status_code == 400       # nowhere to keep keys
        monkeypatch.setattr(s, "live_key_encryption_key", Fernet.generate_key().decode())
        r = c.post("/api/live/accounts", json=new, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["account"]["name"] == "alpha" and body["secret"] and body["account"]["pubkey"]
        assert "key_enc" not in body["account"]
        listed = [a for a in c.get("/api/live/accounts", headers=h).json()["accounts"] if a["name"] == "alpha"][0]
        assert body["secret"] not in str(listed)
        assert c.post("/api/live/accounts", json=new, headers=h).status_code == 400       # duplicate name
        bad = {**new, "name": "beta", "max_open_usd": 10}
        assert c.post("/api/live/accounts", json=bad, headers=h).status_code == 400       # budget < one entry
        bad = {**new, "name": "beta", "leaders": [{"wallet": "nope"}]}
        assert c.post("/api/live/accounts", json=bad, headers=h).status_code == 400
        r = c.patch("/api/live/accounts/alpha", headers=h, json={
            "paused": True, "size_usd": 30, "take_capital_off": True,
            "leaders": [{"wallet": leader, "size_usd": 40}]})
        assert r.status_code == 200, r.text
        a = r.json()["account"]
        assert a["paused"] and a["size_usd"] == 30 and a["take_capital_at_pct"] is None
        assert a["leaders"][0]["size_usd"] == 40 and a["leaders"][0]["unconditional"] is False
        assert c.patch("/api/live/accounts/alpha", headers=h, json={"enabled": False}).json()["account"]["enabled"] is False
        assert c.patch("/api/live/accounts/nope", headers=h, json={}).status_code == 404
    live_api._fails.clear()


def test_leader_toggle_endpoint_and_close_positions(monkeypatch):
    from cryptography.fernet import Fernet

    from app.api import live as live_api
    from app.models import LivePosition

    s = get_settings()
    live_api._fails.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "s3cret-pass")
    monkeypatch.setattr(s, "live_key_encryption_key", Fernet.generate_key().decode())
    h = {"X-Live-Password": "s3cret-pass"}
    leader = "BCyNgSB1njYWrjHubBc56UXqDqzc4ixk8UHPhGbTQ5aQ"
    with TestClient(app) as c:
        r = c.post("/api/live/accounts", headers=h, json={"name": "togl", "size_usd": 10, "max_open_usd": 100,
                                                          "max_daily_loss_usd": 50, "leaders": [{"wallet": leader}]})
        assert r.status_code == 200, r.text
        assert r.json()["account"]["leaders"][0]["enabled"] is True

        async def add():
            async with SessionLocal() as ses:
                for mint in ("TG1", "TG2"):
                    ses.add(LivePosition(mode=s.live_mode, account="togl", token_address=mint, symbol=mint,
                                         leader_wallet=leader, leader_trade_id=1, status="open", cost_usd=10))
                await ses.commit()
        c.portal.call(add)
        acct = [a for a in c.get("/api/live/accounts", headers=h).json()["accounts"] if a["name"] == "togl"][0]
        assert acct["open_by_leader"] == {leader: 2}

        assert c.post("/api/live/accounts/togl/leaders/toggle", json={"wallet": leader, "enabled": False}).status_code == 401
        assert c.post("/api/live/accounts/togl/leaders/toggle", headers=h,
                      json={"wallet": "Nope1111111111111111111111111111111111111", "enabled": False}).status_code == 404
        r = c.post("/api/live/accounts/togl/leaders/toggle", headers=h, json={"wallet": leader, "enabled": False})
        assert r.status_code == 200 and r.json()["closing"] == 0
        assert r.json()["account"]["leaders"][0]["enabled"] is False
        r = c.post("/api/live/accounts/togl/leaders/toggle", headers=h,
                   json={"wallet": leader, "enabled": False, "close_positions": True})
        assert r.json()["closing"] == 2

        async def fracs():
            from sqlalchemy import select
            async with SessionLocal() as ses:
                return [p.manual_sell_frac for p in (await ses.execute(
                    select(LivePosition).where(LivePosition.account == "togl"))).scalars().all()]
        assert c.portal.call(fracs) == [1.0, 1.0]
        r = c.post("/api/live/accounts/togl/leaders/toggle", headers=h, json={"wallet": leader, "enabled": True})
        assert r.json()["account"]["leaders"][0]["enabled"] is True
    live_api._fails.clear()


def test_leader_max_chase_is_saved_and_checked(monkeypatch):
    from cryptography.fernet import Fernet

    from app.api import live as live_api

    s = get_settings()
    live_api._fails.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "s3cret-pass")
    monkeypatch.setattr(s, "live_key_encryption_key", Fernet.generate_key().decode())
    h = {"X-Live-Password": "s3cret-pass"}
    leader = "BCyNgSB1njYWrjHubBc56UXqDqzc4ixk8UHPhGbTQ5aQ"
    body = {"name": "chs", "size_usd": 10, "max_open_usd": 100, "max_daily_loss_usd": 50,
            "leaders": [{"wallet": leader, "max_chase_pct": -5}]}
    with TestClient(app) as c:
        assert c.post("/api/live/accounts", headers=h, json=body).status_code == 400
        body["leaders"][0]["max_chase_pct"] = 30
        r = c.post("/api/live/accounts", headers=h, json=body)
        assert r.status_code == 200 and r.json()["account"]["leaders"][0]["max_chase_pct"] == 30
        r = c.patch("/api/live/accounts/chs", headers=h, json={"leaders": [{"wallet": leader}]})
        assert r.json()["account"]["leaders"][0]["max_chase_pct"] is None        # empty = account default
        # saving the account keeps a wallet that was switched off, off
        c.post("/api/live/accounts/chs/leaders/toggle", headers=h, json={"wallet": leader, "enabled": False})
        saved = c.get("/api/live/accounts", headers=h).json()["accounts"]
        leaders = [a for a in saved if a["name"] == "chs"][0]["leaders"]
        r = c.patch("/api/live/accounts/chs", headers=h, json={"size_usd": 12, "leaders": leaders})
        assert r.json()["account"]["leaders"][0]["enabled"] is False
    live_api._fails.clear()


def test_price_orders_place_list_and_cancel(monkeypatch):
    from app.api import live as live_api

    s = get_settings()
    live_api._fails.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "pw-ord")
    monkeypatch.setattr(s, "live_mode", "dry")
    mint = "HuN5y4fJU7LNkaPjV9LhxkY7LUwpynW7yhL4qoVhpump"

    async def meta(http, mints):
        return {mint: {"symbol": "HUN", "price": 1.3}}

    monkeypatch.setattr(live_api, "token_meta", meta)
    h = {"X-Live-Password": "pw-ord"}
    body = {"account": "main", "mint": mint, "condition": "below", "trigger_price": 1.0, "amount_usd": 20,
            "target_price": 2.0, "target_share": 0.5, "stop_price": 0.8, "expires_hours": 24}
    with TestClient(app) as c:
        assert c.post("/api/live/orders", json=body).status_code == 401
        r = c.post("/api/live/orders", json=body, headers=h)
        assert r.status_code == 200, r.text
        o = r.json()["order"]
        assert (o["symbol"], o["status"], o["max_over_pct"], o["then_target_share"]) == ("HUN", "open", 5.0, 0.5)
        bad = c.post("/api/live/orders", json={**body, "trigger_price": 1.5}, headers=h)       # price already below
        assert bad.status_code == 400 and "already" in bad.json()["detail"]
        assert c.post("/api/live/orders", json={**body, "account": "nope"}, headers=h).status_code == 404
        assert [x["id"] for x in c.get("/api/live/orders", headers=h).json()["orders"]] == [o["id"]]
        assert c.post(f"/api/live/orders/{o['id']}/cancel", headers=h).status_code == 200
        assert c.post(f"/api/live/orders/{o['id']}/cancel", headers=h).status_code == 409
        assert c.get("/api/live/orders", headers=h).json()["orders"] == []
        assert c.get("/api/live/orders?all=true", headers=h).json()["orders"][0]["status"] == "cancelled"
    live_api._fails.clear()


def test_scorecard_lists_copied_wallets_and_scores_an_address(monkeypatch):
    from cryptography.fernet import Fernet

    from app import wallet_scout
    from app.api import live as live_api
    from app.models import WalletScore

    s = get_settings()
    live_api._fails.clear()
    monkeypatch.setattr(s, "live_dashboard_password", "s3cret-pass")
    monkeypatch.setattr(s, "live_key_encryption_key", Fernet.generate_key().decode())
    h = {"X-Live-Password": "s3cret-pass"}
    leader = "C7dbXbgrRiLkhvtQDCJkiLj1NShARZGrQ21Rz2DwtqW1"
    other = "GCZuKXmLZ3QgXsA7AE3fCtMLBUG6L78QUUrAaGpL5ibk"

    async def fake_score(session, wallet, settings=None, scout=None):
        row = await session.get(WalletScore, wallet) or WalletScore(address=wallet)
        row.metrics, row.checks, row.passed, row.reason = {"closed": 20, "t_stat": 2.1}, [
            {"key": "t_stat", "ok": True, "label": "t-stat 2.10"}], True, "20 trades"
        session.add(row)
        await session.commit()
        return row

    monkeypatch.setattr(wallet_scout, "score_wallet", fake_score)
    with TestClient(app) as c:
        r = c.post("/api/live/accounts", headers=h, json={"name": "scorec", "size_usd": 10, "max_open_usd": 100,
                                                          "max_daily_loss_usd": 50,
                                                          "leaders": [{"wallet": leader, "label": "speedy"}]})
        assert r.status_code == 200, r.text
        assert c.get("/api/live/scorecard").status_code == 401
        rows = {w["address"]: w for w in c.get("/api/live/scorecard", headers=h).json()["wallets"]}
        assert rows[leader]["label"] == "speedy" and rows[leader]["accounts"][0]["account"] == "scorec"
        assert c.post("/api/live/scorecard", headers=h, json={"wallet": "not-an-address"}).status_code == 400
        r = c.post("/api/live/scorecard", headers=h, json={"wallet": other})
        assert r.status_code == 200 and r.json()["passed"] is True and r.json()["checks"][0]["key"] == "t_stat"
        rows = {w["address"]: w for w in c.get("/api/live/scorecard", headers=h).json()["wallets"]}
        assert rows[other]["score"]["metrics"]["t_stat"] == 2.1 and rows[other]["accounts"] == []
