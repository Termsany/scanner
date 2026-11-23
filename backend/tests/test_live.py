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
    kw.setdefault("live_trim_cap_pct", None)      # most tests check plain copying; the cap has its own test
    kw.setdefault("live_copy_trims", True)
    kw.setdefault("live_follow_after_trim", False)
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
        if m == "getTokenSupply":
            return httpx.Response(200, json={"result": {"value": {"decimals": 0, "amount": "1"}}})
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
        price_route = respx.get(url__regex=r"https://jup\.test/price/v3.*").mock(return_value=httpx.Response(200, json={
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
    assert price_route.call_count == 1                # SOL's price once; decimals from the RPC, not Jupiter
    assert state["sends"] > 3                         # first attempt was re-broadcast before it expired


def test_unconditional_leader_ignores_liquidity_chase_and_scanner():
    from app.screen import ScreenResult

    class NoScreen:
        async def check(self, mint, label=""):
            return ScreenResult(False, "Alpha Score 10 < 70", 10)

    x = FakeExec()
    x.pay_price = 5.0            # +400% over the leader's price
    s = _s(live_unconditional_wallets=[LEADER], live_max_chase_pct=10, live_max_chase_pct_no_checks=None)

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


def _run_priced(steps, x, **settings):
    notes = []

    async def note(text):
        notes.append(text)

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, **settings), notify=note)
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
        # big has no leaders yet: its engine still runs (hand sales, targets, page sells on what it holds)
        assert sorted(running) == ["big", "main"] and started == ["main", "big"]
        await t._sync([acct("main", size=12), acct("big")], "dry", running, said, None)
        await asyncio.sleep(0)
        assert running["main"][2].a.size_usd == 12 and started == ["main", "big"]   # edits applied in place
        assert LEADER in running["big"][2].a.wallets                                 # big got its leader, no restart
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
HOLD = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"     # ETH (hold-only; TTWO is now blocked from buys)


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



def test_no_checks_leader_still_has_a_chase_limit_and_per_wallet_override():
    from app.live import Account, Leader

    x = FakeExec()
    free = Leader(LEADER, unconditional=True)                     # no checks: default +50% limit
    strict = Leader(OTHER, max_chase_pct=20)                       # its own +20%
    acct = Account("main", 30, 500, 50, [LEADER, OTHER], leaders={LEADER: free, OTHER: strict})
    notes = []

    async def note(t):
        notes.append(t)

    async def body(sess):
        eng = CopyEngine(x, _s(), account=acct, notify=note)
        await eng.step(sess)
        x.pay_price = 4.5                                          # +350% over the leader: past +300%, skip
        sess.add(_trade(mint="SI"))
        await sess.commit()
        await eng.step(sess)
        x.pay_price = 1.4                                          # +40%: fine for the no-checks leader
        sess.add(_trade(mint="OK1"))
        await sess.commit()
        await eng.step(sess)
        sess.add(_trade(wallet=OTHER, mint="OK2", liq=900_000))    # +40% > its own +20%: skip
        await sess.commit()
        await eng.step(sess)

    _db(body)
    assert [c[1] for c in x.calls if c[0] == "buy"] == ["OK1"]
    assert any("+350% above" in n for n in notes) and any("limit +20%" in n for n in notes)


class FastExec(PricedExec):
    """DexScreener price lags; Jupiter (fast) has the real one."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fast, self.fast_calls = {}, 0

    async def token_prices_fast(self, mints):
        self.fast_calls += 1
        return {m: {"usd": self.fast[m], "decimals": 6} for m in mints if m in self.fast}


def test_take_capital_uses_the_fast_price_near_target_and_one_strong_reading_sells():
    x = FastExec()
    pos, x, _ = _run_priced([
        [_trade()],                                             # $30 for 1 token
        lambda: x.price.update(M1=31.0),                         # far from target: Jupiter not asked
        lambda: (x.price.update(M1=50.0), x.fast.update(M1=75.0)),  # Dex 1.67x (near) -> Jupiter says 2.5x: sell now
    ], x)
    sells = [c for c in x.calls if c[0] == "sell"]
    assert sells == [("sell", "M1", 400_000)]                    # $30 of $75 = 40%
    assert x.fast_calls == 2      # right after the buy (no DexScreener price yet) and near the target


def test_just_past_the_target_still_needs_two_readings():
    x = FastExec()
    pos, x, _ = _run_priced([
        [_trade()],
        lambda: (x.price.update(M1=61.0), x.fast.update(M1=61.0)),   # 2.03x: one reading only
        lambda: (x.price.update(M1=50.0), x.fast.update(M1=50.0)),   # gone
    ], x)
    assert [c for c in x.calls if c[0] == "sell"] == []



def _aged_position(x, minutes, proceeds=0.0, leader=LEADER, mint="M1"):
    from datetime import datetime, timedelta, timezone

    return LivePosition(mode="live", account="main", token_address=mint, symbol=mint, leader_wallet=leader,
                        leader_trade_id=0, status="open", cost_usd=30.0, proceeds_usd=proceeds,
                        tokens_raw="1000000", tokens_bought_raw="1000000",
                        opened_at=datetime.now(timezone.utc) - timedelta(minutes=minutes))


def test_time_stop_sells_when_capital_never_came_back():
    x = FakeExec()

    async def body(sess):
        sess.add_all([_aged_position(x, 95, mint="OLD"),                 # 95 min, nothing back: sell
                      _aged_position(x, 30, mint="YOUNG"),               # 30 min: wait
                      _aged_position(x, 300, proceeds=30.0, mint="BAG"),  # capital already back: keep the bag
                      _aged_position(x, 300, leader="manual", mint="MINE")])   # bought by hand: never
        await sess.commit()
        eng = CopyEngine(x, _s(live_time_stop_minutes=90))
        await eng.step(sess)                                             # first step: sets the cursor only
        await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all()

    pos = _db(body)
    assert [c[1] for c in x.calls if c[0] == "sell"] == ["OLD"]
    by = {p.token_address: p for p in pos}
    assert by["OLD"].status == "closed" and by["OLD"].exit_reason == "time_stop"
    assert all(by[m].status == "open" for m in ("YOUNG", "BAG", "MINE"))


def test_time_stop_off():
    x = FakeExec()

    async def body(sess):
        sess.add(_aged_position(x, 500))
        await sess.commit()
        eng = CopyEngine(x, _s(live_time_stop_minutes=None))
        await eng.step(sess)
        await eng.step(sess)

    _db(body)
    assert [c for c in x.calls if c[0] == "sell"] == []


def test_time_stop_is_off_by_default():
    from app.config import Settings

    assert Settings().live_time_stop_minutes is None


def test_partial_sells_are_copied_only_up_to_48_percent_then_the_rest_waits():
    x = PricedExec()
    pos, x, notes = _run_priced([
        [_trade()],                                          # 1M tokens for $30
        [_trade(side="sell", pct_sold=30, remaining=1)],     # copy: 300k
        [_trade(side="sell", pct_sold=40, remaining=1)],     # wants 280k, only 180k left under 48%: 180k
        [_trade(side="sell", pct_sold=20, remaining=1)],     # cap reached: ignored
        [_trade(side="sell", pct_sold=100, remaining=0)],    # leader is out: sell the rest (520k)
    ], x, live_trim_cap_pct=48.0)
    sells = [c for c in x.calls if c[0] == "sell"]
    assert sells == [("sell", "M1", 300_000), ("sell", "M1", 180_000), ("sell", "M1", 520_000)]
    assert pos[0].status == "closed" and pos[0].exit_reason == "leader_sold"
    assert any("already sold" in n for n in notes) and any("only up to 48%" in n for n in notes)


def test_at_2x_sell_48_percent_of_the_coins_counting_earlier_trims():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],                                          # 1M tokens for $30
        lambda: x.price.update(M1=60.0), [],                 # 2x twice: sell 48% of the coins = 480k
        [_trade(side="sell", pct_sold=30, remaining=1)],     # leader trims: 48% already sold -> ignored
        [_trade(side="sell", pct_sold=100, remaining=0)],    # leader out: the rest (520k)
    ], x, live_trim_cap_pct=48.0)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 480_000), ("sell", "M1", 520_000)]

    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],
        [_trade(side="sell", pct_sold=30, remaining=1)],     # trim first: 300k
        lambda: x.price.update(M1=200.0), [],                # then 2x: only 180k more, up to 48%
        [],                                                  # never again
    ], x, live_trim_cap_pct=48.0)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 300_000), ("sell", "M1", 180_000)]



def test_users_rule_50_percent_at_2x_partial_sells_ignored_rest_on_full_exit():
    x = PricedExec()
    pos, x, notes = _run_priced([
        [_trade()],                                          # 1M tokens for $30
        [_trade(side="sell", pct_sold=30, remaining=1)],     # partial sell before 2x: ignored
        lambda: x.price.update(M1=60.0), [],                 # 2x twice: sell 50% = 500k
        [_trade(side="sell", pct_sold=40, remaining=1)],     # partial: ignored
        [_trade(side="sell", pct_sold=100, remaining=0)],    # full exit: the other 500k
    ], x, live_trim_cap_pct=50.0, live_copy_trims=False)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 500_000), ("sell", "M1", 500_000)]
    assert pos[0].status == "closed" and pos[0].exit_reason == "leader_sold"
    assert sum("waits for its full exit" in n for n in notes) == 2


def test_users_rule_full_exit_before_2x_sells_everything():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],
        [_trade(side="sell", pct_sold=100, remaining=0)],
    ], x, live_trim_cap_pct=50.0, live_copy_trims=False)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 1_000_000)]


def test_users_rule_is_the_default():
    from app.config import Settings

    s = Settings()
    assert s.live_trim_cap_pct == 50.0 and s.live_copy_trims is False



RULE = dict(live_trim_cap_pct=50.0, live_copy_trims=False, live_follow_after_trim=True)


def test_rule_no_partial_sell_first_half_at_2x_then_full_exit():
    x = PricedExec()
    pos, x, notes = _run_priced([
        [_trade()],                                          # 1M for $30
        lambda: x.price.update(M1=60.0), [],                 # 2x: sell 500k
        [_trade(side="sell", pct_sold=30, remaining=1)],     # after the half: ignored
        [_trade(side="sell", pct_sold=100, remaining=0)],    # full exit: the other 500k
    ], x, **RULE)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 500_000), ("sell", "M1", 500_000)]
    assert any("half was already sold at 2x" in n for n in notes)


def test_rule_partial_sell_first_then_follow_the_leader_all_the_way():
    x = PricedExec()
    pos, x, _ = _run_priced([
        [_trade()],                                          # 1M
        [_trade(side="sell", pct_sold=30, remaining=1)],     # leader trims first: copy 300k -> following
        lambda: x.price.update(M1=200.0), [], [],            # way past 2x: no 50% sale while following
        [_trade(side="sell", pct_sold=50, remaining=1)],     # copy: 50% of 700k = 350k
        [_trade(side="sell", pct_sold=100, remaining=0)],    # full exit: the last 350k
    ], x, **RULE)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 300_000), ("sell", "M1", 350_000),
                                                       ("sell", "M1", 350_000)]
    assert pos[0].status == "closed" and pos[0].exit_reason == "leader_sold"


def test_rule_is_the_default():
    from app.config import Settings

    s = Settings()
    assert (s.live_trim_cap_pct, s.live_copy_trims, s.live_follow_after_trim) == (50.0, False, True)


def test_blocked_tokens_are_never_bought():
    s = _s(live_blocked_mints="B, C")
    x = FakeExec()

    async def body(sess):
        eng = CopyEngine(x, s)
        await eng.step(sess)
        sess.add_all([_trade(mint="A"), _trade(mint="B")])
        await sess.commit()
        await eng.step(sess)
        return [c[1] for c in x.calls if c[0] == "buy"]

    assert _db(body) == ["A"]
    assert {"pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"} <= Settings().blocked_mints()


def _run_quick(steps, x, quick=True, **settings):
    from app.live import Account, Leader

    acct = Account("main", 30, 100, 50, [LEADER], leaders={LEADER: Leader(LEADER, unconditional=True, quick_exit=quick)})

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0, **settings),
                       account=acct)
        await eng.step(sess)
        for batch in steps:
            if callable(batch):
                batch()
            else:
                sess.add_all(batch)
                await sess.commit()
            await eng.step(sess)
        return (await sess.execute(select(LivePosition).order_by(LivePosition.id))).scalars().all()

    return _db(body)


def test_quick_exit_take_profit_sells_all_and_skips_the_2x_rule():
    x = PricedExec()
    pos = _run_quick([
        [_trade()],                                   # $30 for 1 token
        lambda: x.price.update(M1=44.0),              # +47%: nothing
        lambda: x.price.update(M1=46.0),              # +53%: first reading
        [],                                           # second reading: sell everything
    ], x)
    sells = [c for c in x.calls if c[0] == "sell"]
    assert sells == [("sell", "M1", 1_000_000)]
    assert pos[0].status == "closed" and pos[0].exit_reason == "take_profit"


def test_quick_exit_stop_loss_and_first_leader_trim_sell_all():
    x = PricedExec()
    pos = _run_quick([
        [_trade()],
        lambda: x.price.update(M1=20.0),              # -33%: far past -25%, one reading is enough
    ], x)
    assert pos[0].status == "closed" and pos[0].exit_reason == "stop_loss"

    x2 = PricedExec()
    pos2 = _run_quick([
        [_trade(mint="M2")],
        [_trade(side="sell", mint="M2", pct_sold=20, remaining=8)],   # a 20% trim: quick exit sells it all
    ], x2)
    assert [c for c in x2.calls if c[0] == "sell"] == [("sell", "M2", 1_000_000)]
    assert pos2[0].status == "closed" and pos2[0].exit_reason == "leader_sold"


def test_quick_exit_off_keeps_the_2x_rule():
    x = PricedExec()
    pos = _run_quick([
        [_trade()],
        lambda: x.price.update(M1=46.0), [],          # +53% with the switch off: nothing
        lambda: x.price.update(M1=20.0), [],          # -33%: nothing either (no stop loss in the user's rule)
    ], x, quick=False)
    assert [c for c in x.calls if c[0] == "sell"] == [] and pos[0].status == "open"


def test_quick_exit_time_limit():
    x = PricedExec()

    async def body(sess):
        from app.live import Account, Leader
        acct = Account("main", 30, 100, 50, [LEADER], leaders={LEADER: Leader(LEADER, unconditional=True, quick_exit=True)})
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0),
                        account=acct)
        await eng.step(sess)
        sess.add(_trade())
        await sess.commit()
        await eng.step(sess)
        p = (await sess.execute(select(LivePosition))).scalars().one()
        p.opened_at = datetime.now(timezone.utc) - timedelta(minutes=361)
        await sess.commit()
        await eng.step(sess)
        await sess.refresh(p)
        return p

    p = _db(body)
    assert p.status == "closed" and p.exit_reason == "quick_time"


def test_quick_exit_switch_is_saved_with_the_leaders():
    from app.accounts import clean_leaders

    out = clean_leaders([{"wallet": LEADER, "quick_exit": True}, {"wallet": "5hqZDJQidTDmjCrLLYi1NiwNUuCZYFc4vo9bPPMr3w85"}])
    assert [d["quick_exit"] for d in out] == [True, False]


def test_quick_exit_uses_jupiter_price_when_dexscreener_lags():
    class LaggingExec(PricedExec):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.fast = {}

        async def token_prices_fast(self, mints):
            return {m: {"usd": self.fast[m], "decimals": 6} for m in mints if m in self.fast}

    x = LaggingExec()
    pos = _run_quick([
        [_trade()],
        lambda: (x.price.update(M1=34.0), x.fast.update(M1=60.0)),   # DexScreener +13%, Jupiter +100%: far past
    ], x)
    assert pos[0].status == "closed" and pos[0].exit_reason == "take_profit"


def test_buy_slippage_never_carries_the_fill_past_the_chase_limit():
    from app.live import JupiterExecutor

    seen = []

    async def go():
        x = JupiterExecutor(Settings(jupiter_api_url="https://jup.test", live_rpc_url="https://rpc.test/"), live=False)

        async def sol_price():
            return 100.0

        async def decimals(mint):
            return 0

        async def quote(a, b, lamports, bps=None):
            seen.append(bps)
            return {"outAmount": "100", "inAmount": str(lamports)}    # $30 -> 100 tokens = $0.30 each

        x.sol_price, x.decimals, x.quote = sol_price, decimals, quote
        try:
            return await x.buy("MINT", 30.0, max_price=0.33, slippage_bps=2_500)   # 10% room, 25% asked
        finally:
            await x.close()

    asyncio.run(go())
    assert seen == [2_500, 1_000]      # re-quoted with the slippage capped at the 10% left before the limit


def test_buy_keeps_its_timing():
    x = FakeExec()

    async def body(sess):
        eng = CopyEngine(x, _s())
        await eng.step(sess)
        sess.add(_trade())
        await sess.commit()
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().one()

    p = _db(body)
    t = p.fills[0]["timing"]
    assert {"pickup", "checks", "buy_call", "block_to_bought"} <= set(t) and t["block_to_bought"] >= 0


def _direct_exec(sim=(0, 0, None), pp_status=200, **settings):
    """JupiterExecutor in live mode with PumpPortal, the RPC and Jupiter stubbed (nothing leaves the test).
    Best route is off unless asked for: these tests are about the PumpPortal path itself."""
    import httpx as hx
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction

    from app.live import JupiterExecutor

    kp = Keypair()
    calls = {"quote": 0, "sent": 0, "pp": 0}
    settings = {"live_best_route": False, "live_best_route_sells": False, **settings}
    x = JupiterExecutor(Settings(jupiter_api_url="https://jup.test", live_rpc_url="https://rpc.test/",
                                 live_direct_pump_buys=True, **settings), live=True, private_key=str(kp))
    unsigned = bytes(VersionedTransaction(MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()), [kp]))
    state = {"tok": 0, "sol": 2.0}

    async def sol_price():
        return 100.0

    async def decimals(mint):
        return 6

    async def token_balance(mint):
        return state["tok"]

    async def sol_balance():
        return state["sol"]

    async def quote(a, b, lamports, bps=None):
        calls["quote"] += 1
        return {"outAmount": "1000000", "inAmount": str(lamports)}

    sims = list(sim) if isinstance(sim, list) else None

    async def simulate(raw, mint):
        return sims.pop(0) if sims else sim

    async def post(url, **kw):
        calls["pp"] += 1
        calls.setdefault("slips", []).append(kw.get("data", {}).get("slippage"))
        return hx.Response(pp_status, content=unsigned, headers={"content-type": "application/octet-stream"})

    async def send_tx(tx_bytes, last_valid=None, signed_b64=None):
        calls["sent"] += 1
        last = sim[-1] if isinstance(sim, list) else sim
        state["tok"] += last[0]
        state["sol"] = last[1] / 1e9
        return "SIGD", True

    x.sol_price, x.decimals, x.quote, x.token_balance, x.sol_balance = sol_price, decimals, quote, token_balance, sol_balance
    x._simulate_buy, x.http.post, x._send_tx = simulate, post, send_tx
    return x, calls


MINT_PUMP = "ByCUjGx4FkhK7ac8Vdqzb41yScUtvG78GBYSXKsMpump"


def test_direct_pump_buy_uses_pumpportal_and_the_simulated_price():
    async def go():
        # simulation: +100 tokens (6 decimals: 100e6 raw), SOL 2.0 -> 1.7 (0.3 SOL = $30)
        x, calls = _direct_exec(sim=(100_000_000, int(1.7e9), None))
        try:
            f = await x.buy(MINT_PUMP, 30.0, max_price=0.31)             # pays $0.30 per token: under the limit
        finally:
            await x.close()
        return f, calls

    f, calls = asyncio.run(go())
    assert f.signature == "SIGD" and f.timing["route"] == "pumpportal"
    assert (calls["quote"], calls["sent"], calls["pp"]) == (0, 1, 1) and f.tokens_raw == 100_000_000


def test_direct_buy_chase_skips_and_failures_fall_back_to_jupiter():
    from app.live import ChaseError

    async def chase():
        x, calls = _direct_exec(sim=(100_000_000, int(1.7e9), None))
        try:
            await x.buy(MINT_PUMP, 30.0, max_price=0.25)                 # would pay $0.30: skip, nothing sent
        finally:
            await x.close()

    try:
        asyncio.run(chase())
        raise AssertionError("expected ChaseError")
    except ChaseError:
        pass

    async def failing_sim():
        x, calls = _direct_exec(sim=(0, 0, {"InstructionError": [3, {"Custom": 6004}]}))
        x._buy_once = None    # set below

        async def jup(mint, usd, max_price=None, bps=None):
            calls["quote"] += 1
            return Fill("SIGJ", 0.3, 5, 100.0)

        x._buy_once = jup
        try:
            f = await x.buy(MINT_PUMP, 30.0, max_price=1.0)
        finally:
            await x.close()
        return f, calls

    f, calls = asyncio.run(failing_sim())
    assert f.signature == "SIGJ" and f.timing["route"] == "jupiter" and calls["sent"] == 0


def test_housekeeping_yields_to_a_new_copied_trade():
    x = PricedExec()
    seen = []

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0))
        eng.wake = asyncio.Event()

        async def spy(session):
            seen.append("take_capital")
        eng._take_capital = spy
        await eng.step(sess)                      # first run: only sets the cursor
        await eng.step(sess)                      # nothing pending: housekeeping runs
        eng.wake.set()                            # a copied wallet just traded
        await eng.step(sess)                      # housekeeping is skipped this time
        return seen

    assert _db(body) == ["take_capital"]


def test_no_buy_when_the_leader_already_sold_it():
    x = FakeExec()

    async def body(sess):
        eng = CopyEngine(x, _s())
        await eng.step(sess)
        sess.add_all([_trade(mint="FLIP"), _trade(side="sell", mint="FLIP", pct_sold=100, remaining=0)])
        await sess.commit()
        await eng.step(sess)                  # both rows are in when we look: it is already out

    _db(body)
    assert x.calls == []


def test_ignore_sells_keeps_the_position_and_quick_exit_still_takes_profit():
    from app.live import Account, Leader

    x = PricedExec()
    acct = Account("main", 30, 100, 50, [LEADER],
                   leaders={LEADER: Leader(LEADER, unconditional=True, quick_exit=True, ignore_sells=True)})

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0),
                         account=acct)
        await eng.step(sess)
        sess.add(_trade())
        await sess.commit()
        await eng.step(sess)
        sess.add(_trade(side="sell", pct_sold=100, remaining=0))     # it sells everything: we stay in
        await sess.commit()
        await eng.step(sess)
        before = [c for c in x.calls if c[0] == "sell"]
        x.price.update(M1=60.0)                                       # +100%: far past +50%
        await eng.step(sess)
        p = (await sess.execute(select(LivePosition))).scalars().one()
        return before, p

    before, p = _db(body)
    assert before == [] and p.status == "closed" and p.exit_reason == "take_profit"


def test_full_exit_of_a_held_token_sends_an_alert_partial_does_not():
    pos, x, notes = _run([
        [_trade()],
        [_trade(side="sell", pct_sold=40, remaining=6)],          # a trim: no exit alert
        [_trade(side="sell", pct_sold=100, remaining=0)],         # everything: alert, then the bot sells
    ])
    alerts = [n for n in notes if "🚪" in n]
    assert len(alerts) == 1 and "sold ALL" in alerts[0] and "the bot sells its position now" in alerts[0]
    assert notes.index(alerts[0]) < max(i for i, n in enumerate(notes) if "💰 Sold" in n)   # alert first


def test_exit_alert_kind_is_its_own_switch():
    from app.tg_prefs import live_kind
    assert live_kind("🟢 LIVE 🚪 DODY sold ALL its $X") == "leader_exits"


def test_background_jupiter_calls_never_retry_and_back_off_after_a_429():
    import httpx as hx

    from app.live import JupiterGate

    calls = []

    def handler(req):
        calls.append(req.url.path)
        return hx.Response(429)

    async def go():
        gate = JupiterGate(interval=0.0)
        async with hx.AsyncClient(transport=hx.MockTransport(handler)) as c:
            r1 = await gate.request(c, "GET", "https://jup.test/price/v3", background=True)
            r2 = await gate.request(c, "GET", "https://jup.test/price/v3", background=True)   # cooling down: skipped
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1.status_code == 429 and r2 is None and len(calls) == 1



def test_direct_buy_retries_once_with_more_slippage_on_6004_and_skips_unsupported_tokens():
    from app import live as L

    async def retry():
        x, calls = _direct_exec(sim=[(0, 0, {"InstructionError": [3, {"Custom": 6004}]}),
                                     (100_000_000, int(1.7e9), None)])
        try:
            f = await x.buy(MINT_PUMP, 30.0, max_price=0.31)
        finally:
            await x.close()
        return f, calls

    f, calls = asyncio.run(retry())
    assert f.timing["route"] == "pumpportal" and calls["sent"] == 1 and calls["slips"] == ["15.00", "40.00"]

    L._NO_DIRECT.clear()

    async def unsupported():
        x, calls = _direct_exec(sim=(0, 0, {"InstructionError": [3, {"Custom": 6063}]}))

        async def jup(mint, usd, max_price=None, bps=None):
            calls["quote"] += 1
            return Fill("SIGJ", 0.3, 5, 100.0)
        x._buy_once = jup
        try:
            await x.buy(MINT_PUMP, 30.0, max_price=1.0)
            await x.buy(MINT_PUMP, 30.0, max_price=1.0)          # known now: straight to Jupiter
        finally:
            await x.close()
        return calls

    calls = asyncio.run(unsupported())
    assert calls["pp"] == 1 and calls["quote"] == 2
    L._NO_DIRECT.clear()


def test_quick_exit_uses_the_wallets_own_targets():
    from app.accounts import AccountError, clean_leaders
    from app.live import Account, Leader

    x = PricedExec()
    acct = Account("main", 30, 100, 50, [LEADER], leaders={LEADER: Leader(
        LEADER, unconditional=True, quick_exit=True, quick_tp_pct=20.0, quick_sl_pct=-10.0, quick_hours=1.0)})

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0),
                         account=acct)
        await eng.step(sess)
        sess.add(_trade())
        await sess.commit()
        await eng.step(sess)
        x.price.update(M1=40.0)                     # +33%: past its own +20% (default 50% would hold)
        await eng.step(sess)
        return (await sess.execute(select(LivePosition))).scalars().one()

    p = _db(body)
    assert p.status == "closed" and p.exit_reason == "take_profit"
    out = clean_leaders([{"wallet": LEADER, "quick_exit": True, "quick_tp_pct": "30", "quick_sl_pct": -15, "quick_hours": 2}])
    assert (out[0]["quick_tp_pct"], out[0]["quick_sl_pct"], out[0]["quick_hours"]) == (30.0, -15.0, 2.0)
    try:
        clean_leaders([{"wallet": LEADER, "quick_sl_pct": -150}])   # a stop loss can't lose more than everything
        raise AssertionError("expected AccountError")
    except AccountError:
        pass


def test_stop_loss_typed_as_a_plain_number_is_saved_negative():
    from app.accounts import clean_leaders
    out = clean_leaders([{"wallet": LEADER, "quick_exit": True, "quick_sl_pct": 15}])
    assert out[0]["quick_sl_pct"] == -15.0


def test_direct_buy_that_would_overspend_is_rebuilt_smaller():
    async def go():
        # first simulation spends 0.4 SOL ($40) for a $30 buy; the resized one 0.3 SOL
        x, calls = _direct_exec(sim=[(100_000_000, int(1.6e9), None), (75_000_000, int(1.7e9), None)])
        amounts = []
        orig = x.http.post

        async def post(url, **kw):
            amounts.append(float(kw["data"]["amount"]))
            return await orig(url, **kw)
        x.http.post = post
        try:
            f = await x.buy(MINT_PUMP, 30.0, max_price=1.0)
        finally:
            await x.close()
        return f, calls, amounts

    f, calls, amounts = asyncio.run(go())
    assert f.timing["route"] == "pumpportal" and calls["sent"] == 1
    assert amounts[0] == 0.3 and amounts[1] < 0.23            # asked for less SOL the second time


def test_owner_target_sells_its_share_then_stop_takes_the_rest():
    from app.live import set_position_target

    x = PricedExec()

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0,
                               live_take_capital_at_pct=None))
        await eng.step(sess)
        sess.add(_trade())
        await sess.commit()
        await eng.step(sess)                                      # bought: $30 for 1 token
        p = (await sess.execute(select(LivePosition))).scalars().one()
        ok, _ = await set_position_target(sess, p.id, 50.0, 0.5, 20)   # sell half at +50%, stop at -20%
        assert ok and p.stop_pct == -20.0
        x.price.update(M1=46.0)                                   # +53%: first reading
        await eng.step(sess)
        await eng.step(sess)                                      # second reading: sell 50%
        await sess.refresh(p)
        half = [c for c in x.calls if c[0] == "sell"]
        assert half == [("sell", "M1", 500_000)] and p.target_pct is None and p.stop_pct == -20.0
        x.price.update(M1=20.0)                                   # price -33% from our entry ($30)
        await eng.step(sess)
        await eng.step(sess)
        await sess.refresh(p)
        return p

    p = _db(body)
    assert p.status == "closed" and p.exit_reason == "your_stop"


def test_price_target_hands_off_ignores_the_leader_and_sells_at_the_owners_prices():
    from app.live import set_position_target

    x = PricedExec()

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0,
                               live_take_capital_at_pct=None))
        await eng.step(sess)
        sess.add(_trade())
        await sess.commit()
        await eng.step(sess)                                      # bought: $30 for 1 token
        p = (await sess.execute(select(LivePosition))).scalars().one()
        bad, _ = await set_position_target(sess, p.id, None, 1.0, None, target_price=20.0, stop_price=25.0)
        assert not bad                                            # stop above target
        ok, note = await set_position_target(sess, p.id, None, 0.5, None, target_price=40.0, stop_price=24.0,
                                             hands_off=True)
        assert ok and "$40" in note and p.hands_off and p.target_pct is None
        sess.add(_trade(side="sell", pct_sold=100, remaining=0))  # the leader sells everything: ignored
        await sess.commit()
        await eng.step(sess)
        await sess.refresh(p)
        assert p.status == "open" and not [c for c in x.calls if c[0] == "sell"]
        x.price.update(M1=41.0)                                   # past $40: two readings, sell half
        await eng.step(sess)
        await eng.step(sess)
        await sess.refresh(p)
        assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 500_000)]
        assert p.target_price is None and p.stop_price == 24.0
        x.price.update(M1=23.0)                                   # at/below $24: the rest goes
        await eng.step(sess)
        await eng.step(sess)
        await sess.refresh(p)
        return p

    p = _db(body)
    assert p.status == "closed" and p.exit_reason == "your_stop"


def test_target_on_a_coin_the_bot_does_not_hold_sells_it_at_the_owners_price():
    from app.live import MANUAL, hold_with_target

    x = PricedExec()
    x.balances["M1"] = 1_000_000                                  # bought elsewhere: 1 token in the wallet

    async def body(sess):
        eng = CopyEngine(x, _s(live_reconcile_seconds=0, live_take_capital_check_seconds=0, live_quick_check_seconds=0,
                               live_take_capital_at_pct=None))
        await eng.step(sess)
        bad, _ = await hold_with_target(sess, "main", x.mode, "M1", 1_000_000, "M1", 30.0, 1.0, None, None)
        assert not bad                                            # no price given
        bad, _ = await hold_with_target(sess, "main", x.mode, "M1", 1_000_000, "M1", None, 1.0, 40.0, None)
        assert not bad                                            # no value: can't be watched
        ok, note = await hold_with_target(sess, "main", x.mode, "M1", 1_000_000, "M1", 30.0, 1.0, 40.0, 20.0)
        assert ok, note
        p = (await sess.execute(select(LivePosition))).scalars().one()
        assert p.leader_wallet == MANUAL and p.hands_off and p.target_price == 40.0 and p.stop_price == 20.0
        again, _ = await hold_with_target(sess, "main", x.mode, "M1", 1_000_000, "M1", 30.0, 0.5, 45.0, None)
        assert again and len((await sess.execute(select(LivePosition))).scalars().all()) == 1   # same position
        x.price.update(M1=46.0)
        await eng.step(sess)
        await eng.step(sess)
        await sess.refresh(p)
        return p

    p = _db(body)
    assert [c for c in x.calls if c[0] == "sell"] == [("sell", "M1", 500_000)]
    assert p.status == "open" and p.target_price is None


# ---- best route: PumpPortal vs Jupiter after fees (user's request 2026-11-23) --------------------------------
def test_route_edge_math():
    from app.live import route_edge_buy, route_edge_sell

    assert abs(route_edge_buy(100, 1.0, 110, 1.0) - 0.10) < 1e-12          # Jupiter 10% more tokens per SOL
    assert abs(route_edge_buy(100, 1.0, 100, 1.05) + 0.0476) < 1e-3        # same tokens, Jupiter costs more
    assert abs(route_edge_sell(0.50, 0.49) + 0.02) < 1e-12                  # PumpPortal 2% more SOL
    assert route_edge_sell(0.0, 0.1) == 1.0


def _best_exec(jup_tokens, quote_delay=0.0, **kw):
    """Live executor with both routes stubbed: PumpPortal simulates +100 tokens for 0.3 SOL (all-in),
    Jupiter quotes `jup_tokens` for the same SOL."""
    x, calls = _direct_exec(sim=(100_000_000, int(1.7e9), None), live_best_route=True, **kw)

    async def quote(a, b, lamports, bps=None):
        calls["quote"] += 1
        if quote_delay:
            await asyncio.sleep(quote_delay)
        return {"outAmount": str(jup_tokens), "inAmount": str(lamports)}

    async def send_jup(q):
        calls["jup_sent"] = calls.get("jup_sent", 0) + 1
        x._test_state_tokens = int(q["outAmount"])
        return "SIGJ", True

    x.quote, x._send = quote, send_jup
    bal = x.token_balance

    async def token_balance(mint):
        return getattr(x, "_test_state_tokens", None) or await bal(mint)

    x.token_balance = token_balance
    return x, calls


def test_best_route_buys_on_jupiter_when_it_gives_more_tokens_after_fees():
    async def go():
        x, calls = _best_exec(110_000_000)       # ~9% more tokens per SOL even after fee + rent
        try:
            return await x.buy(MINT_PUMP, 30.0), calls
        finally:
            await x.close()

    f, calls = asyncio.run(go())
    assert f.timing["route"] == "jupiter" and f.signature == "SIGJ"
    assert calls["sent"] == 0 and calls["jup_sent"] == 1 and calls["quote"] == 1   # the winning quote is reused


def test_best_route_keeps_pumpportal_when_jupiter_is_only_marginally_better_or_slow():
    async def near():
        x, calls = _best_exec(100_900_000)       # +0.9% raw, below +0.3% once Jupiter's fee + rent count
        try:
            return await x.buy(MINT_PUMP, 30.0), calls
        finally:
            await x.close()

    f, calls = asyncio.run(near())
    assert f.timing["route"] == "pumpportal" and calls["sent"] == 1 and "jup_sent" not in calls
    assert "route_edge_pct" in f.timing

    async def slow():
        x, calls = _best_exec(200_000_000, quote_delay=3.0, live_best_route_wait_seconds=0.2)
        t0 = asyncio.get_running_loop().time()
        try:
            f = await x.buy(MINT_PUMP, 30.0)
        finally:
            await x.close()
        return f, asyncio.get_running_loop().time() - t0

    f, took = asyncio.run(slow())
    assert f.timing["route"] == "pumpportal" and took < 2.0     # speed first: no waiting for a slow quote


def _sell_exec(pp_sol_out, jup_lamports_out):
    """Holding 100 tokens; PumpPortal's simulated sell brings `pp_sol_out` SOL, Jupiter quotes `jup_lamports_out`."""
    x, calls = _direct_exec(live_best_route_sells=True)
    state = {"tok": 100_000_000, "sol": 2.0}

    async def token_balance(mint):
        return state["tok"]

    async def sol_balance():
        return state["sol"]

    async def simulate(raw, mint):
        return 0, int((2.0 + pp_sol_out) * 1e9), None

    async def quote(a, b, amount, bps=None):
        calls["quote"] += 1
        return {"outAmount": str(jup_lamports_out), "inAmount": str(amount)}

    async def send_pp(tx_bytes, last_valid=None, signed_b64=None):
        calls["sent"] += 1
        state["tok"], state["sol"] = 0, 2.0 + pp_sol_out
        return "SIGPP", True

    async def send_jup(q):
        calls["jup_sent"] = calls.get("jup_sent", 0) + 1
        state["tok"], state["sol"] = 0, 2.0 + int(q["outAmount"]) / 1e9
        return "SIGJ", True

    x.token_balance, x.sol_balance, x._simulate_buy, x.quote = token_balance, sol_balance, simulate, quote
    x._send_tx, x._send = send_pp, send_jup
    return x, calls


def test_best_route_sells_where_it_pays_more_after_fees():
    from app.live import SOL_MINT  # noqa: F401  (route chosen per sell)

    async def run(pp, jup):
        x, calls = _sell_exec(pp, jup)
        try:
            return await x.sell(MINT_PUMP, 100_000_000), calls
        finally:
            await x.close()

    f, calls = asyncio.run(run(0.50, 490_000_000))          # PumpPortal 0.50 vs Jupiter 0.4895 after its fee
    assert f.signature == "SIGPP" and abs(f.sol - 0.50) < 1e-9 and "jup_sent" not in calls
    f, calls = asyncio.run(run(0.50, 520_000_000))          # Jupiter 0.5195 after fee: 3.9% more
    assert f.signature == "SIGJ" and calls["sent"] == 0 and calls["jup_sent"] == 1


def test_best_route_sell_falls_back_to_jupiter_when_pumpportal_cannot():
    import httpx as hx

    async def go():
        x, calls = _sell_exec(0.50, 480_000_000)

        async def post(url, **kw):
            calls["pp"] += 1
            return hx.Response(400, content=b'{"error":"bad pool"}', headers={"content-type": "application/json"})

        x.http.post = post
        try:
            return await x.sell(MINT_PUMP, 100_000_000), calls
        finally:
            await x.close()

    f, calls = asyncio.run(go())
    assert f.signature == "SIGJ" and calls["pp"] == 1 and calls["sent"] == 0


def test_account_with_no_copied_wallets_still_gets_an_engine():
    """2026-11-23: a coin sold in Phantom from an account with no leaders was never booked: its engine (which
    books hand sales, runs targets / stops and the page's Sell button) was never started."""
    from solders.keypair import Keypair

    from app.live import Account, LiveTrader

    started = []

    async def go():
        t = LiveTrader(Settings(live_fast_detection=False, live_screen_enabled=False))
        t._screener = None

        async def fake_run(a, x, mode, eng):
            started.append(a.name)
            await asyncio.sleep(3600)

        t._run_account = fake_run

        async def no_telegram(text):
            return None

        t._telegram = no_telegram
        running, said = {}, set()
        accts = [Account("wallet", 20, 100, 50, [], str(Keypair())), Account("main", 20, 100, 50, ["L1"], str(Keypair()))]
        await t._sync(accts, "live", running, said, None)
        await asyncio.sleep(0)
        names = sorted(running)
        await t._sync(accts, "live", running, said, None)      # a second pass keeps it running
        names2 = sorted(running)
        for task, _, _ in running.values():
            task.cancel()
        return names, names2

    names, names2 = asyncio.run(go())
    assert names == ["main", "wallet"] and names2 == ["main", "wallet"] and sorted(started) == ["main", "wallet"]


def test_decimals_of_a_brand_new_coin_not_finalized_yet():
    """getTokenSupply at "confirmed" (a coin made seconds ago isn't finalized); a pump coin falls back to 6."""
    import asyncio

    import httpx
    import respx

    import app.live as live_mod
    from app.live import JupiterExecutor

    seen = []

    def rpc(request):
        import json as _json
        body = _json.loads(request.content)
        seen.append(body["params"])
        if body["params"][0] == "GOODmint":
            return httpx.Response(200, json={"result": {"value": {"decimals": 9, "amount": "1"}}})
        return httpx.Response(200, json={"error": {"code": -32602, "message": "Invalid param: could not find mint"}})

    async def go():
        x = JupiterExecutor(Settings(jupiter_api_url="https://jup.test", live_rpc_url="https://rpc.test/"), live=False)
        try:
            out = [await x.decimals("GOODmint"), await x.decimals("NEWcoinpump")]
            try:
                await x.decimals("NEWother")
                out.append("no error")
            except Exception:
                out.append("error")
            return out
        finally:
            await x.close()

    live_mod._DECIMALS.clear()
    with respx.mock:
        respx.post("https://rpc.test/").mock(side_effect=rpc)
        got = asyncio.run(go())
    assert got == [9, 6, "error"]
    assert seen[0] == ["GOODmint", {"commitment": "confirmed"}]
    assert "NEWcoinpump" not in live_mod._DECIMALS        # not cached: the real answer is read once it lands


def test_token_balance_of_a_mint_the_rpc_has_not_seen_yet_is_zero():
    import asyncio

    import httpx
    import respx

    from app.live import JupiterExecutor

    async def go():
        x = JupiterExecutor(Settings(jupiter_api_url="https://jup.test", live_rpc_url="https://rpc.test/"), live=False)
        x.pubkey = "Owner1111111111111111111111111111111111111"
        try:
            return await x.token_balance("NEWcoinpump")
        finally:
            await x.close()

    with respx.mock:
        respx.post("https://rpc.test/").mock(return_value=httpx.Response(200, json={"error": {
            "code": -32602, "message": "Error getting token program id and mint: Invalid param: could not find mint"}}))
        assert asyncio.run(go()) == 0


def test_sender_tip_is_added_keeping_every_instruction_and_lookup_table():
    import base64 as b64

    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    from app.live import SENDER_TIP_ACCOUNTS, sender_url, with_tip

    kp = Keypair()
    prog, w1, r1 = Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    table = Pubkey.new_unique()
    ix = Instruction(prog, b"\x01\x02", [AccountMeta(kp.pubkey(), True, True), AccountMeta(w1, False, True),
                                         AccountMeta(r1, False, False)])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [AddressLookupTableAccount(table, [w1, r1])], Hash.default())
    unsigned = bytes(VersionedTransaction.populate(msg, [kp.sign_message(bytes([0]))]))
    assert with_tip(unsigned, kp, 50_000, tables={}) is None                     # table not loaded: old way
    raw = with_tip(unsigned, kp, 50_000, tables={str(table): [w1, r1]})
    tx = VersionedTransaction.from_bytes(b64.b64decode(raw))
    m = tx.message
    assert len(m.instructions) == 2 and len(m.address_table_lookups) == 1
    keys = list(m.account_keys)
    lk = m.address_table_lookups[0]
    loaded_w = [[w1, r1][i] for i in bytes(lk.writable_indexes)]
    loaded_r = [[w1, r1][i] for i in bytes(lk.readonly_indexes)]
    full = keys + loaded_w + loaded_r
    first = [full[i] for i in bytes(m.instructions[0].accounts)]
    assert first == [kp.pubkey(), w1, r1] and bytes(m.instructions[0].data) == b"\x01\x02"
    assert w1 in loaded_w and r1 in loaded_r                                     # writability kept
    tip = full[bytes(m.instructions[1].accounts)[1]]
    assert str(tip) in SENDER_TIP_ACCOUNTS
    assert tx.verify_with_results() == [True]                                     # signed by us

    s = Settings(live_rpc_url="https://mainnet.helius-rpc.com/?api-key=K1")
    assert sender_url(s) == "https://sender.helius-rpc.com/fast?swqos_only=true&api-key=K1"
    assert sender_url(Settings(live_sender=False)) is None


