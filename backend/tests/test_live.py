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

    async def buy(self, mint, usd, max_price=None, slippage_bps=None):
        self.last_bps = slippage_bps
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

    async def buy(self, mint, usd, max_price=None, slippage_bps=None):
        f = await super().buy(mint, usd, max_price, slippage_bps)
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


def test_scanner_screen_blocks_failed_tokens():
    from app.screen import ScreenResult

    class FakeScreen:
        async def check(self, mint, label=""):
            return {"GOOD": ScreenResult(True, "Alpha Score 78", 78),
                    "LOW": ScreenResult(False, "Alpha Score 45 < 70", 45)}.get(mint, ScreenResult(False, "no data"))

    x, notes = FakeExec(), []

    async def note(t):
        notes.append(t)

    async def body(sess):
        eng = CopyEngine(x, _s(), notify=note, screener=FakeScreen())
        await eng.step(sess)
        sess.add_all([_trade(mint="GOOD"), _trade(mint="LOW"), _trade(mint="NODATA")])
        await sess.commit()
        await eng.step(sess)

    _db(body)
    assert [c[1] for c in x.calls] == ["GOOD"]
    assert any("Alpha Score 45 < 70" in n for n in notes) and any("no data" in n for n in notes)


def test_token_screener_rules():
    from app.screen import TokenScreener

    MINT = "Mint111111111111111111111111111111111111111"
    pair = {"chainId": "solana", "pairAddress": "P1", "baseToken": {"address": MINT, "symbol": "T"},
            "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
            "priceUsd": "0.001", "liquidity": {"usd": 60_000}, "volume": {"h1": 90_000, "h24": 400_000},
            "txns": {"h1": {"buys": 400, "sells": 250}, "m5": {"buys": 40, "sells": 20}},
            "priceChange": {"h1": 30, "m5": 4}, "marketCap": 500_000, "fdv": 500_000,
            "pairCreatedAt": int((datetime.now(timezone.utc) - timedelta(hours=3)).timestamp() * 1000)}

    class Dex:
        async def pairs_for_tokens(self, chain, addrs):
            return [pair]

    def rug_report(mint_auth=None, freeze=None, top=5.0, rugged=False):
        return {"mintAuthority": mint_auth, "freezeAuthority": freeze, "rugged": rugged, "score": 100,
                "risks": [{"name": "Rugged", "level": "danger", "description": "x"}] if rugged else [],
                "topHolders": [{"owner": f"H{i}", "address": f"A{i}", "pct": top} for i in range(10)],
                "totalHolders": 900, "markets": [{"pubkey": "P1", "lp": {"lpLockedPct": 100}}]}

    class Rug:
        def __init__(self, rep):
            self.rep = rep

        async def report(self, mint):
            return self.rep

    async def run(rep, **kw):
        sc = TokenScreener(Settings(**kw), dex=Dex(), rugcheck=Rug(rep))
        return await sc.check(MINT, "DULX")

    ok = asyncio.run(run(rug_report(), live_min_alpha_score=None))
    assert ok.ok and ok.score is not None
    assert not asyncio.run(run(rug_report(mint_auth="X"), live_min_alpha_score=None)).ok
    assert not asyncio.run(run(rug_report(freeze="X"), live_min_alpha_score=None)).ok
    assert "top 10" in asyncio.run(run(rug_report(top=8.0), live_min_alpha_score=None)).reason   # 80% > 50%
    assert "RUGGED" in asyncio.run(run(rug_report(rugged=True), live_min_alpha_score=None)).reason
    hi = asyncio.run(run(rug_report(), live_min_alpha_score=101))
    assert not hi.ok and "< 101" in hi.reason
    none = asyncio.run(run(None))
    assert not none.ok and "no buy" in none.reason                                   # fail closed
    assert asyncio.run(run(None, live_screen_fail_closed=False)).ok


