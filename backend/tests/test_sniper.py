"""Paper sniper: simulated fills after our delay, exits, momentum rule, and rows on /paper."""

import asyncio
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base, PaperPosition
from app.sniper import PaperSniper, price_of


def S(**kw):
    base = dict(sniper_paper_enabled=True, sniper_size_usd=30.0, sniper_latency_s=2.0, sniper_fee_pct=1.0,
                sniper_slippage_pct=2.0, sniper_tp_pct=50.0, sniper_sl_pct=-30.0, sniper_max_hold_s=300.0,
                sniper_sample_every=1, sniper_max_dev_buy_sol=3.0, sniper_window_s=20.0, sniper_min_buyers=3,
                sniper_min_net_sol=2.0, sniper_max_open=60, sniper_momentum_enabled=True)
    base.update(kw)
    return Settings(**base)


def create(mint="M", sol=0.5, v_sol=30.0, v_tok=1_000_000_000.0):
    return {"txType": "create", "mint": mint, "symbol": "TST", "solAmount": sol,
            "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tok}


def trade(side, mint="M", sol=1.0, who="w1", v_sol=30.0, v_tok=1_000_000_000.0):
    return {"txType": side, "mint": mint, "solAmount": sol, "traderPublicKey": who,
            "vSolInBondingCurve": v_sol, "vTokensInBondingCurve": v_tok}


P0 = 30.0 / 1_000_000_000.0


def test_price_from_virtual_reserves():
    assert abs(price_of(create()) - P0) < 1e-18
    assert price_of({"vSolInBondingCurve": 0}) is None


def test_fill_waits_for_our_delay_and_take_profit():
    sn = PaperSniper(S(sniper_min_buyers=99))                   # only snipe_all
    assert sn.on_message(create(), 0.0) == ["M"]
    sn.on_message(trade("buy", v_sol=33.0), 1.0)                # 1 s: not filled yet (delay 2 s)
    assert not sn.watch["M"].held
    sn.on_message(trade("buy", v_sol=36.0), 2.5)                # first trade after 2 s: fill here
    pos = sn.watch["M"].held["snipe_all"]
    assert abs(pos["market"] - 36.0 / 1e9) < 1e-18 and "ahead 2" in pos["why"]
    sn.on_message(trade("buy", v_sol=54.1), 10.0)               # +50% over our market price: take profit
    assert not sn.watch["M"].held and sn.closed[0]["reason"] == "take_profit"
    c = sn.closed[0]
    exp = 30.0 / (36e-9 * 1.03) * (54.1e-9 * 0.97) - 30.0      # fee + slippage both ways
    assert abs(c["pnl"] - exp) < 1e-6 and c["pnl"] > 0


def test_stop_loss_and_max_hold():
    sn = PaperSniper(S(sniper_min_buyers=99))
    sn.on_message(create(), 0.0)
    sn.tick(2.0)                                                # no trade: fills at the last known price
    assert "snipe_all" in sn.watch["M"].held
    sn.on_message(trade("sell", v_sol=20.0), 5.0)               # -33%: stop loss
    assert sn.closed[-1]["reason"] == "stop_loss" and sn.closed[-1]["pnl"] < 0

    sn2 = PaperSniper(S(sniper_min_buyers=99))
    sn2.on_message(create(mint="X"), 0.0)
    sn2.tick(2.0)
    sn2.tick(303.0)                                             # 5 min, flat: time exit
    assert sn2.closed[-1]["reason"] == "time"


def test_momentum_needs_buyers_and_sol_in_the_window():
    sn = PaperSniper(S(sniper_sample_every=1000))              # only snipe_momentum
    sn.on_message(create(), 0.0)
    for i, t in enumerate((1.0, 2.0, 3.0)):
        sn.on_message(trade("buy", sol=1.0, who=f"b{i}", v_sol=30.0 + i), t)
    assert sn.watch["M"].pending.get("snipe_momentum")         # 3 buyers, 3 SOL in 3 s: decided
    sn.on_message(trade("buy", sol=0.1, who="b9", v_sol=40.0), 5.5)
    assert "snipe_momentum" in sn.watch["M"].held

    late = PaperSniper(S(sniper_sample_every=1000))
    late.on_message(create(mint="L"), 0.0)
    for i in range(3):
        late.on_message(trade("buy", mint="L", sol=1.0, who=f"b{i}"), 30.0 + i)   # after the 20 s window
    assert "snipe_momentum" not in late.watch["L"].decided


def test_creator_big_buy_is_skipped_and_idle_tokens_are_dropped():
    sn = PaperSniper(S(sniper_sample_every=1000))
    assert sn.on_message(create(sol=5.0), 0.0) == []           # creator bought 5 SOL: skip
    assert "M" not in sn.watch
    sn.on_message(create(mint="Q"), 0.0)
    assert sn.tick(30.0) == ["Q"] and "Q" not in sn.watch     # nothing happened: unsubscribe


def test_flush_writes_open_and_closed_rows_for_the_paper_page():
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/s.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as ses:
                sn = PaperSniper(S(sniper_min_buyers=99), sol_usd=100.0)
                sn.on_message(create(), 0.0)
                sn.tick(2.0)                                     # opened
                sn.on_message(trade("buy", v_sol=60.0), 3.0)     # +100%: closed in the same batch
                sn.on_message(create(mint="N"), 4.0)
                sn.tick(6.0)                                     # another one, still open
                await sn.flush(ses)
                return (await ses.execute(select(PaperPosition).order_by(PaperPosition.id))).scalars().all()
        finally:
            await eng.dispose()

    rows = asyncio.run(go())
    assert [(r.strategy, r.token_address, r.status) for r in rows] == [
        ("snipe_all", "M", "closed"), ("snipe_fast_tp", "M", "closed"),
        ("snipe_all", "N", "open"), ("snipe_fast_tp", "N", "open")]
    assert rows[0].exit_reason == "take_profit" and rows[0].pnl_usd > 0
    assert abs(rows[0].entry_price - 30e-9 * 100) < 1e-12                       # priced in USD


def _curve(v_tok, v_sol, r_sol, complete=False):
    import base64
    import struct
    raw = b"\x17" * 8 + struct.pack("<5Q", int(v_tok * 1e6), int(v_sol * 1e9), 0, int(r_sol * 1e9), 10**15)
    return base64.b64encode(raw + bytes([complete])).decode()


def test_curve_account_decodes_and_drives_trades():
    from app.sniper import curve_state
    st = curve_state(_curve(1_000_000_000, 30.0, 0.0))
    assert abs(st["vSolInBondingCurve"] - 30.0) < 1e-9 and st["complete"] is False
    assert curve_state("bad!") is None

    sn = PaperSniper(S(sniper_sample_every=1000, sniper_min_buyers=3, sniper_min_net_sol=2.0))
    sn.on_message({**create(), "bondingCurveKey": "CURVE"}, 0.0)
    assert sn.watch["M"].curve == "CURVE"
    sn.on_curve("M", _curve(1_000_000_000, 30.0, 0.0), 0.5)       # first reading: baseline only
    for i, t in enumerate((1.0, 2.0, 3.0)):
        sn.on_curve("M", _curve(1_000_000_000 - (i + 1) * 1e7, 31.0 + i, 1.0 * (i + 1)), t)
    w = sn.watch["M"]
    assert w.buys == 3 and abs(w.net_sol - 3.0) < 1e-6 and w.pending.get("snipe_momentum")
    sn.on_curve("M", _curve(980_000_000, 32.0, 2.5), 4.0)         # real SOL fell: a sell
    assert abs(w.net_sol - 2.5) < 1e-6


def test_mayhem_mode_tokens_are_skipped():
    sn = PaperSniper(S())
    assert sn.on_message({**create(), "is_mayhem_mode": True}, 0.0) == []
    assert PaperSniper(S(sniper_skip_mayhem=False)).on_message({**create(), "is_mayhem_mode": True}, 0.0) == ["M"]


def test_open_rows_show_live_pnl():
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/s.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as ses:
                sn = PaperSniper(S(sniper_min_buyers=99), sol_usd=100.0)
                sn.on_message(create(), 0.0)
                sn.tick(2.0)
                await sn.flush(ses)                               # opened at 30 vSol
            mk = async_sessionmaker(eng, expire_on_commit=False)
            async with mk() as ses:                               # the worker uses a new session per flush
                sn.on_message(trade("buy", v_sol=39.0), 3.0)      # +30%: still open
                await sn.flush(ses)
            async with mk() as ses:
                live = (await ses.execute(select(PaperPosition).where(PaperPosition.strategy == "snipe_all"))).scalars().one()
            async with mk() as ses:
                sn.tick(400.0)                                    # max hold: closed by a later flush
                await sn.flush(ses)
            async with mk() as ses:
                closed = (await ses.execute(select(PaperPosition).where(PaperPosition.strategy == "snipe_all"))).scalars().one()
            return live, closed
        finally:
            await eng.dispose()

    r, closed = asyncio.run(go())
    assert closed.status == "closed" and closed.exit_reason == "time"
    exp = 30.0 / (30e-9 * 1.03) * (39e-9 * 0.97) - 30.0
    assert r.status == "open" and abs(r.pnl_usd - exp) < 1e-6 and r.pnl_usd > 0
    assert abs(r.last_price - 39e-9 * 100) < 1e-12 and abs(r.peak_pct - 30.0) < 1e-6


def test_bad_curve_readings_are_ignored():
    from app.sniper import curve_state
    assert curve_state(_curve(1_000_000_000, 30.0, 0.0, complete=True)) is None      # migrated
    assert curve_state(_curve(500, 30.0, 0.0)) is None                               # nonsense reserves
    assert curve_state(_curve(1_000_000_000, 5.0, 0.0)) is None
    sn = PaperSniper(S(sniper_min_buyers=99))
    sn.on_message({**create(), "bondingCurveKey": "C"}, 0.0)
    sn.tick(2.0)                                                     # held at 30 vSol / 1.073B
    sn.on_curve("M", _curve(205_000_000, 128.0, 1.0), 3.0)           # in range but a 20x+ jump: ignored
    assert "snipe_all" in sn.watch["M"].held and not sn.closed


def test_fast_tp_takes_25_or_leaves_15_off_the_high_and_momentum_is_off_by_default():
    sn = PaperSniper(S(sniper_min_buyers=99))
    sn.on_message(create(), 0.0)
    sn.tick(2.0)
    assert {"snipe_all", "snipe_fast_tp"} <= set(sn.watch["M"].held)
    sn.on_message(trade("buy", v_sol=37.6), 3.0)              # +25.3%: fast one out, snipe_all stays
    assert [c["strategy"] for c in sn.closed] == ["snipe_fast_tp"] and sn.closed[0]["reason"] == "take_profit"

    tr = PaperSniper(S(sniper_min_buyers=99))
    tr.on_message(create(), 0.0)
    tr.tick(2.0)
    tr.on_message(trade("buy", v_sol=36.0), 3.0)              # +20% high
    tr.on_message(trade("sell", v_sol=30.5), 4.0)             # 15.3% off the high (+1.7%): out
    assert tr.closed[-1]["strategy"] == "snipe_fast_tp" and tr.closed[-1]["reason"] == "trailing_stop"
    assert "snipe_all" in tr.watch["M"].held

    off = PaperSniper(Settings(**{**S().model_dump(), "sniper_momentum_enabled": False, "sniper_sample_every": 1000}))
    off.on_message(create(), 0.0)
    for i in range(6):
        off.on_message(trade("buy", sol=1.0, who=f"b{i}", v_sol=31.0 + i), 1.0 + i)
    assert "snipe_momentum" not in off.watch["M"].decided
