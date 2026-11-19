import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.live import CopyEngine, Fill, SwapError, load_keypair, set_paused
from app.models import Base, Chain, LivePosition, WalletTrade

LEADER = "DULX3e6vt1ZpHq3AdCUBZLPZ1CrhApdNKcewMPxZQRXf"
OTHER = "Other1111111111111111111111111111111111111"


class FakeExec:
    def __init__(self, mode="live", fail_sell=0, fail_buy=False):
        self.mode, self.pubkey = mode, "Me111"
        self.calls, self.fail_sell, self.fail_buy, self.n = [], fail_sell, fail_buy, 0

    async def sol_balance(self):
        return 1.0

    async def sol_price(self):
        return 150.0

    pay_price = None   # price per token the quote would give (for the max-chase guard)

    async def buy(self, mint, usd, max_price=None):
        if max_price and self.pay_price and self.pay_price > max_price:
            from app.live import ChaseError
            raise ChaseError(self.pay_price, max_price)
        self.calls.append(("buy", mint, usd))
        if self.fail_buy:
            raise SwapError("no route")
        self.n += 1
        return Fill(f"sig{self.n}", usd / 150.0, 1_000_000, 150.0)

    async def sell(self, mint, tokens_raw):
        self.calls.append(("sell", mint, tokens_raw))
        if self.fail_sell:
            self.fail_sell -= 1
            raise SwapError("rpc down")
        self.n += 1
        return Fill(f"sig{self.n}", tokens_raw / 1_000_000 * 0.4, tokens_raw, 150.0)   # 2x on a $30 buy

    async def close(self):
        pass


def _db(body):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/l.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                return await body(s)
        finally:
            await eng.dispose()
    return asyncio.run(go())


def _s(**kw):
    return Settings(live_mode="live", live_wallets=[LEADER], live_size_usd=30, live_max_open_usd=100,
                    live_min_liquidity_usd=10_000, **kw)


_n = [0]


def _trade(wallet=LEADER, side="buy", mint="M1", pct_sold=None, remaining=None, age_s=5, liq=50_000, kind=None):
    _n[0] += 1
    at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return WalletTrade(chain=Chain.solana, wallet=wallet, token_address=mint, symbol=mint, tx_hash=f"t{_n[0]}",
                       amount=1, price_usd=1.0, liquidity_usd=liq, side=side, kind=kind or side,
                       pct_sold=pct_sold, remaining_amount=remaining, block_time=at, detected_at=at)


def _run(steps):
    """steps: list of lists of trades added before each engine step. Returns (positions, executor, notes)."""
    x = FakeExec()
    notes = []

    async def note(text):
        notes.append(text)

    async def body(sess):
        eng = CopyEngine(x, _s(), notify=note)
        await eng.step(sess)                # first run: sets the cursor only
        for batch in steps:
            if callable(batch):
                await batch(sess)
                continue
            sess.add_all(batch)
            await sess.commit()
            await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all()

    return _db(body), x, notes


def test_copy_buy_then_partial_and_full_sell():
    pos, x, notes = _run([
        [_trade()],
        [_trade(side="sell", pct_sold=50, remaining=1)],
        [_trade(side="sell", pct_sold=100, remaining=0)],
    ])
    assert [c[0] for c in x.calls] == ["buy", "sell", "sell"]
    assert x.calls[1][2] == 500_000 and x.calls[2][2] == 500_000
    p = pos[0]
    assert p.status == "closed" and p.exit_reason == "leader_sold" and p.tokens_raw == "0"
    assert abs(p.pnl_usd - 30.0) < 1e-6          # bought $30, sold for $60
    assert any("Bought" in n for n in notes) and any("Closed" in n for n in notes)