def test_dropped_buy_is_rebroadcast_then_retried_with_fresh_quote(monkeypatch):
    import base64
    import json as _json

    import httpx
    import respx
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    import app.live as live_mod
    from app.live import SOL_MINT, JupiterExecutor

    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(),
                                                                       lamports=1))], [], Hash.default())
    unsigned = base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode()

    real_sleep = asyncio.sleep
    monkeypatch.setattr(live_mod.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(live_mod, "JUPITER", live_mod.JupiterGate(interval=0))
    live_mod._sol_px = (0.0, 0.0)

    state = {"attempt": 0, "sends": 0, "quotes": 0, "tokens": 0}
    MINT = "Mint111111111111111111111111111111111111111"

    def rpc(req):
        body = _json.loads(req.content)
        m = body["method"]
        if m == "sendTransaction":
            state["sends"] += 1
            return httpx.Response(200, json={"result": f"SIG{state['attempt']}"})
        if m == "getSignatureStatuses":
            if state["attempt"] == 2:            # second attempt lands
                state["tokens"] = 5000
                return httpx.Response(200, json={"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
            return httpx.Response(200, json={"result": {"value": [None]}})
        if m == "getBlockHeight":
            return httpx.Response(200, json={"result": 200})          # past lastValidBlockHeight=100 -> expired
        if m == "getBalance":
            return httpx.Response(200, json={"result": {"value": 2_000_000_000 - state["tokens"] * 10}})
        if m == "getTokenAccountsByOwner":
            accts = [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(state["tokens"])}}}}}}]
            return httpx.Response(200, json={"result": {"value": accts}})
        return httpx.Response(404)

    def quote(req):
        state["quotes"] += 1
        state["attempt"] += 1
        return httpx.Response(200, json={"outAmount": "5000", "inAmount": "1"})

    with respx.mock:
        respx.post("https://rpc.test/").mock(side_effect=rpc)
        respx.get(url__regex=r"https://jup\.test/price/v3.*").mock(return_value=httpx.Response(200, json={
            SOL_MINT: {"usdPrice": 100.0, "decimals": 9}, MINT: {"usdPrice": 0.004, "decimals": 0}}))
        respx.get(url__regex=r"https://jup\.test/swap/v1/quote.*").mock(side_effect=quote)
        respx.post("https://jup.test/swap/v1/swap").mock(return_value=httpx.Response(200, json={
            "swapTransaction": unsigned, "lastValidBlockHeight": 100}))

        async def go():
            x = JupiterExecutor(Settings(jupiter_api_url="https://jup.test", live_rpc_url="https://rpc.test/"),
                                live=True, private_key=str(kp))
            try:
                return await x.buy(MINT, 20.0, max_price=1.0)
            finally:
                await x.close()

        f = asyncio.run(go())

    assert f.signature == "SIG2" and f.tokens_raw == 5000
    assert state["quotes"] == 2                       # fresh quote for the retry
    assert state["sends"] > 3                         # first attempt was re-broadcast before it expired


def test_unconditional_leader_ignores_liquidity_chase_and_scanner():
    from app.screen import ScreenResult

    class NoScreen:
        async def check(self, mint, label=""):
            return ScreenResult(False, "Alpha Score 10 < 70", 10)

    x = FakeExec()
    x.pay_price = 5.0            # +400% over the leader's price
    s = _s(live_unconditional_wallets=[LEADER], live_max_chase_pct=10)

    async def body(sess):
        eng = CopyEngine(x, s, screener=NoScreen())
        await eng.step(sess)
        sess.add(_trade(mint="FREE", liq=900))            # tiny pool, price ran, scanner says no: still copied
        sess.add(_trade(wallet=OTHER, mint="GATED", liq=900))
        await sess.commit()
        await eng.step(sess)

    _db(body)
    assert [c[1] for c in x.calls] == ["FREE"]


def test_tracker_records_every_trade_of_unconditional_leader():
    import httpx

    from app.wallet_tracker import Trade, WalletTracker

    def dex(req):
        return httpx.Response(200, json=[{"baseToken": {"address": "THIN", "symbol": "THIN"}, "priceUsd": "1",
                                          "liquidity": {"usd": 900}}])

    s = Settings(tracker_min_buy_usd=100, tracker_min_liquidity_usd=5_000, live_unconditional_wallets=[LEADER])

    async def body(sess):
        t = WalletTracker(s, http=httpx.AsyncClient(transport=httpx.MockTransport(dex)))
        await t.record(sess, [Trade(Chain.solana, LEADER, "THIN", 5.0, "a"),        # $5, $900 pool
                              Trade(Chain.solana, LEADER, "NOPAIR", 5.0, "b"),      # no DEX pair at all
                              Trade(Chain.solana, OTHER, "THIN", 5.0, "c")], {})    # other wallet: filtered
        await sess.commit()
        rows = (await sess.execute(select(WalletTrade).order_by(WalletTrade.id))).scalars().all()
        await t.http.aclose()
        return [(r.tx_hash, r.wallet == LEADER, r.telegram_sent) for r in rows]

    assert _db(body) == [("a", True, True), ("b", True, True)]   # recorded, no Telegram alert (too small)


