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
    c.on_curve("M", acct(15), 10.0)                    # (below the early research levels: they stay out)
    c.on_curve("M", acct(71), 20.0)
    assert "curve_70" in c.watch["M"].pending and "curve_70" not in c.watch["M"].held   # waits for our delay
    c.on_curve("M", acct(74), 22.5)
    assert abs(c.watch["M"].held["curve_70"]["market"] - price_at(74)) / price_at(74) < 1e-6
    c.on_curve("M", acct(86), 30.0)
    c.tick(32.5)                                                           # no trade: fill on the tick
    assert set(c.watch["M"].held) == {"curve_70", "curve_85", "curve_70_half", "curve_70_trail",
                                      "curve_70_filter", "curve_85_half40", "curve_70_live"}
    c.on_curve("M", acct(100, complete=True), 40.0)
    reasons = {p["strategy"]: p for p in c.closed}
    assert {p["reason"] for p in c.closed if p["strategy"] != "curve_70_live"} == {"graduated"}
    assert reasons["curve_70_live"]["pnl"] > 0                              # the real-money copy of curve_70
    assert reasons["curve_70"]["pnl"] > reasons["curve_85"]["pnl"] > 0      # earlier entry, bigger gain
    assert set(c.watch["M"].held) == {"curve_70_trail"}                     # the trail rides on after graduation
    assert c.tick(41.0) == []


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
    c.on_curve("B", acct(25), 35.0)                    # too slow for curve_fast19 (5 SOL within 30 s)
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
    assert {r.strategy for r in rows} == {"curve_70", "curve_85", "curve_70_half", "curve_70_trail",
                                          "curve_70_filter", "curve_85_half40"}    # first seen at 85%: no 30% buy
    done = [r for r in rows if r.strategy != "curve_70_trail"]
    assert all(r.status == "closed" and r.exit_reason == "graduated" and r.pnl_usd > 0 for r in done)
    trail = next(r for r in rows if r.strategy == "curve_70_trail")
    assert trail.status == "open" and trail.size_usd == 10.0 and trail.sl_pct == 0.0   # rides after graduation


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
    assert set(c.watch["M"].held) == {"curve_70", "curve_85", "curve_70_half", "curve_70_trail",
                                      "curve_70_filter", "curve_85_half40"}
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


# ---- user's rule: $10, no stop loss, half at 2x, then a stop at the entry --------------------------------
def _two_x_progress(entry: float) -> float:
    p = entry
    while price_at(p) < 2 * price_at(entry):
        p += 0.1
    return p + 0.2


def _filled_at_70():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 10.0)
    c.on_curve("M", acct(70), 12.5)
    assert {"curve_70_half", "curve_70_trail"} <= set(c.watch["M"].held)
    return c


def test_half_and_trail_have_no_stop_loss_and_are_10_dollars():
    c = _filled_at_70()
    assert c.watch["M"].held["curve_70_half"]["size"] == 10.0
    c.on_curve("M", acct(45), 20.0)                              # price far down: curve_70 stops out at -25%
    held = set(c.watch["M"].held)
    assert "curve_70" not in held and {"curve_70_half", "curve_70_trail"} <= held


def test_half_sold_at_2x_then_the_rest_stops_at_the_entry():
    c = _filled_at_70()
    c.on_curve("M", acct(_two_x_progress(70)), 20.0)            # 2x: half out, capital back
    pos = c.watch["M"].held["curve_70_half"]
    assert pos["half"] and pos["rem"] == 0.5 and pos["proceeds"] > 9.0
    c.on_curve("M", acct(69), 30.0)                              # back under the entry: the rest out
    half = next(p for p in c.closed if p["strategy"] == "curve_70_half")
    assert half["reason"] == "breakeven_stop" and half["pnl"] > 3.0          # ~+40% on $10 after costs


def test_trail_rides_after_graduation_and_sells_30_percent_off_the_high():
    c = _filled_at_70()
    c.on_curve("M", acct(100, complete=True), 20.0)             # graduated: half sold on the way, the rest rides
    pos = c.watch["M"].held["curve_70_trail"]
    grad = c.watch["M"].price
    assert pos["half"] and pos["gpeak"] == grad and c.riding() == ["M"]
    c.on_riding_price("M", grad * 1.5, 30.0)                    # keeps running on PumpSwap
    assert "curve_70_trail" in c.watch["M"].held
    c.on_riding_price("M", grad * 1.5 * 0.69, 40.0)             # 31% off the high
    trail = next(p for p in c.closed if p["strategy"] == "curve_70_trail")
    assert trail["reason"] == "trailing_stop" and trail["pnl"] > 0
    assert c.tick(41.0) == ["M"]                                 # now nothing left on it


def test_trail_time_limit_after_graduation():
    c = PaperCurve(S(curve_trail_max_hold_s=100.0))
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 10.0)
    c.on_curve("M", acct(70), 12.5)
    c.on_curve("M", acct(100, complete=True), 20.0)
    c.on_riding_price("M", c.watch["M"].price, 120.0)           # 107 s after the fill
    assert next(p for p in c.closed if p["strategy"] == "curve_70_trail")["reason"] == "time"