def test_ignores_other_wallets_old_buys_low_liquidity_and_duplicates():
    pos, x, notes = _run([
        [_trade(wallet=OTHER, mint="A")],
        [_trade(mint="B", age_s=600)],
        [_trade(mint="C", liq=2_000)],
        [_trade(mint="D"), _trade(mint="D")],
    ])
    assert [c[1] for c in x.calls] == ["D"]
    assert len(pos) == 1
    assert any("min old" in n for n in notes) and any("liquidity" in n for n in notes)


def test_open_cap_and_pause():
    pos, x, notes = _run([
        [_trade(mint="A"), _trade(mint="B"), _trade(mint="C"), _trade(mint="D")],   # $30 x3 = $90; 4th > $100
    ])
    assert [c[1] for c in x.calls] == ["A", "B", "C"]
    assert any("limit" in n for n in notes)

    async def pause(sess):
        await set_paused(sess, True)
        await sess.commit()

    pos, x, notes = _run([pause, [_trade(mint="E")], [_trade(mint="E", side="sell", remaining=0)]])
    assert x.calls == [] and any("paused" in n for n in notes)


def test_sell_retry_and_transfer_exit():
    x_holder = {}

    pos, x, notes = _run([
        [_trade(mint="R")],
        [_trade(mint="R", side="sell", kind="transfer")],   # leader moved tokens out: full exit
    ])
    assert pos[0].status == "closed"

    x = FakeExec(fail_sell=2)
    async def body(sess):
        eng = CopyEngine(x, _s())
        await eng.step(sess)
        sess.add(_trade(mint="Q"))
        await sess.commit()
        await eng.step(sess)
        sess.add(_trade(mint="Q", side="sell", remaining=0))
        await sess.commit()
        await eng.step(sess)             # fails
        await eng.step(sess)             # retry fails
        await eng.step(sess)             # retry succeeds
        return (await sess.execute(select(LivePosition))).scalars().one()
    p = _db(body)
    assert p.status == "closed" and [c[0] for c in x.calls].count("sell") == 3
    del x_holder


def test_failed_buy_is_recorded():
    x = FakeExec(fail_buy=True)

    async def body(sess):
        eng = CopyEngine(x, _s())
        await eng.step(sess)
        sess.add(_trade(mint="F"))
        await sess.commit()
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().one()

    p = _db(body)
    assert p.status == "failed" and "no route" in p.error


def test_load_keypair_formats():
    from solders.keypair import Keypair

    kp = Keypair()
    assert str(load_keypair(str(kp)).pubkey()) == str(kp.pubkey())
    assert str(load_keypair(json.dumps(list(bytes(kp)))).pubkey()) == str(kp.pubkey())


def test_manual_sell_partial_then_full():
    from app.live import request_manual_sell

    x = FakeExec()
    notes = []

    async def note(t):
        notes.append(t)

    async def body(sess):
        eng = CopyEngine(x, _s(), notify=note)
        await eng.step(sess)
        sess.add(_trade(mint="MS"))
        await sess.commit()
        await eng.step(sess)
        pos = (await sess.execute(select(LivePosition))).scalars().one()

        assert (await request_manual_sell(sess, pos.id, 0.3))[0] is False        # only 25/50/100%
        ok, _ = await request_manual_sell(sess, pos.id, 0.5)
        assert ok and (await request_manual_sell(sess, pos.id, 1.0))[0] is False  # one request at a time
        await eng.step(sess)
        await sess.refresh(pos)
        assert pos.status == "open" and pos.tokens_raw == "500000" and pos.manual_sell_frac is None

        assert (await request_manual_sell(sess, pos.id, 1.0))[0]
        await eng.step(sess)
        await sess.refresh(pos)
        assert pos.status == "closed" and pos.exit_reason == "manual"
        assert (await request_manual_sell(sess, pos.id, 1.0))[0] is False        # closed
        return pos

    p = _db(body)
    assert [c[0] for c in x.calls] == ["buy", "sell", "sell"]
    assert abs(p.pnl_usd - 30.0) < 1e-6 and any("manual" in n for n in notes)