def test_slippage_failure_retries_with_wider_slippage(monkeypatch):
    import base64
    import json as _json
    from urllib.parse import parse_qs, urlparse

    import httpx
    import respx
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    import app.live as live_mod
    from app.live import SOL_MINT, JupiterExecutor

    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(),
                                                                       lamports=1))], [], Hash.default())
    unsigned = base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(live_mod.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(live_mod, "JUPITER", live_mod.JupiterGate(interval=0))
    live_mod._sol_px = (0.0, 0.0)
    MINT = "Mint111111111111111111111111111111111111111"
    st = {"bps": [], "tokens": 0}

    def rpc(req):
        m = _json.loads(req.content)["method"]
        if m == "sendTransaction":
            if len(st["bps"]) == 1:   # first attempt: simulation fails with Jupiter 6001
                return httpx.Response(200, json={"error": {"code": -32002, "message":
                    "Transaction simulation failed: Error processing Instruction 6: custom program error: 0x1771"}})
            return httpx.Response(200, json={"result": "SIGOK"})
        if m == "getSignatureStatuses":
            st["tokens"] = 7000
            return httpx.Response(200, json={"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
        if m == "getBalance":
            return httpx.Response(200, json={"result": {"value": 2_000_000_000}})
        if m == "getTokenAccountsByOwner":
            return httpx.Response(200, json={"result": {"value": [
                {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(st["tokens"])}}}}}}]}})
        return httpx.Response(404)

    def quote(req):
        st["bps"].append(int(parse_qs(urlparse(str(req.url)).query)["slippageBps"][0]))
        return httpx.Response(200, json={"outAmount": "7000"})

    with respx.mock:
        respx.post("https://rpc.test/").mock(side_effect=rpc)
        respx.get(url__regex=r"https://jup\.test/price/v3.*").mock(return_value=httpx.Response(200, json={
            SOL_MINT: {"usdPrice": 100.0, "decimals": 9}}))
        respx.get(url__regex=r"https://jup\.test/swap/v1/quote.*").mock(side_effect=quote)
        respx.post("https://jup.test/swap/v1/swap").mock(return_value=httpx.Response(200, json={
            "swapTransaction": unsigned, "lastValidBlockHeight": 10**12}))

        async def go():
            x = JupiterExecutor(Settings(jupiter_api_url="https://jup.test", live_rpc_url="https://rpc.test/"),
                                live=True, private_key=str(kp))
            try:
                return await x.buy(MINT, 20.0, slippage_bps=2500)
            finally:
                await x.close()

        f = asyncio.run(go())

    assert f.signature == "SIGOK" and f.tokens_raw == 7000
    assert st["bps"] == [2500, 4000]          # 25% first, then 1.6x after the slippage failure


# ---- sales made by hand in the wallet (Phantom) ---------------------------------