def test_a_tipped_transaction_goes_through_sender_and_the_rpc():
    import asyncio
    import base64 as b64

    import httpx
    import respx
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    from app.live import JupiterExecutor

    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(),
                                                                      lamports=1))], [], Hash.default())
    signed = b64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode()
    sig = str(VersionedTransaction(msg, [kp]).signatures[0])
    hits = {"sender": 0, "rpc_send": 0}

    def rpc(request):
        import json as _json
        m = _json.loads(request.content)["method"]
        if m == "sendTransaction":
            hits["rpc_send"] += 1
            return httpx.Response(200, json={"error": {"code": -32002, "message": "node is behind"}})
        if m == "getSignatureStatuses":
            return httpx.Response(200, json={"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
        return httpx.Response(200, json={"result": None})

    def sender(request):
        hits["sender"] += 1
        return httpx.Response(200, json={"result": sig})

    async def go():
        x = JupiterExecutor(Settings(live_rpc_url="https://rpc.test/?api-key=K"), live=True, private_key=str(kp))
        x._tipped.add(signed)
        try:
            return await x._send_tx(bytes(VersionedTransaction(msg, [kp])), signed_b64=signed)
        finally:
            await x.close()

    with respx.mock:
        respx.post(url__startswith="https://rpc.test/").mock(side_effect=rpc)
        respx.post(url__startswith="https://sender.helius-rpc.com/fast").mock(side_effect=sender)
        got = asyncio.run(go())
    # the RPC refused the send but Sender took it: it is followed like any send, and lands
    assert got == (sig, True) and hits["sender"] == 1 and hits["rpc_send"] == 1