def test_accounts_from_settings(monkeypatch):
    from app.live import accounts_from_settings

    monkeypatch.setenv("LIVE_PRIVATE_KEY_BIG", "k-big")
    s = Settings(live_wallets=[LEADER], live_private_key="k-main", live_accounts=[
        {"name": "Big", "size_usd": 50, "max_open_usd": 500},
        {"name": "bad name!", "size_usd": 1},
        {"name": "main", "size_usd": 1},
    ])
    accts = accounts_from_settings(s)
    assert [a.name for a in accts] == ["main", "big"]
    big = accts[1]
    assert (big.size_usd, big.max_open_usd, big.max_daily_loss_usd) == (50, 500, 250)
    assert big.wallets == [LEADER] and big.private_key == "k-big" and accts[0].private_key == "k-main"


def test_two_accounts_copy_the_same_buy_with_their_own_budget():
    from app.live import Account, live_status

    s = _s()
    main = Account("main", 20, 100, 50, [LEADER])
    big = Account("big", 50, 500, 250, [LEADER])
    xm, xb = FakeExec(), FakeExec()

    async def body(sess):
        em, eb = CopyEngine(xm, s, account=main), CopyEngine(xb, s, account=big)
        await em.step(sess)
        await eb.step(sess)
        sess.add_all([_trade(mint=f"T{i}") for i in range(7)])
        await sess.commit()
        await em.step(sess)
        await eb.step(sess)
        sess.add(_trade(mint="T0", side="sell", remaining=0))
        await sess.commit()
        await em.step(sess)
        await eb.step(sess)
        return await live_status(sess, Settings(live_mode="live", live_wallets=[LEADER],
                                                 live_accounts=[{"name": "big", "size_usd": 50, "max_open_usd": 500}]))

    st = _db(body)
    assert [c[2] for c in xm.calls if c[0] == "buy"] == [20] * 5            # $100 / $20
    assert [c[2] for c in xb.calls if c[0] == "buy"] == [50] * 7            # $500 cap not reached
    assert sum(1 for c in xm.calls if c[0] == "sell") == 1 and sum(1 for c in xb.calls if c[0] == "sell") == 1
    a_main, a_big = st["accounts"]
    assert (len(a_main["open"]), len(a_main["closed"])) == (4, 1)
    assert (len(a_big["open"]), len(a_big["closed"])) == (6, 1)
    assert a_big["open_cost_usd"] == 300


def test_max_chase_skips_buys_far_above_the_leaders_price():
    x = FakeExec()
    notes = []

    async def note(t):
        notes.append(t)

    async def body(sess):
        eng = CopyEngine(x, _s(live_max_chase_pct=10), notify=note)
        await eng.step(sess)
        x.pay_price = 1.27                    # leader's price 1.0 -> +27%: skip
        sess.add(_trade(mint="RUN"))
        await sess.commit()
        await eng.step(sess)
        x.pay_price = 1.05                    # +5%: buy
        sess.add(_trade(mint="OK"))
        await sess.commit()
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().all()

    rows = _db(body)
    assert [c[1] for c in x.calls] == ["OK"] and [r.token_address for r in rows] == ["OK"]
    assert any("+27% above" in n for n in notes)


def test_jupiter_gate_retries_429_and_spaces_calls():
    import httpx
    import respx

    from app.live import JupiterGate

    gate = JupiterGate(interval=0.05, retries=3)

    async def go():
        with respx.mock:
            route = respx.get("https://jup.test/q").mock(side_effect=[
                httpx.Response(429, headers={"retry-after": "0.01"}),
                httpx.Response(429, headers={"retry-after": "0.01"}),
                httpx.Response(200, json={"ok": True}),
            ])
            async with httpx.AsyncClient() as http:
                t0 = asyncio.get_running_loop().time()
                r = await gate.request(http, "GET", "https://jup.test/q")
                return r, route.call_count, asyncio.get_running_loop().time() - t0

    r, calls, took = asyncio.run(go())
    assert r.status_code == 200 and calls == 3 and took >= 0.1   # 3 calls spaced >= 0.05s apart


