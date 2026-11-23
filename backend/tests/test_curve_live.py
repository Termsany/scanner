"""Real-money curve trades: signals from the paper engine, bought by the account that follows the rule (hands-off,
exit-level target, stop from our fill, time limit), and nothing for accounts that don't."""

import base64
import struct
from datetime import timedelta

from sqlalchemy import select

from app import curve_live
from app.config import Settings
from app.curve import CURVE_RULES, REAL_TOKENS_START, PaperCurve
from app.live import Account, CopyEngine, _now
from app.models import LivePosition
from tests.test_live import FakeExec, _db

K = 30.0 * 1_073_000_000.0


def acct(progress):
    sold = REAL_TOKENS_START * progress / 100
    v_tok = 1_073_000_000.0 - sold
    v_sol = K / v_tok
    raw = b"\0" * 8 + struct.pack("<5Q", int(v_tok * 1e6), int(v_sol * 1e9), int((REAL_TOKENS_START - sold) * 1e6),
                                  int((v_sol - 30) * 1e9), int(1e15)) + bytes([0])
    return base64.b64encode(raw).decode()


def S(**kw):
    base = dict(live_mode="live", live_size_usd=10, live_max_open_usd=100, live_curve_max_signal_age_s=8.0,
                live_curve_max_chase_pct=15.0, curve_paper_enabled=True, curve_latency_s=2.0)
    base.update(kw)
    return Settings(**base)


def test_price_ratio_and_live_rules():
    assert abs(curve_live.price_ratio(33.8, 96.6) - 6.881) < 0.01        # ~588% from 34% to 97% sold
    rules = curve_live.live_rules()
    assert "curve_fast34" in rules and "curve_30_70_sl" in rules
    assert "curve_70" not in rules and "curve_30_70" not in rules        # no stop: not traded for real


def test_publish_take_by_strategy_and_age():
    start = curve_live.last_id()
    r = CURVE_RULES["curve_fast34"]
    a = curve_live.publish("curve_fast34", r, "MA", "AAA", 1e-7, 34.0, "x", 900, now=100.0)
    curve_live.publish("curve_fast19", CURVE_RULES["curve_fast19"], "MB", "BBB", 1e-7, 20.0, "x", 900, now=100.0)
    assert curve_live.publish("curve_70", CURVE_RULES["curve_70"], "MC", "C", 1e-7, 70.0, "x", 900) is None
    got, last = curve_live.take(start, "curve_fast34", 8.0, now=105.0)
    assert [g.mint for g in got] == ["MA"] and last >= a.id
    assert curve_live.take(start, "curve_fast34", 8.0, now=120.0)[0] == []          # too late
    assert curve_live.take(last, "curve_fast34", 8.0, now=105.0)[0] == []           # already seen


def test_paper_engine_publishes_only_rules_a_live_account_trades():
    start = curve_live.last_id()
    c = PaperCurve(S())
    c.on_create({"txType": "create", "mint": "PX", "symbol": "PX", "bondingCurveKey": "CPX"}, 0.0)
    c.on_curve("PX", acct(35), 30.0)                       # curve_fast34 decides
    assert curve_live.take(start, "curve_fast34", 1e9)[0] == []
    c2 = PaperCurve(S())
    c2.live_signals = {"curve_fast34"}
    c2.on_create({"txType": "create", "mint": "PY", "symbol": "PY", "bondingCurveKey": "CPY"}, 0.0)
    c2.on_curve("PY", acct(35), 30.0)
    got, _ = curve_live.take(start, "curve_fast34", 1e9)
    assert [g.mint for g in got] == ["PY"] and got[0].sl_pct == -25.0 and got[0].max_hold_s == 900.0


