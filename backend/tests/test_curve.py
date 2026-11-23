"""Paper pre-graduation trades: progress from the curve, entries at 70 / 85 %, exits, pruning, rows on /paper."""

import asyncio
import base64
import struct
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.curve import REAL_TOKENS_START, PaperCurve, parse_curve
from app.models import Base, PaperPosition

K = 30.0 * 1_073_000_000.0          # classic curve: virtual SOL x virtual tokens


def S(**kw):
    base = dict(curve_paper_enabled=True, curve_size_usd=30.0, curve_latency_s=2.0, curve_fee_pct=1.0,
                curve_slippage_pct=2.0, curve_grad_exit_extra_pct=3.0, curve_tp_pct=0.0, curve_sl_pct=-25.0,
                curve_max_hold_s=3600.0, curve_max_open=40, curve_max_watch=800, curve_prune1_s=120.0,
                curve_prune1_pct=10.0, curve_prune2_s=900.0, curve_prune2_pct=40.0, curve_max_watch_s=7200.0)
    base.update(kw)
    return Settings(**base)


def acct(progress: float, complete: bool = False) -> str:
    """A bonding-curve account at this share sold (constant product from the classic start)."""
    sold = REAL_TOKENS_START * progress / 100
    v_tok = 1_073_000_000.0 - sold
    v_sol = K / v_tok
    r_tok = REAL_TOKENS_START - sold
    raw = b"\0" * 8 + struct.pack("<5Q", int(v_tok * 1e6), int(v_sol * 1e9), int(r_tok * 1e6),
                                  int((v_sol - 30) * 1e9), int(1e15)) + bytes([complete])
    return base64.b64encode(raw).decode()


def price_at(progress: float) -> float:
    v_tok = 1_073_000_000.0 - REAL_TOKENS_START * progress / 100
    return K / v_tok / v_tok


def create(mint="M"):
    return {"txType": "create", "mint": mint, "symbol": "TST", "bondingCurveKey": "C" + mint}


def test_parse_curve_progress_and_price():
    st = parse_curve(acct(50))
    assert abs(st["progress"] - 50) < 0.01 and not st["complete"]
    assert abs(st["price"] / price_at(50) - 1) < 1e-6
    assert parse_curve(acct(100, complete=True))["complete"]
    assert parse_curve("not base64!") is None


def test_buy_at_70_and_85_then_sell_at_graduation():
    c = PaperCurve(S())
    assert c.on_create(create(), 0.0) == "M"
    c.on_curve("M", acct(40), 10.0)
    c.on_curve("M", acct(71), 20.0)
    assert "curve_70" in c.watch["M"].pending and not c.watch["M"].held    # waits for our delay
    c.on_curve("M", acct(74), 22.5)
    assert abs(c.watch["M"].held["curve_70"]["market"] - price_at(74)) / price_at(74) < 1e-6
    c.on_curve("M", acct(86), 30.0)
    c.tick(32.5)                                                           # no trade: fill on the tick
    assert set(c.watch["M"].held) == {"curve_70", "curve_85"}
    c.on_curve("M", acct(100, complete=True), 40.0)
    reasons = {p["strategy"]: p for p in c.closed}
    assert {p["reason"] for p in c.closed} == {"graduated"}
    assert reasons["curve_70"]["pnl"] > reasons["curve_85"]["pnl"] > 0      # earlier entry, bigger gain
    assert c.tick(41.0) == ["M"]                                            # nothing left to watch


def test_stop_loss_when_the_curve_turns():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 10.0)
    c.on_curve("M", acct(70), 12.5)
    assert "curve_70" in c.watch["M"].held
    c.on_curve("M", acct(55), 20.0)                    # sellers: price down ~ -32%
    assert c.closed and c.closed[0]["reason"] == "stop_loss" and c.closed[0]["pnl"] < 0


def test_graduation_before_our_fill_cancels_the_buy():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(99), 10.0)
    c.on_curve("M", acct(100, complete=True), 11.0)
    c.tick(13.0)
    assert not c.closed and not c.opened