def test_flush_survives_tokens_added_and_closed_while_it_waits_on_the_database():
    """2026-11-23: a new token arriving while flush awaited the database raised "dictionary changed size during
    iteration"; the feed loop then closed every open position as feed_lost."""
    async def run():
        with tempfile.TemporaryDirectory() as d:
            eng = create_async_engine(f"sqlite+aiosqlite:///{d}/t.db")
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(eng, expire_on_commit=False)
            c = PaperCurve(S(), sol_usd=200.0)
            c.on_create(create("A"), 0.0)
            c.on_curve("A", acct(85), 10.0)
            c.on_curve("A", acct(86), 12.5)               # bought
            async with Session() as s:
                await c.flush(s)
            async with Session() as s:
                real_get = s.get

                async def get(*a, **kw):                  # the feed runs while flush waits: new token, a close
                    c.on_create(create(f"N{len(c.watch)}"), 13.0)
                    c.on_curve("A", acct(100, complete=True), 14.0)
                    return await real_get(*a, **kw)

                s.get = get
                await c.flush(s)                          # must not raise
            async with Session() as s:
                await c.flush(s)                          # the close that landed mid-flush is written now
                rows = (await s.execute(select(PaperPosition))).scalars().all()
            await eng.dispose()
            return rows

    rows = asyncio.run(run())
    grads = [r for r in rows if r.strategy in ("curve_70", "curve_85", "curve_70_half")]
    assert grads and all(r.status == "closed" and r.exit_reason == "graduated" for r in grads)


def _up_progress(entry: float, pct: float) -> float:
    p = entry
    while price_at(p) < price_at(entry) * (1 + pct / 100):
        p += 0.1
    return p + 0.2


def test_filter_skips_a_buy_whose_price_fell_10_percent_before_the_fill():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 10.0)                              # decision at 70%
    c.on_curve("M", acct(64), 12.5)                              # sold into: price well under the decision
    held = set(c.watch["M"].held)
    assert "curve_70_filter" not in held and "curve_70_half" in held


def test_filter_sells_half_at_plus_20_and_half40_at_plus_40():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 10.0)
    c.on_curve("M", acct(70), 12.5)                              # curve_70_filter in at 70%
    c.on_curve("M", acct(_up_progress(70, 20)), 20.0)
    f = c.watch["M"].held["curve_70_filter"]
    assert f["half"] and f["rem"] == 0.5 and f["size"] == 10.0
    assert not c.watch["M"].held["curve_70_half"]["half"]        # the 2x rule has not sold yet
    c.on_curve("M", acct(85), 30.0)
    c.on_curve("M", acct(85), 32.5)                              # curve_85_half40 in at 85%
    c.on_curve("M", acct(_up_progress(85, 40)), 40.0)
    assert c.watch["M"].held["curve_85_half40"]["half"]


def test_30_to_70_buys_at_30_and_sells_everything_at_70():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(30), 10.0)                              # decision at 30%
    c.on_curve("M", acct(31), 12.5)                              # filled
    held = c.watch["M"].held
    assert {"curve_30_70", "curve_30_70_sl"} <= set(held) and held["curve_30_70"]["size"] == 10.0
    assert "curve_70" not in held
    c.on_curve("M", acct(71), 20.0)                              # past 70%: everything out
    out = {p["strategy"]: p for p in c.closed}
    assert out["curve_30_70"]["reason"] == "curve_target" and out["curve_30_70"]["pnl"] > 10.0   # ~2.5x on $10


def test_30_to_70_stop_variant_stops_at_minus_30_the_other_holds():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(30), 10.0)
    c.on_curve("M", acct(30), 12.5)
    c.on_curve("M", acct(5), 20.0)                               # price -35% from the entry
    reasons = {p["strategy"]: p["reason"] for p in c.closed}
    assert reasons.get("curve_30_70_sl") == "stop_loss" and "curve_30_70" in c.watch["M"].held


def test_each_strategy_has_its_own_open_limit():
    c = PaperCurve(S(curve_max_open=1))
    for m in ("A", "B"):
        c.on_create(create(m), 0.0)
        c.on_curve(m, acct(30), 10.0)
        c.on_curve(m, acct(31), 12.5)
    assert c._open_count("curve_30_70") == 1                    # the second token waits for a free slot
    c.on_curve("A", acct(70), 20.0)
    c.on_curve("A", acct(71), 22.5)
    assert "curve_70" in c.watch["A"].held                       # 30% buys did not take the 70% slots


def test_30_to_70_skips_a_token_first_seen_well_past_30():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(45), 10.0)                              # first reading already 45%
    c.on_curve("M", acct(46), 12.5)
    assert "curve_30_70" not in c.watch["M"].held and not c.watch["M"].pending.get("curve_30_70")


def _custom(**kw):
    from app.curve import CurveRule

    base = dict(level=30.0, mode="custom", size=10.0, max_hold_s=3600.0)
    base.update(kw)
    return CurveRule(**base)