def test_live_account_buys_the_signal_hands_off_with_target_stop_and_time_limit():
    s = S()
    curve = Account("curve", 10, 30, 20, [], curve_strategy="curve_fast34")
    other = Account("main", 10, 30, 20, [])
    x, y = FakeExec(), FakeExec()

    async def body(sess):
        eng, eng2 = CopyEngine(x, s, account=curve), CopyEngine(y, s, account=other)
        await eng.step(sess)
        await eng2.step(sess)
        r = CURVE_RULES["curve_fast34"]
        curve_live.publish("curve_fast34", r, "MINT1pump", "ONE", 2e-7, 34.0, "curve 34% at 40s", 900)
        curve_live.publish("curve_fast34", r, "MINT1pump", "ONE", 2e-7, 35.0, "again", 900)   # same coin
        curve_live.publish("curve_fast34", r, "OLDpump", "OLD", 2e-7, 34.0, "late", 900, now=0.0)
        await eng.step(sess)
        await eng2.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().all()

    pos = _db(body)
    assert [c for c in x.calls if c[0] == "buy"] == [("buy", "MINT1pump", 10)] and y.calls == []
    p = pos[0]
    assert len(pos) == 1 and p.account == "curve" and p.leader_wallet == "curve:curve_fast34" and p.hands_off
    assert abs(p.target_price - 2e-7 * 150 * curve_live.price_ratio(34.0, 96.6)) < 1e-12
    assert p.stop_pct == -25.0 and p.target_share == 1.0
    assert timedelta(minutes=14) < p.sell_by.replace(tzinfo=_now().tzinfo) - _now() <= timedelta(minutes=15)


def test_daily_loss_limit_and_chase_cap_skip_the_buy():
    s = S()
    x = FakeExec()
    x.pay_price = 2e-7 * 150 * 1.2                           # 20% above the decision: over the 15% cap

    async def body(sess):
        eng = CopyEngine(x, s, account=Account("curve", 10, 30, 20, [], curve_strategy="curve_fast34"))
        await eng.step(sess)
        curve_live.publish("curve_fast34", CURVE_RULES["curve_fast34"], "RUNpump", "RUN", 2e-7, 34.0, "x", 900)
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().all()

    assert _db(body) == [] and x.calls == []


def test_time_limit_sells_everything():
    s = S()
    x = FakeExec()

    async def body(sess):
        eng = CopyEngine(x, s, account=Account("curve", 10, 30, 20, [], curve_strategy="curve_fast34"))
        sess.add(LivePosition(mode="live", account="curve", token_address="TIMEpump", symbol="T",
                              leader_wallet="curve:curve_fast34", leader_trade_id=0, status="open", cost_usd=10,
                              tokens_raw="1000000", tokens_bought_raw="1000000", hands_off=True, stop_pct=-25.0,
                              sell_by=_now() - timedelta(seconds=1), fills=[]))
        await sess.commit()
        await eng.step(sess)
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().all()

    p = _db(body)[0]
    assert ("sell", "TIMEpump", 1_000_000) in x.calls and p.status == "closed" and p.exit_reason == "quick_time"


def test_a_curve_position_can_be_sold_by_hand_from_the_page():
    """The owner's Sell button (request_manual_sell) works on a real curve position like on any other."""
    from app.live import request_manual_sell

    s = S()
    x = FakeExec()

    async def body(sess):
        eng = CopyEngine(x, s, account=Account("curve", 10, 30, 20, [], curve_strategy="curve_fast34"))
        sess.add(LivePosition(mode="live", account="curve", token_address="HANDpump", symbol="H",
                              leader_wallet="curve:curve_fast34", leader_trade_id=0, status="open", cost_usd=10,
                              tokens_raw="1000000", tokens_bought_raw="1000000", hands_off=True, stop_pct=-25.0,
                              sell_by=_now() + timedelta(minutes=10), fills=[]))
        await sess.commit()
        pid = (await sess.execute(select(LivePosition.id))).scalar_one()
        ok, _ = await request_manual_sell(sess, pid, 1.0)
        await eng.step(sess)            # first step: sets the trade cursor
        await eng.step(sess)
        return ok, (await sess.execute(select(LivePosition))).scalars().one()

    ok, p = _db(body)
    assert ok and ("sell", "HANDpump", 1_000_000) in x.calls
    assert p.status == "closed" and p.exit_reason == "manual"
