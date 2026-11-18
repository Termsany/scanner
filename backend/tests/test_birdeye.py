"""Birdeye trader discovery against a mocked API (response shapes from data.birdeye.so docs)."""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.birdeye import BirdeyeClient, discover_traders, evaluate_summary, normalize_win_rate, wallet_label
from app.config import Settings
from app.models import (Badge, Base, BirdeyeCall, Chain, MarketSnapshot, Signal, SmartWallet, Token,
                        TraderCandidate, WalletSource)

NOW = datetime.now(timezone.utc)


def summary(win_rate, realized, trades):
    return {"unique_tokens": 12, "counts": {"total_buy": trades // 2, "total_sell": trades // 2, "total_trade": trades,
                                             "total_win": 7, "total_loss": 3, "win_rate": win_rate},
            "cashflow_usd": {}, "pnl": {"realized_profit_usd": realized, "total_usd": realized}}


SUMMARIES = {"WGOOD": summary(0.7, 12_000, 80), "G1": summary(45, 30_000, 120), "G2": summary(0.8, 9_000, 60)}


def handler(calls: list):
    def respond(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, req.headers.get("x-chain"), dict(req.url.params)))
        if req.headers.get("X-API-KEY") != "good-key":
            return httpx.Response(401, json={"success": False, "message": "Unauthorized. Authentication required."})
        if req.url.path == "/defi/v2/tokens/top_traders":
            items = [
                {"owner": "WGOOD", "tags": [], "totalPnl": 5_000, "realizedPnl": 4_000},
                {"owner": "WSNIPER", "tags": ["sniper"], "totalPnl": 50_000},
                {"owner": "WSMALL", "tags": [], "totalPnl": 50},
                {"owner": "11111111111111111111111111111111", "tags": [], "totalPnl": 9_000},
            ]
            return httpx.Response(200, json={"success": True, "data": {"items": items}})
        if req.url.path == "/trader/gainers-losers":
            items = [
                {"network": "solana", "address": "G1", "pnl": 20_000, "trade_count": 100, "volume": 1e5},
                {"network": "solana", "address": "G2", "pnl": 15_000, "trade_count": 40, "volume": 1e5},
                {"network": "solana", "address": "BOT", "pnl": 99_999, "trade_count": 9_000, "volume": 1e7},
            ]
            return httpx.Response(200, json={"success": True, "data": {"items": items}})
        if req.url.path == "/wallet/v2/pnl/summary":
            return httpx.Response(200, json={"success": True, "data": {"summary": SUMMARIES[req.url.params["wallet"]]}})
        return httpx.Response(404, json={"success": False, "message": "not found"})
    return respond


async def _seed_winner(s):
    t = Token(chain=Chain.solana, address="mintWIN", symbol="WIN", name="Win", price_usd=1.0, discovery_sources=[],
              alpha_score=82, badge=Badge.strong_alpha)
    s.add(t)
    await s.flush()
    at = NOW - timedelta(hours=8)
    s.add(Signal(token_id=t.id, score=82, badge=Badge.strong_alpha, price_usd=1.0, payload={}, created_at=at))
    s.add(MarketSnapshot(token_id=t.id, price_usd=1.9, captured_at=at + timedelta(hours=2)))
    await s.commit()


def run(settings: Settings, key: str = "good-key"):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/be.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(eng, expire_on_commit=False)
        calls: list = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler(calls)))
        client = BirdeyeClient(settings.model_copy(update={"birdeye_api_key": key}), http=http)
        client._last_call = 0.0
        try:
            async with Session() as s:
                await _seed_winner(s)
                stats = await discover_traders(s, client.settings, client=client)
                await s.commit()
                wallets = {w.address: w for w in (await s.execute(select(SmartWallet))).scalars()}
                cands = {c.address: c for c in (await s.execute(select(TraderCandidate))).scalars()}
                logged = (await s.execute(select(func.count(BirdeyeCall.id)))).scalar_one()
            return stats, wallets, cands, calls, logged
        finally:
            await http.aclose()
            await eng.dispose()

    return asyncio.run(go())


S = Settings(chains=["solana"], birdeye_daily_call_budget=30)


def test_full_pipeline_qualifies_verified_traders_only():
    stats, wallets, cands, calls, logged = run(S)
    assert set(wallets) == {"WGOOD", "G2"}
    w = wallets["WGOOD"]
    assert w.source == WalletSource.birdeye and w.enabled and w.label == "BE 70% · $12K 30d"
    assert w.stats["win_rate"] == 0.7 and w.win_tokens[0]["symbol"] == "WIN" and w.win_tokens[0]["pnl_usd"] == 5000
    # G1 reports win_rate as a percent (45) -> 0.45 < 0.55 -> rejected
    assert cands["G1"].qualified is False and "win rate 45%" in cands["G1"].reject_reason
    # sniper / dust / system address / bot-like seed never become candidates
    assert not {"WSNIPER", "WSMALL", "BOT", "11111111111111111111111111111111"} & set(cands)
    assert stats["winner_tokens"] == 1 and stats["added"] == 2 and stats["qualified"] == 2
    # 1 top_traders + 1 gainers + 3 summaries, every call logged, chain header sent
    assert logged == len(calls) == 5 and all(c[1] == "solana" for c in calls)
    tt = next(c for c in calls if c[0] == "/defi/v2/tokens/top_traders")
    assert tt[2]["sort_by"] == "total_pnl" and tt[2]["address"] == "mintWIN"


def test_budget_stops_discovery():
    stats, wallets, cands, calls, logged = run(S.model_copy(update={"birdeye_daily_call_budget": 2}))
    assert stats["stopped"].startswith("daily budget reached") and len(calls) == 2 and logged == 2
    assert set(wallets) == {"WGOOD"}  # top_traders + one verification fit; no seeding


def test_bad_key_is_reported_not_raised():
    stats, wallets, cands, calls, logged = run(S, key="wrong")
    assert "401" in stats["error"] and not wallets and logged == 1


def test_evaluate_summary_rules():
    ok, why, st = evaluate_summary(summary(0.6, 6_000, 30), S)
    assert ok and st["realized_usd_30d"] == 6000
    assert not evaluate_summary(summary(0.9, 6_000, 9_999), S)[0]       # bot-like
    assert not evaluate_summary(summary(0.9, 6_000, 5), S)[0]           # too few trades
    assert not evaluate_summary(summary(0.9, 1_000, 50), S)[0]          # too little profit
    assert normalize_win_rate(72) == 0.72 and normalize_win_rate(0.72) == 0.72
    assert wallet_label({"win_rate": 0.64, "realized_usd_30d": 1_250_000}) == "BE 64% · $1.2M 30d"