def test_custom_rule_buys_at_its_level_and_sells_at_its_curve_exit():
    c = PaperCurve(S())
    c.set_custom({"cc1": _custom(exit_at=60.0)})
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(30), 10.0)
    c.on_curve("M", acct(31), 12.5)
    assert c.watch["M"].held["cc1"]["size"] == 10.0
    c.on_curve("M", acct(61), 20.0)
    out = {p["strategy"]: p for p in c.closed}
    assert out["cc1"]["reason"] == "curve_target" and out["cc1"]["pnl"] > 0


def test_custom_rule_take_profit_half_and_stop():
    c = PaperCurve(S())
    c.set_custom({"cc1": _custom(tp=20.0), "cc2": _custom(half=20.0), "cc3": _custom(sl=-30.0)})
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(30), 10.0)
    c.on_curve("M", acct(30), 12.5)
    c.on_curve("M", acct(_up_progress(30, 20)), 20.0)            # +20%: cc1 all out, cc2 half out
    reasons = {p["strategy"]: p["reason"] for p in c.closed}
    assert reasons.get("cc1") == "take_profit" and c.watch["M"].held["cc2"]["half"]
    c.on_curve("M", acct(30), 30.0)                              # back to the entry: cc2's rest out
    c.on_curve("M", acct(5), 40.0)                               # -35%: cc3 stops out
    reasons = {p["strategy"]: p["reason"] for p in c.closed}
    assert reasons.get("cc2") == "breakeven_stop" and reasons.get("cc3") == "stop_loss"


def test_custom_rule_switched_off_buys_nothing_and_a_deleted_one_finishes_its_trades():
    c = PaperCurve(S())
    c.set_custom({"cc1": _custom(exit_at=60.0), "cc2": _custom(exit_at=60.0, enabled=False)})
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(30), 10.0)
    c.on_curve("M", acct(31), 12.5)
    assert "cc1" in c.watch["M"].held and "cc2" not in c.watch["M"].held
    c.set_custom({})                                             # cc1 deleted while its trade is open
    assert "cc1" in c.rules and not c.rules["cc1"].enabled
    c.on_curve("M", acct(61), 20.0)
    assert any(p["strategy"] == "cc1" and p["reason"] == "curve_target" for p in c.closed)


def test_entry_snapshot_has_speed_dev_and_recent_buying():
    c = PaperCurve(S())
    c.on_create({**create(), "traderPublicKey": "Dev1", "initialBuy": 79_310_000, "uri": "https://x/meta.json"}, 0.0)
    for i, p in enumerate((40, 50, 60, 66, 70)):
        c.on_curve("M", acct(p), 2.0 + i * 5)                    # five buys in the last minute
    c.on_curve("M", acct(70), 25.0)
    pos = c.watch["M"].held["curve_70"]
    f = pos["features"]
    assert f["dev_pct"] == 10.0 and f["uri"] == "https://x/meta.json" and f["dev_sold"] is False
    assert f["buys_60s"] >= 4 and f["sells_60s"] == 0 and f["sol_in_60s"] > 0
    assert f["secs_to_level"] == 22.0 and f["fill_level"] == 70.0 and f["progress_60s"] > 20


def test_speed_and_backslide_filters():
    c = PaperCurve(S())
    c.set_custom({"cc1": _custom(level=70.0, exit_at=90.0, max_secs=60.0),
                  "cc2": _custom(level=70.0, exit_at=90.0, skip_backslide=True),
                  "cc3": _custom(level=70.0, exit_at=90.0)})
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 100.0)                             # 100 s to 70%: slower than cc1 allows
    c.on_curve("M", acct(68), 102.5)                             # slid back before the buy
    held = set(c.watch["M"].held)
    assert "cc1" not in held and "cc2" not in held and "cc3" in held


def test_custom_rule_rides_after_graduation_on_a_trailing_stop():
    c = PaperCurve(S())
    c.set_custom({"cc1": _custom(level=70.0, grad_trail=30.0), "cc2": _custom(level=70.0)})
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(70), 10.0)
    c.on_curve("M", acct(70), 12.5)
    c.on_curve("M", acct(100, complete=True), 20.0)              # graduation: cc2 sells, cc1 rides
    reasons = {p["strategy"]: p["reason"] for p in c.closed}
    assert reasons.get("cc2") == "graduated" and "cc1" in c.watch["M"].held
    assert c.riding() == ["M"]
    grad = c.watch["M"].price
    c.on_riding_price("M", grad * 1.5, 30.0)                     # new high
    assert "cc1" in c.watch["M"].held
    c.on_riding_price("M", grad * 1.5 * 0.65, 40.0)              # 35% off the high: out
    out = next(p for p in c.closed if p["strategy"] == "cc1")
    assert out["reason"] == "trailing_stop" and out["pnl"] > 0


