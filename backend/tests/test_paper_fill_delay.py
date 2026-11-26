"""Copy trades fill PAPER_COPY_DELAY_SECONDS after the wallet's buy, at the market price then (app/paper._fill_pending)."""
from datetime import timedelta

from sqlalchemy import select

from app import paper
from app.models import PaperPosition
from app.paper import process_entries, process_exits
from tests.test_paper import LEADER, MINT, _db, _prices, _settings, _trade


def _later(seconds):
    """Move the engine's clock forward."""
    real = paper._now
    t = real() + timedelta(seconds=seconds)
    paper._now = lambda: t
    return real


async def _open_copy(sess, s, price=1.0):
    await process_entries(sess, s)            # set cursors
    await sess.commit()
    sess.add(_trade(1, LEADER, price=price))
    await sess.commit()
    await process_entries(sess, s)
    await sess.commit()
    return {p.strategy: p for p in (await sess.execute(select(PaperPosition))).scalars().all()}


def test_copy_trade_waits_then_fills_at_the_later_market_price():
    s = _settings(paper_copy_delay_seconds=20)

    async def body(sess):
        pos = await _open_copy(sess, s)
        d = pos["dulx_all"]
        assert d.features["fill_pending"] and d.entry_price == 1.0

        await process_exits(sess, _prices({MINT: 1.3}), s)          # too early: still waiting
        await sess.commit()
        await sess.refresh(d)
        assert d.features.get("fill_pending") and d.entry_price == 1.0

        real = _later(25)
        try:
            await process_exits(sess, _prices({MINT: 1.3}), s)
        finally:
            paper._now = real
        await sess.commit()
        await sess.refresh(d)
        assert "fill_pending" not in d.features
        assert d.entry_price == 1.3 and abs(d.fill_price - 1.3 * 1.015) < 1e-9
        assert d.features["signal_price"] == 1.0 and d.features["paid_vs_signal_pct"] == 30.0
        assert d.peak_pct == 0.0 and d.status == "open"
        return True

    assert _db(body)


def test_fill_is_never_below_the_wallets_price():
    s = _settings(paper_copy_delay_seconds=20)

    async def body(sess):
        pos = await _open_copy(sess, s)
        real = _later(25)
        try:
            await process_exits(sess, _prices({MINT: 0.8}), s)
        finally:
            paper._now = real
        await sess.commit()
        d = pos["dulx_all"]
        await sess.refresh(d)
        assert d.entry_price == 1.0 and d.features["paid_vs_signal_pct"] == 0.0
        return True

    assert _db(body)


def test_leader_sell_before_our_fill_exits_at_our_entry():
    s = _settings(paper_copy_delay_seconds=20)

    async def body(sess):
        pos = await _open_copy(sess, s)
        sess.add(_trade(2, LEADER, side="sell", price=1.5, pct_sold=100.0, remaining=0))   # it sells within 20 s
        await sess.commit()
        real = _later(25)
        try:
            await process_exits(sess, _prices({MINT: 1.5}), s)
        finally:
            paper._now = real
        await sess.commit()
        d = pos["dulx_all"]
        await sess.refresh(d)
        # filled at 1.5, the wallet was already out: in and straight out, fees only (no paper profit from its pump)
        assert d.status == "closed" and d.exit_reason == "leader_sold" and d.pnl_pct < 0
        return True

    assert _db(body)


def test_no_price_for_too_long_closes_unfilled():
    s = _settings(paper_copy_delay_seconds=20)

    async def body(sess):
        pos = await _open_copy(sess, s)
        real = _later(s.paper_no_price_minutes * 60 + 1)
        try:
            await process_exits(sess, _prices({}), s)
        finally:
            paper._now = real
        await sess.commit()
        d = pos["dulx_all"]
        await sess.refresh(d)
        assert d.status == "closed" and d.exit_reason == "no_fill"
        return True

    assert _db(body)


def test_delay_off_fills_at_once():
    s = _settings(paper_copy_delay_seconds=0)

    async def body(sess):
        pos = await _open_copy(sess, s)
        assert not pos["dulx_all"].features
        return True

    assert _db(body)
