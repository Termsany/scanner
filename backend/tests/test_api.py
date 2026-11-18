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
