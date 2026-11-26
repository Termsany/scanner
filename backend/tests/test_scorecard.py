from datetime import datetime, timedelta, timezone

from app.models import Chain, GradPrice, PaperPosition
from app.paper import pnl
from app.scorecard import _loss_streak, _max_drawdown, paper_scorecard, score_strategy
from tests.test_paper import _db, _settings

T0 = datetime.now(timezone.utc) - timedelta(days=3)


def _pos(i, pct, strategy="curve_30_70", reason="take_profit", mint=None, size=100.0, day=0):
    t = T0 + timedelta(days=day, minutes=i)
    return PaperPosition(strategy=strategy, chain=Chain.solana, token_address=mint or f"M{i}", symbol="T",
                         status="closed", size_usd=size, entry_time=t, entry_price=1.0, fill_price=1.0,
                         tp_pct=0, sl_pct=-25, max_hold_minutes=60, exit_time=t + timedelta(minutes=5),
                         exit_price=1 + pct / 100, exit_reason=reason, pnl_usd=size * pct / 100, pnl_pct=pct)


def test_drawdown_and_streak():
    assert _max_drawdown([10, -5, -20, 30, -40]) == 40
    assert _max_drawdown([5, 5]) == 0
    assert _loss_streak([1, -1, -2, 0, 3, -1]) == 3


def test_too_few_trades_has_no_verdict():
    c = score_strategy([_pos(i, 10) for i in range(5)])
    assert c["verdict"] == "too_few" and c["trades"] == 5 and c["expectancy_pct"] == 10


def test_steady_winner_is_edge():
    c = score_strategy([_pos(i, 20 if i % 4 else -10) for i in range(60)])
    assert c["verdict"] == "edge"
    assert c["ci95_low_pct"] > 0 and c["win_rate"] == 0.75
    assert c["profit_factor"] == round(20 * 45 / (10 * 15), 2)
    assert c["trades_needed"] is None


def test_steady_loser_is_losing():
    c = score_strategy([_pos(i, -15 if i % 4 else 5) for i in range(60)])
    assert c["verdict"] == "losing" and c["ci95_high_pct"] < 0


def test_coin_flip_is_unproven_and_says_how_many_trades():
    c = score_strategy([_pos(i, 21 if i % 2 else -20) for i in range(40)])
    assert c["verdict"] == "unproven"
    assert c["ci95_low_pct"] < 0 < c["ci95_high_pct"]
    assert c["trades_needed"] > 40


def test_many_restart_exits_is_unreliable():
    trades = [_pos(i, 20, reason="restart" if i < 10 else "take_profit") for i in range(40)]
    assert score_strategy(trades)["verdict"] == "unreliable"


def test_median_and_green_days():
    trades = [_pos(0, 300, day=0), *[_pos(i, -10, day=1) for i in range(1, 5)]]
    c = score_strategy(trades)
    assert c["median_pct"] == -10 and c["expectancy_pct"] == 52
    assert (c["green_days"], c["days"]) == (1, 2)


def test_graduation_hold_compare():
    t = _pos(1, 50, mint="GRAD")
    g = GradPrice(mint="GRAD", grad_at=t.entry_time + timedelta(minutes=2), prices={"60": 2.0, "300": 0.5, "600": None})
    c = score_strategy([t], {"GRAD": g}, slippage_pct=1.5)["graduation"]
    assert c["trades"] == 1 and c["actual_avg_pct"] == 50
    assert c["hold_avg_pct"]["60"]["held"] == round(pnl(100, 1.0, 2.0, 1.5)[1], 2)
    assert c["hold_avg_pct"]["300"]["held"] < 0
    assert c["hold_avg_pct"]["600"] is None


def test_graduation_before_entry_is_ignored():
    t = _pos(1, 50, mint="GRAD")
    g = GradPrice(mint="GRAD", grad_at=t.entry_time - timedelta(minutes=1), prices={"60": 2.0})
    assert score_strategy([t], {"GRAD": g})["graduation"] is None


def test_scorecard_endpoint_query():
    async def body(s):
        s.add_all([_pos(i, 20 if i % 4 else -10) for i in range(40)])
        s.add_all([_pos(100 + i, -15, strategy="curve_70_organic") for i in range(3)])
        open_pos = _pos(200, 0)
        open_pos.status = "open"
        s.add(open_pos)
        s.add(GradPrice(mint="M1", grad_at=T0 + timedelta(minutes=3), prices={"60": 1.5, "300": 1.2}))
        await s.commit()
        return await paper_scorecard(s, hours=24 * 30, settings=_settings())

    out = _db(body)
    names = [c["name"] for c in out["strategies"]]
    assert names == ["curve_30_70", "curve_70_organic"]          # edge first, too_few after
    first = out["strategies"][0]
    assert first["trades"] == 40 and first["verdict"] == "edge"   # the open trade is not counted
    assert first["graduation"]["trades"] == 1
    assert out["strategies"][1]["verdict"] == "too_few"