class WalletExec(FakeExec):
    """FakeExec with a real-looking wallet: token balances and an on-chain history of hand sales."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.balances, self.sales, self.history_calls = {}, {}, 0
        self.buys, self.buy_calls = {}, 0

    async def buy(self, mint, usd, max_price=None, slippage_bps=None):
        f = await super().buy(mint, usd, max_price, slippage_bps)
        self.balances[mint] = self.balances.get(mint, 0) + f.tokens_raw
        return f

    async def sell(self, mint, tokens_raw):
        if self.balances.get(mint, 0) <= 0:
            self.calls.append(("sell", mint, tokens_raw))
            raise SwapError("no tokens left to sell")
        f = await super().sell(mint, min(tokens_raw, self.balances[mint]))
        self.balances[mint] -= f.tokens_raw
        return f

    async def token_balance(self, mint):
        return self.balances.get(mint, 0)

    def hand_sell(self, mint, tokens, sol, sig):
        self.balances[mint] -= tokens
        self.sales.setdefault(mint, []).append(
            {"sig": sig, "at": datetime.now(timezone.utc), "tokens_raw": tokens, "sol": sol, "usd": 0.0})

    async def wallet_sales(self, mints, since):
        self.history_calls += 1
        return {m: list(self.sales.get(m, [])) for m in mints}

    def hand_buy(self, mint, tokens, sol, sig):
        self.balances[mint] = self.balances.get(mint, 0) + tokens
        self.buys.setdefault(mint, []).append(
            {"sig": sig, "at": datetime.now(timezone.utc), "tokens_raw": tokens, "sol": sol, "usd": 0.0})

    async def wallet_buys(self, mints, since):
        self.buy_calls += 1
        return {m: list(self.buys.get(m, [])) for m in mints}


def _run_wallet(steps, x=None):
    x = x or WalletExec()
    notes = []

    async def note(text):
        notes.append(text)

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0), notify=note)
        await eng.step(sess)
        for batch in steps:
            if callable(batch):
                batch()
            else:
                sess.add_all(batch)
                await sess.commit()
            await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all()

    return _db(body), x, notes


def test_full_hand_sale_closes_position_with_real_proceeds():
    x = WalletExec()
    pos, x, notes = _run_wallet([
        [_trade()],                                               # bot buys 1_000_000 tokens for $30
        lambda: x.hand_sell("M1", 1_000_000, 0.4, "hand1"),     # owner sells all in Phantom for 0.4 SOL ($60)
        [],                                                       # second check in a row books it
    ], x)
    p = pos[0]
    assert p.status == "closed" and p.exit_reason == "sold_by_hand" and p.tokens_raw == "0"
    assert abs(p.proceeds_usd - 60.0) < 1e-6 and abs(p.pnl_usd - 30.0) < 1e-6
    assert any(f.get("side") == "sell_hand" and f["sigs"] == ["hand1"] for f in p.fills)
    assert any("sold by hand" in n.lower() or "by hand" in n for n in notes)


def test_one_odd_balance_reading_books_nothing():
    x = WalletExec()
    pos, x, _ = _run_wallet([
        [_trade()],
        lambda: x.hand_sell("M1", 1_000_000, 0.4, "hand1"),     # only one check sees the shortfall
    ], x)
    assert pos[0].status == "open" and pos[0].proceeds_usd == 0 and x.history_calls == 0


def test_partial_hand_sale_keeps_rest_open_and_leader_sell_closes_it():
    x = WalletExec()
    pos, x, _ = _run_wallet([
        [_trade()],
        lambda: x.hand_sell("M1", 250_000, 0.1, "hand1"),       # sells a quarter for $15
        [],
        [_trade(side="sell", pct_sold=100, remaining=0)],         # leader exits: bot sells the other 750k
    ], x)
    p = pos[0]
    assert p.status == "closed" and p.exit_reason == "leader_sold"
    assert x.calls[-1] == ("sell", "M1", 750_000)
    assert abs(p.pnl_usd - (15.0 + 45.0 - 30.0)) < 1e-6           # hand $15 + bot 750k at 2x = $45


def test_hand_sale_is_never_booked_twice():
    x = WalletExec()
    pos, x, _ = _run_wallet([
        [_trade()],
        lambda: x.hand_sell("M1", 250_000, 0.1, "hand1"),
        [], [], [],
        lambda: x.hand_sell("M1", 750_000, 0.3, "hand2"),
        [], [],
    ], x)
    p = pos[0]
    assert p.status == "closed" and p.exit_reason == "sold_by_hand"
    assert abs(p.proceeds_usd - 60.0) < 1e-6 and abs(p.pnl_usd - 30.0) < 1e-6


def test_hand_sale_after_leader_sold_failed_with_no_tokens():
    x = WalletExec()
    pos, x, notes = _run_wallet([
        [_trade()],
        lambda: x.hand_sell("M1", 1_000_000, 0.6, "hand1"),     # owner took +200% ($90) by hand
        [_trade(side="sell", pct_sold=100, remaining=0)],         # leader exits, bot finds nothing to sell
        [],
    ], x)
    p = pos[0]
    assert p.status == "closed" and p.exit_reason == "sold_by_hand" and p.error is None
    assert abs(p.pnl_usd - 60.0) < 1e-6
    assert not any(f.get("side") == "sell_failed" for f in p.fills)


def test_tokens_gone_without_a_sale_are_not_booked():
    x = WalletExec()

    def moved():
        x.balances["M1"] = 0                                     # moved out, no sale in the history

    pos, x, _ = _run_wallet([[_trade()], moved, [], []], x)
    assert pos[0].status == "open" and pos[0].proceeds_usd == 0


def test_tx_deltas_reads_sol_wsol_stables_and_tokens():
    from app.live import SOL_MINT, _tx_deltas
    me, usdc = "Me111", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def bal(mint, owner, amount):
        return {"mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(amount)}}

    tx = {"blockTime": 1_700_000_000,
          "transaction": {"message": {"accountKeys": [{"pubkey": "Pool"}, {"pubkey": me}]}},
          "meta": {"preBalances": [5, 1_000_000_000], "postBalances": [5, 1_400_000_000],
                   "preTokenBalances": [bal("M1", me, 1000), bal(SOL_MINT, me, 0), bal(usdc, me, 0),
                                        bal("M1", "Pool", 9)],
                   "postTokenBalances": [bal("M1", me, 0), bal(SOL_MINT, me, 100_000_000),
                                         bal(usdc, me, 2_500_000), bal("M1", "Pool", 1009)]}}
    d = _tx_deltas(tx, me)
    assert d["tokens"] == {"M1": -1000}
    assert abs(d["sol"] - 0.5) < 1e-9 and abs(d["usd"] - 2.5) < 1e-9


# ---- take the capital out at +100% ------------------------------------------------


class PricedExec(WalletExec):
    """WalletExec with a live token price (USD per whole token, 6 decimals: 1_000_000 raw = 1 token)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.price = {}

    async def token_prices(self, mints):
        return {m: {"usd": self.price[m], "decimals": 6} for m in mints if m in self.price}


def _run_priced(steps, x):
    notes = []

    async def note(text):
        notes.append(text)

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0), notify=note)
        await eng.step(sess)
        for batch in steps:
            if callable(batch):
                batch()
            else:
                sess.add_all(batch)
                await sess.commit()
            await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all()

    return _db(body), x, notes


def test_takes_capital_at_2x_then_rest_follows_leader():
    x = PricedExec()
    pos, x, notes = _run_priced([
        [_trade()],                                    # $30 for 1_000_000 raw (1 token)
        lambda: x.price.update(M1=40.0),               # +33%: nothing
        lambda: x.price.update(M1=60.0),               # 2x, first check
        [],                                            # 2x again: sell $30 worth = half
        [], [],                                        # never twice
        [_trade(side="sell", pct_sold=100, remaining=0)],   # leader exits: the profit bag goes too
    ], x)
    sells = [c for c in x.calls if c[0] == "sell"]
    assert sells == [("sell", "M1", 500_000), ("sell", "M1", 500_000)]
    p = pos[0]
    assert p.status == "closed" and p.exit_reason == "leader_sold"
    assert any(f.get("reason") == "take_capital" for f in p.fills)
    assert abs(p.pnl_usd - 30.0) < 1e-6
    assert any("take capital" in n for n in notes)


