"""Wallet scout: USD round trips, the strict filter, the daily run, paper trials and their verdict."""
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import (Base, Chain, LivePosition, PaperPosition, SmartWallet, WalletCandidate, WalletSource,
                        WalletTrade)
from app.paper import process_entries, process_exits
from app.wallet_scout import TRIAL_STRATEGY, WalletScout, checks, judge, summarize

W = "5hqZDJQidTDmjCrLLYi1NiwNUuCZYFc4vo9bPPMr3w85"     # a real (on-curve) address for the candidate
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T0 = 1_795_000_000


def _swap(ts, mint, tokens, sol=None, usdc=None, wallet=W):
    """A Helius-parsed swap: +tokens = buy. Paid in SOL (native) or USDC."""
    tt = [{"mint": mint, "tokenAmount": abs(tokens),
           "toUserAccount": wallet if tokens > 0 else "pool", "fromUserAccount": "pool" if tokens > 0 else wallet}]
    acct = []
    if sol is not None:
        acct = [{"account": wallet, "nativeBalanceChange": int(sol * 1e9)}]
    if usdc is not None:
        tt.append({"mint": USDC, "tokenAmount": abs(usdc),
                   "toUserAccount": wallet if usdc > 0 else "pool", "fromUserAccount": "pool" if usdc > 0 else wallet})
    return {"timestamp": ts, "signature": f"s{ts}{mint}", "accountData": acct, "tokenTransfers": tt}


def _history(n_tokens=20, win_every=2, days=4.0):
    """n round trips over `days`: every `win_every`-th +60%, the rest -20%, held 30 min; $100 in USDC or SOL."""
    txs = []
    step = days * 86400 / n_tokens
    for i in range(n_tokens):
        t = T0 + int(i * step)
        mint = f"M{i}"
        win = i % win_every == 0
        if i % 2:
            txs += [_swap(t, mint, 1000, sol=-1.0), _swap(t + 1800, mint, -1000, sol=1.6 if win else 0.8)]
        else:
            txs += [_swap(t, mint, 1000, usdc=-100), _swap(t + 1800, mint, -1000, usdc=160 if win else 80)]
    return sorted(txs, key=lambda x: -x["timestamp"])


def S(**kw):
    base = dict(scout_enabled=True, scout_min_hits=2, scout_max_trials=4, scout_trial_days=3.0, scout_min_days=3.0,
                scout_max_swaps_per_day=150, scout_min_closed=15, scout_min_profit_factor=1.3, scout_min_win_rate=40,
                scout_min_hold_minutes=5, scout_trial_min_trades=2, live_rpc_url="https://rpc.test/?api-key=k",
                paper_size_usd=100.0, paper_slippage_pct=1.5, scout_max_idle_hours=1e9)
    base.update(kw)
    return Settings(**base)


def test_round_trips_in_usd_from_sol_and_usdc():
    m = summarize(_history(), W, sol_usd=100.0)
    assert m["closed"] == 20 and m["win_rate"] == 50.0
    assert abs(m["net_usd"] - (10 * 60 - 10 * 20)) < 1e-6         # +$60 x10, -$20 x10
    assert m["hold_min"] == 30.0 and 3.7 < m["days"] < 4.1


def test_strict_filter():
    s = S()
    ok, why = judge(summarize(_history(), W, 100.0), s)
    assert ok, why
    # one huge winner and many small losers: profitable only because of the best trade
    txs = _history(n_tokens=20, win_every=100)
    txs.append(_swap(T0 + 50, "BIG", 1000, usdc=-100))
    txs.append(_swap(T0 + 2000, "BIG", -1000, usdc=5000))
    ok, why = judge(summarize(txs, W, 100.0), s)
    assert not ok and "without its best trade" in why
    # fast flips: held 1 minute
    fast = [dict(t, timestamp=t["timestamp"] if t["tokenTransfers"][0]["toUserAccount"] == W else t["timestamp"] - 1740)
            for t in _history()]
    ok, why = judge(summarize(fast, W, 100.0), s)
    assert not ok and "hold" in why


def _db(body):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/s.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                return await body(s)
        finally:
            await eng.dispose()
    return asyncio.run(go())