def test_a_short_feed_drop_keeps_open_trades_and_watches_them_again():
    c = PaperCurve(S())
    c.on_create(create("A"), 0.0)
    c.on_create(create("B"), 0.0)
    c.on_curve("A", acct(70), 10.0)
    c.on_curve("A", acct(70), 12.5)                              # bought A; B is only watched
    c.on_feed_lost(20.0)
    assert "A" in c.watch and "B" not in c.watch and not c.closed
    assert c.after_reconnect(30.0) == ["A"]                      # back in 10 s: watch A again
    c.on_curve("A", acct(100, complete=True), 40.0)
    assert {p["reason"] for p in c.closed} >= {"graduated"}      # the trade finished normally


def test_a_long_feed_outage_closes_open_trades_as_feed_lost():
    c = PaperCurve(S())
    c.on_create(create("A"), 0.0)
    c.on_curve("A", acct(70), 10.0)
    c.on_curve("A", acct(70), 12.5)
    c.on_feed_lost(20.0)
    assert c.after_reconnect(20.0 + PaperCurve.FEED_GRACE_S + 1) == []
    assert c.closed and {p["reason"] for p in c.closed} == {"feed_lost"} and not c.watch


def test_research_rules_fast_early_curves_and_late_fast_curve():
    c = PaperCurve(S())
    c.on_create(create(), 0.0)
    c.on_curve("M", acct(20), 20.0)                              # 5 SOL in within 30 s
    c.on_curve("M", acct(35), 50.0)                              # 10 SOL in within 60 s
    c.on_curve("M", acct(36), 53.0)
    held = c.watch["M"].held
    assert {"curve_fast19", "curve_fast34"} <= set(held) and held["curve_fast19"]["size"] == 10.0
    c.on_curve("M", acct(93), 150.0)                             # 93% within 4 min
    c.on_curve("M", acct(93.5), 153.0)
    assert "curve_93_fast" in c.watch["M"].held
    c.on_curve("M", acct(97), 160.0)                             # early ones sell at 96.6%, before graduation
    out = {p["strategy"]: p for p in c.closed}
    assert out["curve_fast19"]["reason"] == "curve_target" and out["curve_fast19"]["pnl"] > 30
    assert "curve_93_fast" in c.watch["M"].held
    c.on_curve("M", acct(99.2), 170.0)
    assert {p["strategy"]: p["reason"] for p in c.closed}["curve_93_fast"] == "curve_target"

    slow = PaperCurve(S())
    slow.on_create(create("S"), 0.0)
    slow.on_curve("S", acct(20), 45.0)                           # 5 SOL but after 45 s: no fast19
    slow.on_curve("S", acct(35), 70.0)                           # 10 SOL after 70 s: no fast34
    slow.on_curve("S", acct(36), 75.0)
    assert not {"curve_fast19", "curve_fast34"} & set(slow.watch["S"].held)


# ---- curve_fast34_mom: momentum after the signal and a dev bag under 25 % (user's choice, 2026-11-23) ------------
def _mom(dev_buy_tokens: float, fill_progress: float, live: bool = False):
    from app import curve_live

    c = PaperCurve(S())
    if live:
        c.live_signals = {"curve_fast34_mom"}
    c.on_create({**create(), "initialBuy": dev_buy_tokens}, 0.0)
    c.on_curve("M", acct(34), 5.0)                    # signal at 34 % sold, 5 s after launch
    start = curve_live.last_id()
    c.on_curve("M", acct(fill_progress), 7.5)         # our buy lands 2 s later
    return c, curve_live.take(start - 1, "curve_fast34_mom", 1e9)[0]


def test_mom_buys_only_when_the_curve_kept_rising_and_the_dev_bag_is_small():
    dev_10 = 0.10 * REAL_TOKENS_START
    c, _ = _mom(dev_10, 45.0)
    assert "curve_fast34_mom" in c.watch["M"].held and "curve_fast34" in c.watch["M"].held
    c, _ = _mom(dev_10, 36.0)                         # stalled at the level
    assert "curve_fast34_mom" not in c.watch["M"].held and "curve_fast34" in c.watch["M"].held
    c, _ = _mom(0.30 * REAL_TOKENS_START, 45.0)       # dev holds 30 %
    assert "curve_fast34_mom" not in c.watch["M"].held