def test_take_capital_needs_two_checks_in_a_row():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],
        lambda: x.price.update(M1=60.0),               # one spike...
        lambda: x.price.update(M1=50.0),               # ...gone
        [],
    ], x)
    assert [c for c in x.calls if c[0] == "sell"] == [] and pos[0].status == "open"


def test_take_capital_sells_only_what_is_missing_after_a_leader_trim():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],
        [_trade(side="sell", pct_sold=50, remaining=1)],    # leader trims half: we get $30*... at 2x = $30
        lambda: x.price.update(M1=200.0),
        [],
    ], x)
    # the trim already returned $30 (500k at 2x in FakeExec): capital is back, nothing more to take
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 500_000)]


def test_take_capital_off_when_disabled():
    x = PricedExec()

    async def body(sess):
        eng = CopyEngine(x, _s(live_take_capital_at_pct=None, live_take_capital_check_seconds=0))
        await eng.step(sess)
        sess.add_all([_trade()]); await sess.commit(); await eng.step(sess)
        x.price["M1"] = 100.0
        await eng.step(sess); await eng.step(sess)
    _db(body)
    assert [c for c in x.calls if c[0] == "sell"] == []


# ---- accounts from the database (/accounts page) ---------------------------------
W2 = "BCyNgSB1njYWrjHubBc56UXqDqzc4ixk8UHPhGbTQ5aQ"


def test_seed_copies_environment_accounts_with_leader_flags():
    from app.accounts import load_accounts, rows

    s = _s(live_unconditional_wallets=[LEADER], live_new_tokens_only_wallets=[W2],
           live_accounts=[{"name": "big", "size_usd": 50, "max_open_usd": 500, "wallets": [LEADER]}],
           live_take_capital_at_pct=100.0)
    s.live_wallets = [LEADER, W2]

    async def body(sess):
        rs = await rows(sess, s)
        await rows(sess, s)                                  # seeding happens once
        accts = await load_accounts(sess, s)
        return rs, accts
    rs, accts = _db(body)
    assert [r.name for r in rs] == ["main", "big"] and all(r.key_source == "env" for r in rs)
    main = {d["wallet"]: d for d in rs[0].leaders}
    assert main[LEADER]["unconditional"] and not main[LEADER]["new_tokens_only"]
    assert main[W2]["new_tokens_only"] and not main[W2]["unconditional"]
    assert accts[1].size_usd == 50 and accts[1].wallets == [LEADER] and accts[1].take_capital_at_pct == 100.0


def test_db_key_roundtrip_and_wrong_key():
    import pytest
    from cryptography.fernet import Fernet

    from app.accounts import AccountError, decrypt_key, encrypt_key, new_wallet

    s = _s(live_key_encryption_key=Fernet.generate_key().decode())
    pub, secret = new_wallet()
    assert str(load_keypair(secret).pubkey()) == pub
    tok = encrypt_key(s, secret)
    assert secret not in tok and decrypt_key(s, tok) == secret
    with pytest.raises(AccountError):
        encrypt_key(_s(), secret)                           # no encryption key on the server


def _engine_with(account_kw, steps):
    from app.live import Account, Leader

    x = FakeExec()
    leaders = account_kw.pop("leaders")
    acct = Account("main", 30, 100, 50, list(leaders), leaders=leaders, **account_kw)

    async def body(sess):
        eng = CopyEngine(x, _s(), account=acct)
        await eng.step(sess)
        for batch in steps:
            sess.add_all(batch)
            await sess.commit()
            await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all(), eng
    return _db(body), x


def test_per_leader_entry_size_and_unconditional():
    from app.live import Leader

    (pos, _), x = _engine_with({"leaders": {LEADER: Leader(LEADER, size_usd=10),
                                            W2: Leader(W2, unconditional=True)}},
                               [[_trade(mint="A")], [_trade(wallet=W2, mint="B", liq=100)]])
    assert [c for c in x.calls if c[0] == "buy"] == [("buy", "A", 10), ("buy", "B", 30)]   # B: low liq but free


def test_paused_account_skips_buys_but_still_sells():
    from app.live import Leader

    (pos, eng), x = _engine_with({"leaders": {LEADER: Leader(LEADER)}}, [[_trade(mint="A")]])
    assert x.calls == [("buy", "A", 30)]

    from app.live import Account
    x2 = FakeExec()
    acct = Account("main", 30, 100, 50, [LEADER], leaders={LEADER: Leader(LEADER)})

    async def body(sess):
        eng = CopyEngine(x2, _s(), account=acct)
        await eng.step(sess)
        sess.add(_trade(mint="A")); await sess.commit(); await eng.step(sess)
        eng.update(Account("main", 30, 100, 50, [LEADER], leaders={LEADER: Leader(LEADER)}, paused=True))
        sess.add(_trade(mint="B")); await sess.commit(); await eng.step(sess)
        sess.add(_trade(side="sell", mint="A", pct_sold=100, remaining=0)); await sess.commit(); await eng.step(sess)
    _db(body)
    assert x2.calls == [("buy", "A", 30), ("sell", "A", 1_000_000)]


