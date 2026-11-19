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
        assert r.status_code == 200 and r.json()["paused"] is False and r.json()["open"] == []
        r = c.post("/api/live/pause", json={"paused": True}, headers={"X-Live-Password": "s3cret-pass"})
        assert r.status_code == 200
        assert c.get("/api/live", headers={"X-Live-Password": "s3cret-pass"}).json()["paused"] is True
        assert c.post("/api/live/pause", json={"paused": False}).status_code == 401
        for _ in range(10):
            c.get("/api/live", headers={"X-Live-Password": "wrong"})
        assert c.get("/api/live", headers={"X-Live-Password": "s3cret-pass"}).status_code == 429   # locked out
    live_api._fails.clear()