def test_mom_sends_its_live_signal_only_once_its_filters_pass():
    from app import curve_live

    start = curve_live.last_id()
    c = PaperCurve(S())
    c.live_signals = {"curve_fast34_mom"}
    c.on_create({**create("Q"), "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
    c.on_curve("Q", acct(34), 5.0)
    assert curve_live.take(start, "curve_fast34_mom", 1e9)[0] == []          # not at the decision
    c.on_curve("Q", acct(45), 7.5)
    got = curve_live.take(start, "curve_fast34_mom", 1e9)[0]
    assert [g.mint for g in got] == ["Q"] and abs(got[0].progress - 45) < 0.1    # at the fill, its price
    start = curve_live.last_id()
    c2 = PaperCurve(S())
    c2.live_signals = {"curve_fast34_mom"}
    c2.on_create({**create("R"), "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
    c2.on_curve("R", acct(34), 5.0)
    c2.on_curve("R", acct(35), 7.5)                                          # stalled: no live signal at all
    assert curve_live.take(start, "curve_fast34_mom", 1e9)[0] == []


def test_mom_skips_a_copycat_named_like_a_coin_launched_just_before():
    from app.curve import name_key

    assert name_key("BЕAR") == name_key("bear") == "bear"           # Cyrillic Е, case
    c = PaperCurve(S())
    c.on_create({**create("ORIG"), "symbol": "BOBCAT", "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
    c.on_create({**create("COPY"), "symbol": "BobCat", "initialBuy": 0.10 * REAL_TOKENS_START}, 120.0)
    c.on_create({**create("LATE"), "symbol": "bobcat", "initialBuy": 0.10 * REAL_TOKENS_START}, 120.0 + 1801.0)
    assert not c.watch["ORIG"].copycat and c.watch["COPY"].copycat and not c.watch["LATE"].copycat
    for m, t in (("ORIG", 0.0), ("COPY", 120.0)):
        c.on_curve(m, acct(34), t + 5.0)
        c.on_curve(m, acct(45), t + 7.5)
    assert "curve_fast34_mom" in c.watch["ORIG"].held
    assert "curve_fast34_mom" not in c.watch["COPY"].held and "curve_fast34" in c.watch["COPY"].held
    assert c.watch["COPY"].held["curve_fast34"]["features"]["copycat"] is True


def test_mom_needs_a_real_rise_after_the_signal_not_a_launch_already_past_40():
    c = PaperCurve(S())
    c.on_create({**create("BUNDLE"), "symbol": "PLAN", "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
    c.on_curve("BUNDLE", acct(43.3), 0.1)             # launched at 43 % (its own bundle)
    c.on_curve("BUNDLE", acct(44.4), 2.6)             # +1 point: nobody kept buying
    assert "curve_fast34_mom" not in c.watch["BUNDLE"].held and "curve_fast34" in c.watch["BUNDLE"].held
    c.on_create({**create("REAL"), "symbol": "REAL", "initialBuy": 0.10 * REAL_TOKENS_START}, 10.0)
    c.on_curve("REAL", acct(36.6), 10.1)
    c.on_curve("REAL", acct(42.7), 12.6)              # +6 points and past 40 %
    assert "curve_fast34_mom" in c.watch["REAL"].held


def test_mom2_needs_buyers_before_the_signal_and_skips_bundler_launch_tools():
    def run(mint, uri, with_buy):
        c = PaperCurve(S())
        c.on_create({**create(mint), "symbol": mint, "uri": uri, "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
        if with_buy:
            c.on_curve(mint, acct(20), 2.0)
            c.on_curve(mint, acct(25), 3.0)              # a buy after the launch
        c.on_curve(mint, acct(34), 5.0)
        c.on_curve(mint, acct(45), 7.5)
        return c.watch[mint].held

    assert "curve_fast34_mom2" in run("A1", "https://ipfs.io/ipfs/x", True)
    held = run("A2", "https://ipfs.io/ipfs/x", False)
    assert "curve_fast34_mom2" not in held and "curve_fast34_mom" in held
    assert "curve_fast34_mom2" not in run("A3", "https://meta.thecompanysol.fun/x.json", True)


def test_curve_70_organic_needs_two_way_trading_and_rides_graduation():
    def run(mint, two_way):
        c = PaperCurve(S())
        c.on_create({**create(mint), "symbol": mint, "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
        t, p = 100.0, 40.0
        c.on_curve(mint, acct(p), t)
        for i in range(28):                             # a minute of trading before 70 %
            t += 2
            p += 1.3 if (not two_way or i % 3 != 2) else -0.5
            c.on_curve(mint, acct(p), t)
        c.on_curve(mint, acct(70), t + 1)
        c.on_curve(mint, acct(72), t + 3.5)
        return c

    c = run("O1", True)
    assert "curve_70_organic" in c.watch["O1"].held
    c.on_curve("O1", acct(100, complete=True), 200.0)  # graduation: it keeps riding (trailing stop)
    assert "curve_70_organic" in c.watch["O1"].held
    assert "curve_70_organic" not in run("O2", False).watch["O2"].held     # buys only: no sells


def test_paused_paper_strategy_opens_nothing_but_still_signals_live():
    from app import curve_live, paper_switches

    paper_switches.PAUSED = {"curve_fast34_mom", "curve_fast34"}
    try:
        start = curve_live.last_id()
        c = PaperCurve(S())
        c.live_signals = {"curve_fast34_mom", "curve_fast34"}
        c.on_create({**create("P"), "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
        c.on_curve("P", acct(34), 5.0)
        c.on_curve("P", acct(45), 7.5)
        held = c.watch["P"].held
        assert "curve_fast34_mom" not in held and "curve_fast34" not in held and "curve_30_70" in held
        sent = {s.strategy for s in curve_live.take(start, {"curve_fast34_mom", "curve_fast34"}, 1e9)[0]}
        assert sent == {"curve_fast34_mom", "curve_fast34"}
    finally:
        paper_switches.PAUSED = set()


def test_curve_70_organic_slow_only_buys_coins_that_took_15_min():
    def run(mint, t0):
        c = PaperCurve(S())
        c.on_create({**create(mint), "symbol": mint, "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
        t, p = t0, 45.0
        c.on_curve(mint, acct(p), t)
        for i in range(28):
            t += 2
            p += 0.9 if i % 3 != 2 else -0.4
            c.on_curve(mint, acct(p), t)
        c.on_curve(mint, acct(70), t + 1)
        c.on_curve(mint, acct(72), t + 3.5)
        return c.watch[mint].held

    fast, slow = run("F1", 100.0), run("S1", 1000.0)
    assert "curve_70_organic" in fast and "curve_70_organic_slow" not in fast
    assert "curve_70_organic_slow" in slow and "curve_70_organic" not in slow


def test_curve_70_organic_grad_sells_at_the_graduation():
    c = PaperCurve(S())
    c.on_create({**create("G1"), "symbol": "G1", "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
    t, p = 100.0, 45.0
    c.on_curve("G1", acct(p), t)
    for i in range(28):
        t += 2
        p += 0.9 if i % 3 != 2 else -0.4
        c.on_curve("G1", acct(p), t)
    c.on_curve("G1", acct(70), t + 1)
    c.on_curve("G1", acct(72), t + 3.5)
    held = c.watch["G1"].held
    assert "curve_70_organic_grad" in held and "curve_70_organic" in held
    c.on_curve("G1", acct(100, complete=True), t + 60)
    held = c.watch["G1"].held
    assert "curve_70_organic_grad" not in held and "curve_70_organic" in held        # sold vs still riding
    sold = [p for p in c.closed if p["strategy"] == "curve_70_organic_grad"][0]
    assert sold["reason"] == "graduated" and sold["pnl"] > 15     # jumped past 99 %: sold at the graduation, ~3x
    from app import curve_live
    assert "curve_70_organic_grad" in curve_live.live_rules()      # tradeable for real (exit level 99 %)


def _organic_coin(c, mint, fill_at, t0=100.0):
    c.on_create({**create(mint), "symbol": mint, "initialBuy": 0.10 * REAL_TOKENS_START}, 0.0)
    t, p = t0, 45.0
    c.on_curve(mint, acct(p), t)
    for i in range(28):
        t += 2
        p += 0.9 if i % 3 != 2 else -0.4
        c.on_curve(mint, acct(p), t)
    c.on_curve(mint, acct(70), t + 1)
    c.on_curve(mint, acct(fill_at), t + 3.5)
    return t + 3.5


def test_organic_v2_does_not_chase_a_price_that_ran_before_the_fill():
    c = PaperCurve(S())
    _organic_coin(c, "C1", 72.0)                      # +6 % between decision and fill: v2 skips it
    assert "curve_70_organic" in c.watch["C1"].held and "curve_70_organic_v2" not in c.watch["C1"].held
    _organic_coin(c, "C2", 70.5)                      # +1.6 %: bought
    assert "curve_70_organic_v2" in c.watch["C2"].held and "curve_70_organic_v2_late_sl" in c.watch["C2"].held


def test_curve_70_quiet_buys_a_slow_climb_and_skips_a_rush_of_buying():
    def run(mint, start):
        c = PaperCurve(S())
        c.on_create({**create(mint), "symbol": mint, "initialBuy": 0.05 * REAL_TOKENS_START}, 0.0)
        t, p = 100.0, start
        c.on_curve(mint, acct(p), t)
        for _ in range(30):                             # the minute before 70 %
            t += 2
            p += (69.9 - start) / 30
            c.on_curve(mint, acct(p), t)
        c.on_curve(mint, acct(70), t + 1)
        c.on_curve(mint, acct(70.3), t + 3.5)
        return c, t + 3.5

    c, t = run("Q1", 68.8)                              # ~1 SOL net in the minute: quiet
    held = c.watch["Q1"].held
    assert "curve_70_quiet" in held and "curve_70_quiet_wide_sl" in held and "curve_70" in held
    c.on_curve("Q1", acct(58.5), t + 10)                # about -29 %: the -25 % stop sells, the -45 % one holds
    held = c.watch["Q1"].held
    assert "curve_70_quiet" not in held and "curve_70_quiet_wide_sl" in held
    c.on_curve("Q1", acct(99.2), t + 60)                # sold at 99 % sold
    assert "curve_70_quiet_wide_sl" not in c.watch["Q1"].held
    assert [p["reason"] for p in c.closed if p["strategy"] == "curve_70_quiet_wide_sl"] == ["curve_target"]

    c2, _ = run("Q2", 61.0)                             # ~9 SOL net in the minute: a rush, skipped
    assert "curve_70" in c2.watch["Q2"].held and "curve_70_quiet" not in c2.watch["Q2"].held

    from app.curve_live import live_rules
    assert "curve_70_quiet" not in live_rules() and "curve_70_quiet_wide_sl" not in live_rules()   # paper only


def test_late_stop_waits_a_minute_unless_the_drop_is_deep():
    from app.curve_live import live_rules

    assert "curve_70_organic_v2_late_sl" not in live_rules() and "curve_70_organic_v2" in live_rules()
    c = PaperCurve(S())
    t = _organic_coin(c, "D1", 70.5)
    c.on_curve("D1", acct(59.5), t + 10)              # about -27 % at 10 s: v2 stops, the late stop waits
    held = c.watch["D1"].held
    assert "curve_70_organic_v2" not in held and "curve_70_organic_v2_late_sl" in held
    c.on_curve("D1", acct(59.4), t + 70)              # still under -25 % after a minute: now it stops
    assert "curve_70_organic_v2_late_sl" not in c.watch["D1"].held
    c2 = PaperCurve(S())
    t = _organic_coin(c2, "D2", 70.5)
    c2.on_curve("D2", acct(50), t + 5)                # about -44 % at 5 s: past -40 %, out at once
    assert "curve_70_organic_v2_late_sl" not in c2.watch["D2"].held


def test_curve_70_slow3_only_buys_coins_that_took_3_minutes_to_reach_70():
    from app.curve_live import live_rules

    def run(mint, at):
        c = PaperCurve(S())
        c.on_create({**create(mint), "symbol": mint, "initialBuy": 0.05 * REAL_TOKENS_START}, 0.0)
        c.on_curve(mint, acct(60.0), at - 10)
        c.on_curve(mint, acct(70.0), at)
        c.on_curve(mint, acct(70.3), at + 2.5)
        return c.watch[mint].held

    assert "curve_70_slow3" in run("S1", 200.0) and "curve_70" in run("S1", 200.0)
    held = run("S2", 60.0)                              # 70 % in a minute: skipped, plain curve_70 still buys
    assert "curve_70_slow3" not in held and "curve_70" in held
    assert "curve_70_slow3" not in live_rules()         # paper only


def test_organic_2min_and_be50_variants():
    from app.curve_live import live_rules, price_ratio

    c = PaperCurve(S())
    t = _organic_coin(c, "A1", 70.5, t0=100.0)        # 70 % about 157 s after launch
    held = c.watch["A1"].held
    assert {"curve_70_organic", "curve_70_organic_2min", "curve_70_organic_be50"} <= set(held)
    _organic_coin(c, "A2", 70.5, t0=10.0)              # about 67 s: too fast for the 2-min variant
    assert "curve_70_organic" in c.watch["A2"].held and "curve_70_organic_2min" not in c.watch["A2"].held

    up = next(x / 10 for x in range(706, 990) if price_ratio(70.5, x / 10) >= 1.55)   # +55 %
    c.on_curve("A1", acct(up), t + 20)
    c.on_curve("A1", acct(70.0), t + 40)               # back under the entry: only be50 leaves, at about 0
    held = c.watch["A1"].held
    assert "curve_70_organic_be50" not in held and "curve_70_organic" in held
    be = [p for p in c.closed if p["strategy"] == "curve_70_organic_be50"]
    assert [p["reason"] for p in be] == ["breakeven_stop"] and be[0]["pnl_pct"] > -10
    assert "curve_70_organic_2min" not in live_rules() and "curve_70_organic_be50" not in live_rules()


def test_organic_safe_waits_for_holders_and_skips_snipers_and_whales():
    from app.curve_live import live_rules

    def run(mint, facts, fail=False):
        c = PaperCurve(S())
        t = _organic_coin(c, mint, 70.5)
        w = c.watch[mint]
        assert "curve_70_organic" in w.held and "curve_70_organic_safe" not in w.held
        assert mint in c._holder_todo() and "curve_70_organic_safe" in w.pending   # asked, waiting

        async def check(url, m, http=None, until=None):
            if fail:
                raise RuntimeError("down")
            return facts
        import app.curve_holders as ch
        real = ch.check
        ch.check = check
        try:
            asyncio.run(c.fetch_holders())
        finally:
            ch.check = real
        c.on_curve(mint, acct(70.6), t + 1)
        return c.watch[mint]

    ok = {"creator": "D", "holders": 60, "top1_pct": 4.0, "top10_pct": 25.0, "dev_hold_pct": 0.0,
          "sniper_n": 3, "sniper_hold_pct": 0.0}
    w = run("H1", ok)
    assert "curve_70_organic_safe" in w.held
    assert w.held["curve_70_organic_safe"]["features"]["h_top1_pct"] == 4.0
    assert "curve_70_organic_safe" not in run("H2", {**ok, "sniper_hold_pct": 6.5}).held     # snipers still hold
    assert "curve_70_organic_safe" not in run("H3", {**ok, "top1_pct": 12.0}).held           # a whale
    w = run("H4", ok, fail=True)                                                             # unread: skipped
    assert "curve_70_organic_safe" not in w.held and "curve_70_organic_safe" not in w.pending
    assert "curve_70_organic_safe" not in live_rules()


def test_organic_safe_gives_up_when_holders_never_come():
    c = PaperCurve(S())
    t = _organic_coin(c, "H5", 70.5)
    for k in range(1, 40):
        c.on_curve("H5", acct(70.5 + k * 0.01), t + k * 0.6)
    assert "curve_70_organic_safe" not in c.watch["H5"].pending and "curve_70_organic_safe" not in c.watch["H5"].held


def test_curve_holders_replay_finds_snipers_top_holder_and_skips_the_curve():
    from app import curve_holders, pump_tx

    mint = "So11111111111111111111111111111111111111112"
    bc = pump_tx.curve_address(mint)

    def tx(slot, pre, post, create=False):
        def bals(d):
            return [{"mint": mint, "owner": o, "uiTokenAmount": {"uiAmount": v}} for o, v in d.items()]
        return {"slot": slot, "transaction": {"message": {"accountKeys": ["DEV"]}},
                "meta": {"err": None, "logMessages": ["Program log: Instruction: Create"] if create else [],
                         "preTokenBalances": bals(pre), "postTokenBalances": bals(post)}}

    txs = [tx(100, {}, {bc: 900e6, "DEV": 50e6}, create=True),
           tx(101, {bc: 900e6}, {bc: 860e6, "SNIPE": 40e6}),          # sniper, 1 slot after the create
           tx(150, {bc: 860e6}, {bc: 830e6, "LATE": 30e6}),
           tx(160, {"SNIPE": 40e6}, {})]                                # sniper sold out, account closed
    h = curve_holders.replay(mint, txs)
    assert h["sniper_n"] == 1 and h["sniper_hold_pct"] == 0.0
    assert h["top1_pct"] == 5.0 and h["dev_hold_pct"] == 5.0 and h["holders"] == 2
    import pytest
    with pytest.raises(curve_holders.NotPump):
        curve_holders.replay(mint, txs[1:])


def test_open_trades_carry_over_a_restart_and_close_normally():
    MV = "7ySJbt6vJG1hnKcmLphmfxwMc626S2tRGNCUdUM1pump"
    async def run():
        with tempfile.TemporaryDirectory() as d:
            eng = create_async_engine(f"sqlite+aiosqlite:///{d}/t.db")
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(eng, expire_on_commit=False)
            c = PaperCurve(S(), sol_usd=200.0)
            c.on_create(create(MV), 0.0)
            c.on_curve(MV, acct(85), 10.0)
            c.on_curve(MV, acct(86), 12.5)
            async with Session() as s:
                await c.flush(s)
                from app.models import Chain

                old = PaperPosition(strategy="curve_70", chain=Chain.solana, token_address="OLD", symbol="O", status="open", size_usd=30.0, entry_price=1.0,
                                    fill_price=1.0, tp_pct=0.0, sl_pct=-25.0, max_hold_minutes=60)
                s.add(old)                                  # a row from before this change: no saved state
                await s.commit()
            # the worker restarts: a new engine reads the open rows
            c2 = PaperCurve(S(), sol_usd=200.0)
            async with Session() as s:
                rows = (await s.execute(select(PaperPosition).where(PaperPosition.status == "open"))).scalars().all()
                left = c2.resume(rows, 100.0)
            held = set(c2.watch[MV].held)
            back = c2.after_reconnect(100.0)
            c2.on_curve(MV, acct(100, complete=True), 120.0)   # it graduates after the restart
            async with Session() as s:
                await c2.flush(s)
                rows = (await s.execute(select(PaperPosition).where(PaperPosition.token_address == MV))).scalars().all()
            await eng.dispose()
            return [r.token_address for r in left], held, back, rows

    left, held, back, rows = asyncio.run(run())
    assert left == ["OLD"]                                  # old rows are closed as before
    assert {"curve_70", "curve_85"} <= held and back == [MV]   # held again, watched once the feed connects
    done = {r.strategy: r for r in rows}
    assert done["curve_70"].status == "closed" and done["curve_70"].exit_reason == "graduated" \
        and done["curve_70"].pnl_usd > 0


def test_graduation_prices_are_recorded_for_the_boost_study():
    from app.models import GradPrice

    async def run():
        with tempfile.TemporaryDirectory() as d:
            eng = create_async_engine(f"sqlite+aiosqlite:///{d}/t.db")
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(eng, expire_on_commit=False)
            c = PaperCurve(S(), sol_usd=200.0)
            c.on_create(create("G"), 0.0)
            c.on_curve("G", acct(90), 50.0)
            c.on_curve("G", acct(100, complete=True), 60.0)           # graduates at t=60
            px = iter([1.0, 1.1, 1.2, 1.3, 1.35, 1.4, 1.5, 1.2, 1.1, 0.9])

            async def jup(mints):
                return {m: next(px) for m in mints}
            c._jup_usd = jup
            for o in c.GRAD_SAMPLE_S:                                  # one reading per sample time
                await c.sample_grads(Session, 60.0 + o)
            missed = dict(c.grads)
            async with Session() as s:
                rows = (await s.execute(select(GradPrice))).scalars().all()
            await eng.dispose()
            return rows, missed

    rows, missed = asyncio.run(run())
    assert not missed and len(rows) == 1
    r = rows[0]
    assert r.mint == "G" and r.secs_to_grad == 60.0 and r.curve_price_usd > 0
    assert r.prices["0"] == 1.0 and r.prices["300"] == 1.5 and r.prices["600"] == 0.9