def test_removed_leader_is_no_longer_copied():
    from app.live import Account, Leader

    x = FakeExec()
    acct = Account("main", 30, 100, 50, [LEADER, W2], leaders={LEADER: Leader(LEADER), W2: Leader(W2)})

    async def body(sess):
        eng = CopyEngine(x, _s(), account=acct)
        await eng.step(sess)
        eng.update(Account("main", 30, 100, 50, [W2], leaders={W2: Leader(W2)}))
        sess.add_all([_trade(mint="A"), _trade(wallet=W2, mint="B")]); await sess.commit(); await eng.step(sess)
    _db(body)
    assert x.calls == [("buy", "B", 30)]


def test_supervisor_starts_updates_and_stops_engines_without_restart():
    from app.live import Account, Leader, LiveTrader

    t = LiveTrader(_s(live_fast_detection=False))
    t._screener = None
    started, notes = [], []

    async def fake_run(a, x, mode, eng):
        started.append(a.name)
        await asyncio.sleep(3600)

    async def tg(text):
        notes.append(text)
    t._run_account, t._telegram = fake_run, tg

    def acct(name, size=30, wallets=(LEADER,)):
        return Account(name, size, 100, 50, list(wallets), leaders={w: Leader(w) for w in wallets})

    async def go():
        running, said = {}, set()
        await t._sync([acct("main"), acct("big", wallets=())], "dry", running, said, None)
        await asyncio.sleep(0)
        assert sorted(running) == ["main"] and started == ["main"]          # big has no leaders yet
        await t._sync([acct("main", size=12), acct("big")], "dry", running, said, None)
        await asyncio.sleep(0)
        assert running["main"][2].a.size_usd == 12 and started == ["main", "big"]   # edit applied in place
        await t._sync([acct("big")], "dry", running, said, None)
        assert sorted(running) == ["big"] and any("[main] stopped" in n for n in notes)
        for task, _, _ in running.values():
            task.cancel()
    asyncio.run(go())


def test_only_the_wallet_that_gave_the_entry_closes_the_position():
    """Both wallets are copied. A opens the position; B buying or selling the same token never touches it;
    only A's sell closes it."""
    from app.live import Leader

    (pos, _), x = _engine_with({"leaders": {LEADER: Leader(LEADER), W2: Leader(W2)}}, [
        [_trade(mint="T")],                                             # A buys: we copy
        [_trade(wallet=W2, mint="T")],                                  # B buys the same token: still one position
        [_trade(wallet=W2, side="sell", mint="T", pct_sold=100, remaining=0)],   # B exits: we keep holding
        [_trade(wallet=W2, side="sell", mint="T", pct_sold=50, remaining=1)],    # B trims: nothing either
    ])
    assert x.calls == [("buy", "T", 30)] and pos[0].status == "open" and pos[0].tokens_raw == "1000000"

    (pos, _), x = _engine_with({"leaders": {LEADER: Leader(LEADER), W2: Leader(W2)}}, [
        [_trade(mint="T")],
        [_trade(wallet=W2, side="sell", mint="T", pct_sold=100, remaining=0)],
        [_trade(side="sell", mint="T", pct_sold=100, remaining=0)],     # A exits: we exit
    ])
    assert x.calls == [("buy", "T", 30), ("sell", "T", 1_000_000)]
    assert pos[0].status == "closed" and pos[0].exit_reason == "leader_sold" and pos[0].leader_wallet == LEADER


def test_user_notes_never_name_the_leader():
    """A registered user's own chat gets buy/sell notes with neutral words, never the leader's wallet or label."""
    from app.live import Account, Leader

    x = FakeExec()
    sent = []

    async def send_to(chat, text):
        sent.append((chat, text))
    acct = Account("u7", 30, 100, 50, [LEADER], leaders={LEADER: Leader(LEADER, label="Whale DULX")},
                   user_chat_id="555", user_lang="en")

    async def body(sess):
        eng = CopyEngine(x, _s(), account=acct, send_to=send_to)
        await eng.step(sess)
        t = _trade(mint="T")
        t.wallet_label = "Whale DULX"
        sess.add(t); await sess.commit(); await eng.step(sess)
        sess.add(_trade(side="sell", mint="T", pct_sold=100, remaining=0)); await sess.commit(); await eng.step(sess)
    _db(body)
    texts = [t for c, t in sent]
    assert all(c == "555" for c, _ in sent) and len(texts) == 2
    assert "bought $T" in texts[0] and "exit signal" in texts[1] and "P&L" in texts[1]
    joined = " ".join(texts)
    assert LEADER not in joined and LEADER[:6] not in joined and "Whale" not in joined and "leader" not in joined.lower()