def test_stalled_tokens_are_dropped():
    c = PaperCurve(S())
    c.on_create(create("A"), 0.0)
    c.on_create(create("B"), 0.0)
    c.on_curve("A", acct(3), 5.0)
    c.on_curve("B", acct(25), 5.0)
    assert c.tick(121.0) == ["A"]                      # under 10 % after 2 min
    assert c.tick(901.0) == ["B"]                      # under 40 % after 15 min


def test_watch_cap_and_mayhem_skip():
    c = PaperCurve(S(curve_max_watch=1))
    assert c.on_create(create("A"), 0.0) == "A"
    assert c.on_create(create("B"), 0.0) is None
    c2 = PaperCurve(S())
    assert c2.on_create({**create("X"), "is_mayhem_mode": True}, 0.0) is None


def test_rows_written_for_paper_page():
    async def run():
        with tempfile.TemporaryDirectory() as d:
            eng = create_async_engine(f"sqlite+aiosqlite:///{d}/t.db")
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(eng, expire_on_commit=False)
            c = PaperCurve(S(), sol_usd=200.0)
            c.on_create(create(), 0.0)
            c.on_curve("M", acct(85), 10.0)
            c.on_curve("M", acct(86), 12.5)
            async with Session() as s:
                await c.flush(s)
            c.on_curve("M", acct(100, complete=True), 20.0)
            async with Session() as s:
                await c.flush(s)
                rows = (await s.execute(select(PaperPosition))).scalars().all()
            await eng.dispose()
            return rows

    rows = asyncio.run(run())
    assert {r.strategy for r in rows} == {"curve_70", "curve_85"}
    assert all(r.status == "closed" and r.exit_reason == "graduated" and r.pnl_usd > 0 for r in rows)


def pp_trade(progress: float, mint="M", side="buy"):
    v_tok = 1_073_000_000.0 - REAL_TOKENS_START * progress / 100
    return {"txType": side, "mint": mint, "vSolInBondingCurve": K / v_tok, "vTokensInBondingCurve": v_tok}


HEL = "https://mainnet.helius-rpc.com/?api-key=x"


def test_feed_choice():
    assert PaperCurve(S(live_rpc_url=HEL, pumpportal_api_key="k")).feed == "hybrid"
    assert PaperCurve(S(live_rpc_url=HEL, pumpportal_api_key=None)).feed == "helius"
    assert PaperCurve(S(live_rpc_url=None, pumpportal_api_key="k")).feed == "pumpportal"
    assert PaperCurve(S(live_rpc_url=None, pumpportal_api_key=None)).feed is None
    assert PaperCurve(S(live_rpc_url=None, pumpportal_api_key="k", curve_feed="hybrid")).feed is None


def test_pumpportal_only_trades_and_migration():
    c = PaperCurve(S(live_rpc_url=None, pumpportal_api_key="k"))
    c.on_create(create(), 0.0)
    assert '"subscribeTokenTrade"' in c.pp_sync()[0]          # every new token is streamed
    c.on_pp(pp_trade(86), 10.0)
    assert abs(c.watch["M"].progress - 86) < 0.01
    c.on_pp(pp_trade(88), 12.5)
    assert set(c.watch["M"].held) == {"curve_70", "curve_85"}
    c.on_pp({"txType": "migrate", "mint": "M"}, 20.0)
    assert {p["reason"] for p in c.closed} == {"graduated"} and c.pp_events == 2
    c.tick(21.0)
    assert '"unsubscribeTokenTrade"' in c.pp_sync()[0]


def test_hybrid_pays_only_for_tokens_past_the_threshold():
    c = PaperCurve(S(live_rpc_url=HEL, pumpportal_api_key="k", curve_pp_from_pct=50.0))
    c.on_create(create("A"), 0.0)
    c.on_create(create("B"), 0.0)
    c.on_curve("A", acct(20), 5.0)
    c.on_curve("B", acct(55), 5.0)
    assert c.pp_sync() == ['{"method": "subscribeTokenTrade", "keys": ["B"]}']
    c.on_pp(pp_trade(30, mint="A"), 6.0)                        # not streamed: ignored, not counted
    assert c.watch["A"].progress < 25 and c.pp_events == 0
