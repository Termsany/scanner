import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.live import ChaseError, Fill, SwapError
from app.models import Base, LivePosition, PriceOrder
from app.orders import ORDER, cancel_order, check_orders, create_order, validate

MINT = "HuN5y4fJU7LNkaPjV9LhxkY7LUwpynW7yhL4qoVhpump"


def _db(body):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/o.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                return await body(s)
        finally:
            await eng.dispose()
    return asyncio.run(go())


class FakeX:
    """Executor stub: a price per check (list, last one repeats), a buy that fills, chases or fails."""

    mode = "live"

    def __init__(self, prices, buy="ok", sol=5.0):
        self.prices, self.buy_mode, self.sol, self.bought = list(prices), buy, sol, []

    async def token_prices_fast(self, mints):
        px = self.prices.pop(0) if len(self.prices) > 1 else self.prices[0]
        return {m: {"usd": px, "decimals": 6} for m in mints}

    async def token_prices(self, mints):
        return {}

    async def sol_balance(self):
        return self.sol

    async def sol_price(self):
        return 100.0

    async def buy(self, mint, usd, max_price=None, slippage_bps=None):
        self.bought.append((mint, usd, max_price))
        if self.buy_mode == "chase":
            raise ChaseError(max_price * 1.2, max_price)
        if self.buy_mode == "fail":
            raise SwapError("no route")
        f = Fill("SIGO", usd / 100.0, 5_000_000, 100.0)
        f.timing = {"route": "pumpportal"}
        return f


def _eng(x, **kw):
    notes = []

    async def notify(t):
        notes.append(t)

    e = SimpleNamespace(s=Settings(live_orders_check_seconds=0, **kw), x=x, a=SimpleNamespace(name="main"),
                        notify=notify, _tag=lambda: "LIVE", _tx=lambda sig: sig, _orders_at=0.0, _order_seen={})
    return e, notes


async def _order(sess, **kw):
    args = dict(account="main", mode="live", mint=MINT, symbol="HUN", condition="below", trigger_price=1.0,
                amount_usd=20.0, max_over_pct=5.0, target_price=2.0, target_share=0.5, stop_price=0.8,
                expires_hours=24, price_now=1.3)
    args.update(kw)
    o, note = await create_order(sess, **args)
    assert o is not None, note
    await sess.commit()
    return o


def test_validate_rules():
    ok = dict(mint=MINT, condition="below", trigger_price=1.0, amount_usd=20, max_over_pct=5, target_price=2.0,
              target_share=0.5, stop_price=0.8, expires_hours=24, price_now=1.3)
    assert validate(**ok) is None
    assert "address" in validate(**{**ok, "mint": "not-a-coin"})
    assert "sell price" in validate(**{**ok, "target_price": 0.9})
    assert "stop price" in validate(**{**ok, "stop_price": 1.1})
    assert "already at or below" in validate(**{**ok, "price_now": 0.95})            # would fill at once
    assert "already at or above" in validate(**{**ok, "condition": "above", "price_now": 1.05})
    assert "Amount" in validate(**{**ok, "amount_usd": 0.5})


def test_dip_order_fills_after_two_readings_and_opens_an_owner_managed_position():
    x = FakeX([1.20, 0.99, 0.99])          # above, then at/below twice (not far): fills on the 2nd reading below

    async def body(sess):
        o = await _order(sess)
        e, notes = _eng(x)
        for _ in range(3):
            await check_orders(e, sess)
        pos = (await sess.execute(select(LivePosition))).scalars().one()
        await sess.refresh(o)
        return o, pos, notes

    o, pos, notes = _db(body)
    assert x.bought == [(MINT, 20.0, 1.05)]                       # one buy, capped at trigger + 5%
    assert o.status == "filled" and o.position_id == pos.id and "pumpportal" in o.note
    assert pos.leader_wallet == ORDER and pos.hands_off and pos.status == "open"
    assert (pos.target_price, pos.target_share, pos.stop_price) == (2.0, 0.5, 0.8)
    assert any("Price order" in n and "filled" in n for n in notes)


def test_far_past_trigger_fills_on_one_reading_and_breakout_orders_work():
    x = FakeX([0.90])                       # 10% under the trigger: one reading is enough

    async def dip(sess):
        o = await _order(sess)
        e, _ = _eng(x)
        await check_orders(e, sess)
        await sess.refresh(o)
        return o.status

    assert _db(dip) == "filled"

    x2 = FakeX([1.5, 2.1, 2.1])

    async def breakout(sess):
        o = await _order(sess, condition="above", trigger_price=2.0, target_price=3.0, stop_price=1.8, price_now=1.5)
        e, _ = _eng(x2)
        for _ in range(3):
            await check_orders(e, sess)
        await sess.refresh(o)
        return o.status

    assert _db(breakout) == "filled" and x2.bought[0][2] == 2.0 * 1.05


def test_a_jump_past_the_cap_keeps_the_order_waiting_and_failures_and_expiry_close_it():
    async def chase(sess):
        o = await _order(sess)
        e, _ = _eng(FakeX([0.5], buy="chase"))
        await check_orders(e, sess)
        await sess.refresh(o)
        return o.status, o.note

    status, note = _db(chase)
    assert status == "open" and "still waiting" in note

    async def failed(sess):
        o = await _order(sess)
        e, notes = _eng(FakeX([0.5], buy="fail"))
        await check_orders(e, sess)
        await sess.refresh(o)
        return o.status, notes

    status, notes = _db(failed)
    assert status == "failed" and any("buy failed" in n for n in notes)

    async def broke(sess):
        o = await _order(sess)
        e, notes = _eng(FakeX([0.5], sol=0.1))            # $20 needs 0.2 SOL + reserve
        await check_orders(e, sess)
        await sess.refresh(o)
        return o.status, notes

    status, notes = _db(broke)
    assert status == "failed" and any("only 0.100 SOL" in n for n in notes)

    async def expired(sess):
        o = await _order(sess)
        o.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await sess.commit()
        x = FakeX([0.5])
        e, notes = _eng(x)
        await check_orders(e, sess)
        await sess.refresh(o)
        return o.status, x.bought, notes

    status, bought, notes = _db(expired)
    assert status == "expired" and bought == [] and any("expired" in n for n in notes)


def test_cancel_and_other_accounts_orders_are_left_alone():
    async def body(sess):
        mine = await _order(sess)
        other = await _order(sess, account="big")
        ok, _ = await cancel_order(sess, mine.id)
        again, _ = await cancel_order(sess, mine.id)
        await sess.commit()
        x = FakeX([0.5])
        e, _ = _eng(x)
        await check_orders(e, sess)                       # engine of "main": its order is cancelled, big's not its own
        await sess.refresh(other)
        return ok, again, x.bought, other.status

    ok, again, bought, other_status = _db(body)
    assert ok and not again and bought == [] and other_status == "open"