def test_no_user_chat_no_user_notes():
    from app.live import Account, Leader

    sent = []

    async def send_to(chat, text):
        sent.append(text)

    async def body(sess):
        eng = CopyEngine(FakeExec(), _s(), account=Account("main", 30, 100, 50, [LEADER],
                                                             leaders={LEADER: Leader(LEADER)}), send_to=send_to)
        await eng.step(sess)
        sess.add(_trade(mint="T")); await sess.commit(); await eng.step(sess)
    _db(body)
    assert sent == []


def test_engine_writes_a_heartbeat_for_the_watchdog():
    from app.live import HEARTBEAT, _get, _key

    async def body(sess):
        eng = CopyEngine(FakeExec(), _s())
        await eng.step(sess)
        await eng.step(sess)
        return await _get(sess, _key(HEARTBEAT, "main"))
    beat = _db(body)
    assert beat and beat.startswith("20")


def test_user_notes_in_arabic_by_default():
    from app.live import Account, Leader, user_note

    assert "اشترينا $T" in user_note("ar", "bought", sym="T", usd=30, tx="x")
    sold = user_note("ar", "sold", sym="T", usd=60, share=1.0, done=True, reason="leader_sold", pnl=30, tx="x")
    assert "إشارة خروج" in sold and "+$30.00" in sold
    assert "Sold" in user_note("en", "sold", sym="T", usd=60, share=0.5, done=False, reason="take_capital", pnl=0, tx="x")
    assert Account("u", 1, 1, 1, []).user_lang == "ar"


# ---- hold-only tokens -------------------------------------------------------------------
HOLD = "TTWofwAge91oFhZs7kpQdyrVRkmevgM88xijGvQFbKo"


def test_hold_only_token_is_bought_but_never_sold_by_the_bot():
    pos, x, notes = _run([
        [_trade(mint=HOLD)],
        [_trade(side="sell", mint=HOLD, pct_sold=50, remaining=1)],
        [_trade(side="sell", mint=HOLD, pct_sold=100, remaining=0)],
    ])
    assert x.calls == [("buy", HOLD, 30)]                       # bought, never sold
    assert pos[0].status == "open" and pos[0].tokens_raw == "1000000"
    assert any("hold-only" in n for n in notes)


def test_hold_only_token_skips_take_capital_and_max_hold_but_manual_sell_works():
    from datetime import timedelta

    from app.live import Account, Leader

    x = PricedExec()

    async def body(sess):
        eng = CopyEngine(x, _s(live_take_capital_check_seconds=0, live_reconcile_seconds=0, live_max_hold_hours=1),
                         account=Account("main", 30, 100, 50, [LEADER], leaders={LEADER: Leader(LEADER)}))
        await eng.step(sess)
        sess.add(_trade(mint=HOLD)); await sess.commit(); await eng.step(sess)
        x.price[HOLD] = 300.0                                      # 10x: no take-capital for hold-only
        p = (await sess.execute(select(LivePosition))).scalars().first()
        p.opened_at = p.opened_at - timedelta(hours=5)             # past max hold: still kept
        await sess.commit()
        await eng.step(sess); await eng.step(sess)
        assert [c for c in x.calls if c[0] == "sell"] == []
        p.manual_sell_frac = 0.5                                   # the owner sells by hand: allowed
        await sess.commit()
        await eng.step(sess)
        return p
    p = _db(body)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", HOLD, 500_000)]
    assert p.status == "open" and p.tokens_raw == "500000"


def test_fake_token_with_the_same_name_is_not_held():
    pos, x, _ = _run([
        [_trade(mint="FakeSOLmint1111111111111111111111111111111")],
        [_trade(side="sell", mint="FakeSOLmint1111111111111111111111111111111", pct_sold=100, remaining=0)],
    ])
    assert [c[0] for c in x.calls] == ["buy", "sell"]


def test_hand_buy_joins_the_position_cost_and_leaves_with_the_leader():
    x = WalletExec()
    pos, x, notes = _run_wallet([
        [_trade()],                                               # bot buys 1_000_000 tokens for $30
        lambda: x.hand_buy("M1", 500_000, 0.1333334, "hbuy1"),   # owner adds ~$20 in Phantom
        [],                                                       # second check in a row books it
        [], [],                                                   # never booked twice
        [_trade(side="sell", pct_sold=100, remaining=0)],         # leader exits: everything goes
    ], x)
    p = pos[0]
    assert abs(p.cost_usd - 50.0) < 0.01 and p.tokens_bought_raw == "1500000"
    assert sum(1 for f in p.fills if f.get("side") == "buy_hand") == 1
    assert p.status == "closed" and x.calls[-1] == ("sell", "M1", 1_500_000)
    assert x.balances["M1"] == 0
    assert any("by hand" in n and "total entry now $50.00" in n for n in notes)