def test_daily_run_finds_filters_and_starts_a_trial(monkeypatch):
    s = S()
    now = datetime.now(timezone.utc)

    async def body(sess):
        for i, mint in enumerate(("WIN1", "WIN2")):           # two winners we made money on
            sess.add(LivePosition(mode="live", account="main", token_address=mint, symbol=mint, leader_wallet="L", leader_trade_id=0,
                                  status="closed", cost_usd=30, pnl_usd=20, buy_signature=f"sig{i}",
                                  opened_at=now - timedelta(hours=5), fills=[]))
        await sess.commit()
        scout = WalletScout(s)

        async def fake_get(c, path, **params):
            if path.startswith("/addresses/WIN"):               # early buyers of each winner
                mint = path.split("/")[2]
                if params.get("before", "").startswith("sig"):
                    return [{"signature": "x", "tokenTransfers": [{"mint": mint, "tokenAmount": 5, "toUserAccount": W}]}]
                return []
            if path == f"/addresses/{W}/transactions":
                return _history() if not params.get("before") else []
            return None

        class Price:
            async def get(self, *a, **k):
                class R:
                    @staticmethod
                    def json():
                        return {"So11111111111111111111111111111111111111112": {"usdPrice": 100.0}}
                return R()

        monkeypatch.setattr(scout, "_get", fake_get)
        import app.wallet_scout as ws

        class Client:
            async def __aenter__(self):
                return Price()

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **k: Client())
        out = await scout.run_once(sess)
        cand = (await sess.execute(select(WalletCandidate))).scalars().one()
        sw = (await sess.execute(select(SmartWallet))).scalars().one()
        return out, cand, sw

    out, cand, sw = _db(body)
    assert out["found"] == 1 and out["trials"] == 1
    assert cand.status == "trial" and cand.hits == 2 and cand.metrics["closed"] == 20
    assert sw.source == WalletSource.scout and sw.track_buys and not sw.enabled


def test_trial_copies_on_paper_and_is_judged():
    s = S()
    now = datetime.now(timezone.utc)

    async def body(sess):
        sess.add(WalletCandidate(address=W, status="trial", hits=2, trial_start=now - timedelta(minutes=1),
                                 trial_end=now + timedelta(days=3)))
        await sess.commit()
        await process_entries(sess, s)                       # cursors
        await sess.commit()
        for i, mint in enumerate(("A1", "A2")):
            sess.add(WalletTrade(chain=Chain.solana, wallet=W, wallet_label="scout trial", token_address=mint,
                                 symbol=mint, tx_hash=f"b{i}", amount=10, usd_value=300, price_usd=1.0, side="buy",
                                 kind="buy", first_buy=True, block_time=now, detected_at=now))
        await sess.commit()
        await process_entries(sess, s)
        await sess.commit()
        rows = (await sess.execute(select(PaperPosition).where(PaperPosition.strategy == TRIAL_STRATEGY))).scalars().all()
        assert len(rows) == 2

        async def prices(chain, addrs):
            return {a: 2.5 for a in addrs}                   # both ran 2.5x: half out at 2x on paper
        await process_exits(sess, prices, s)
        await sess.commit()
        c = (await sess.execute(select(WalletCandidate))).scalars().one()
        c.trial_end = now - timedelta(seconds=1)              # trial over
        await sess.commit()
        await WalletScout(s).judge_trials(sess)
        await sess.commit()
        return c

    c = _db(body)
    assert c.status == "passed" and c.trial["trades"] == 2 and c.trial["net_usd"] + c.trial["open_usd"] > 0


def test_history_reads_more_pages_until_it_covers_the_days():
    s = S(scout_history_pages=8, scout_min_days=3.0)
    scout = WalletScout(s)
    pages = []

    async def fake_get(c, path, **params):
        i = len(pages)
        pages.append(i)
        # 100 swaps per page, each page one day older: page k spans days k..k+1
        start = T0 + 10 * 86400 - i * 86400
        return [{"timestamp": start - j * 864, "signature": f"p{i}-{j}"} for j in range(100)]

    scout._get = fake_get
    txs = asyncio.run(scout._history(None, W))
    assert len(pages) == 4 and len(txs) == 400          # 3.3 days covered after 4 pages, then it stops


def test_scorecard_stats_t_stat_spread_and_idle():
    m = summarize(_history(), W, 100.0, now_ts=T0 + 5 * 86400)
    # +60% / -20% half and half: mean +20%, spread ~0.41, t ~2.2 over 20 trades
    assert m["mean_ret_pct"] == 20.0 and 0.38 < m["ret_std"] < 0.44 and m["t_stat"] > 1.645
    assert 20 < m["idle_h"] < 30
    s = S(scout_max_idle_hours=72)
    rows = {c["key"]: c["ok"] for c in checks(m, s)}
    assert all(rows.values()) and len(rows) == 11
    # same trades, last seen 10 days ago: no longer active
    stale = summarize(_history(), W, 100.0, now_ts=T0 + 14 * 86400)
    ok, why = judge(stale, s)
    assert not ok and "last swap" in why

    # a lottery wallet: mostly small losses, two +900% hits -> profitable, but a wild spread and a weak t-stat
    txs = []
    for i in range(20):
        t = T0 + i * 15000
        out = 1000 if i in (3, 11) else 85
        txs += [_swap(t, f"L{i}", 1000, usdc=-100), _swap(t + 1800, f"L{i}", -1000, usdc=out)]
    lot = summarize(txs, W, 100.0, now_ts=T0 + 4 * 86400)
    bad = {c["key"] for c in checks(lot, S()) if not c["ok"]}
    assert lot["net_usd"] > 0 and {"steady", "t_stat"} <= bad