class BalExec(FakeExec):
    """FakeExec with an on-chain token balance, including tokens the owner bought by hand."""

    def __init__(self, manual=None):
        super().__init__()
        self.bal = dict(manual or {})

    async def token_balance(self, mint):
        return self.bal.get(mint, 0)

    async def buy(self, mint, usd, max_price=None):
        f = await super().buy(mint, usd, max_price)
        self.bal[mint] = self.bal.get(mint, 0) + f.tokens_raw
        return f

    async def sell(self, mint, tokens_raw):
        amount = min(tokens_raw, self.bal.get(mint, 0))
        f = await super().sell(mint, amount)
        self.bal[mint] -= amount
        return f


def test_leader_sells_also_sell_manual_tokens():
    x = BalExec(manual={"MAN": 3_000_000, "ONLY": 2_000_000})
    notes = []

    async def note(t):
        notes.append(t)

    async def body(sess):
        eng = CopyEngine(x, _s(), notify=note)
        await eng.step(sess)
        sess.add(_trade(mint="MAN"))                                   # bot buys 1,000,000
        await sess.commit()
        await eng.step(sess)
        sess.add(_trade(mint="MAN", side="sell", pct_sold=50, remaining=1))
        await sess.commit()
        await eng.step(sess)
        assert x.bal["MAN"] == 2_000_000                               # 500k bot + 1.5M manual left
        sess.add(_trade(mint="MAN", side="sell", pct_sold=100, remaining=0))
        sess.add(_trade(mint="ONLY", side="sell", pct_sold=100, remaining=0))   # manual-only token
        await sess.commit()
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().one()

    pos = _db(body)
    sells = [c[2] for c in x.calls if c[0] == "sell"]
    assert sells == [500_000, 1_500_000, 500_000, 1_500_000, 2_000_000]
    assert x.bal == {"MAN": 0, "ONLY": 0}
    assert pos.status == "closed" and pos.tokens_raw == "0"
    assert sum("your own" in n for n in notes) == 3


def test_new_tokens_only_leader_skips_add_on_buys():
    pos, x, notes = None, FakeExec(), []
    s = _s(live_new_tokens_only_wallets=[LEADER])

    def buy(mint, amount, remaining, first=True):
        t = _trade(mint=mint)
        t.amount, t.remaining_amount, t.first_buy = amount, remaining, first
        return t

    async def note(t):
        notes.append(t)

    async def body(sess):
        eng = CopyEngine(x, s, notify=note)
        await eng.step(sess)
        sess.add_all([
            buy("NEW", 100, 100),                 # held 0 before: copy
            buy("ADD", 100, 350),                 # held 250 before: skip
            buy("OLD", 100, None, first=False),   # no balance info, seen before: skip
            buy("UNK", 100, None, first=True),    # no balance info, first time: copy
        ])
        sess.add(_trade(wallet=OTHER, mint="ADD2"))   # other leaders are not restricted
        await sess.commit()
        await eng.step(sess)

    _db(body)
    assert [c[1] for c in x.calls] == ["NEW", "UNK"]
    assert not any("already holding" in n for n in notes)    # skipped silently


def test_buy_trades_record_balance_after():
    from app.wallet_tracker import solana_trades_from_tx

    W = LEADER
    tx = {"blockTime": 1, "meta": {"err": None,
          "preTokenBalances": [{"owner": W, "mint": "M", "uiTokenAmount": {"uiAmount": 250.0}}],
          "postTokenBalances": [{"owner": W, "mint": "M", "uiTokenAmount": {"uiAmount": 350.0}}]}}
    [t] = solana_trades_from_tx(tx, W, "s")
    assert (t.side, t.amount, t.remaining) == ("buy", 100.0, 350.0)