def test_tokens_sent_in_without_a_purchase_stay_the_owners():
    x = WalletExec()
    pos, x, _ = _run_wallet([
        [_trade()],
        lambda: x.hand_buy("M1", 500_000, 0.0, "airdrop"),        # transfer in: no SOL spent
        [], [], [],
    ], x)
    p = pos[0]
    assert p.cost_usd == 30.0 and p.tokens_raw == "1000000"
    assert x.buy_calls == 1                                        # looked up once, not on every check


def test_leader_turned_off_buys_nothing_but_its_positions_still_follow_its_sells():
    from app.live import Account, Leader

    s = _s()
    on = Leader(LEADER, enabled=True)
    acct = Account("main", 30, 100, 50, [LEADER, OTHER], leaders={LEADER: on, OTHER: Leader(OTHER, enabled=False)})
    x = FakeExec()

    async def body(sess):
        eng = CopyEngine(x, s, account=acct)
        await eng.step(sess)
        sess.add_all([_trade(mint="A"), _trade(wallet=OTHER, mint="B")])
        await sess.commit()
        await eng.step(sess)
        bought = [c for c in x.calls if c[0] == "buy"]
        assert [c[1] for c in bought] == ["A"]                         # the switched-off leader's buy is skipped
        on.enabled = False                                             # switch the first one off too
        sess.add(_trade(side="sell", mint="A", pct_sold=100, remaining=0))
        await sess.commit()
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().all()

    pos = _db(body)
    assert len(pos) == 1 and pos[0].status == "closed" and pos[0].exit_reason == "leader_sold"


def test_hand_add_on_after_capital_was_taken_takes_it_again():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],                                          # bot: $30 for 1M
        lambda: x.price.update(M1=60.0), [],                 # 2x twice: capital out (sell 500k for $30)
        lambda: x.hand_buy("M1", 1_000_000, 0.2, "hb1"),     # owner adds $30 by hand: entry now $60
        [], [],                                              # booked; still 2x on the new entry -> take again
        [],
    ], x)
    sells = [c for c in x.calls if c[0] == "sell"]
    assert sells == [("sell", "M1", 500_000), ("sell", "M1", 500_000)]
    p = pos[0]
    assert abs(p.cost_usd - 60.0) < 0.01 and p.status == "open"
    assert [f.get("cost_at") for f in p.fills if f.get("reason") == "take_capital"] == [30.0, 60.0]


class AdoptExec(PricedExec):
    async def wallet_tokens(self):
        return {m: b for m, b in self.balances.items() if b > 0}

    async def token_symbol(self, mint):
        return "HND"


def test_own_hand_bought_token_is_adopted_and_capital_comes_out_at_2x():
    from datetime import datetime, timedelta, timezone

    x = AdoptExec()
    notes = []

    async def note(text):
        notes.append(text)

    def buy_old():
        x.hand_buy("H1", 1_000_000, 0.2, "hb1")                # $30 at $150/SOL
        x.buys["H1"][0]["at"] = datetime.now(timezone.utc) - timedelta(hours=3)
        x.hand_buy("AIR", 5_000_000, 0.0, "air1")              # airdrop: never adopted

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_max_hold_hours=1),
                         notify=note)
        await eng.step(sess)
        buy_old()
        await eng.step(sess)                                   # adopted as a manual position
        x.price.update(H1=60.0)
        await eng.step(sess)
        await eng.step(sess)                                   # 2x twice: capital out
        await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all()

    pos = _db(body)
    assert len(pos) == 1
    p = pos[0]
    assert p.leader_wallet == "manual" and p.symbol == "HND" and abs(p.cost_usd - 30.0) < 0.01
    assert p.status == "open"                                  # 3h old, max hold 1h: never sold for max hold
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "H1", 500_000)]
    assert any("bought by hand" in n for n in notes)


def test_after_capital_is_out_the_bag_ignores_trims_and_leaves_on_full_exit():
    x = PricedExec()
    pos, x, notes = _run_priced([
        [_trade()],                                          # $30 for 1M
        lambda: x.price.update(M1=60.0), [],                 # 2x twice: capital out (sell 500k)
        [_trade(side="sell", pct_sold=30, remaining=1)],     # leader trims 30%: ignored
        [_trade(side="sell", pct_sold=50, remaining=1)],     # trims again: ignored
        [_trade(side="sell", pct_sold=100, remaining=0)],    # sells all: the bag goes
    ], x)
    sells = [c for c in x.calls if c[0] == "sell"]
    assert sells == [("sell", "M1", 500_000), ("sell", "M1", 500_000)]
    assert pos[0].status == "closed" and pos[0].exit_reason == "leader_sold"
    assert any("waits for its full exit" in n for n in notes)


def test_before_capital_is_out_trims_are_still_mirrored():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],
        [_trade(side="sell", pct_sold=30, remaining=1)],     # capital not back yet: copy the trim
    ], x)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 300_000)]
