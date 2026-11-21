import asyncio
import json
import tempfile

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.fast_watch import WAKE, FastWatcher, ws_url
from app.models import Base, WalletTrade
from app.wallet_tracker import WalletTracker

W = "DULX3e6vt1ZpHq3AdCUBZLPZ1CrhApdNKcewMPxZQRXf"
MINT = "3UNWAGVf5Z8ZDLXmrXZ6uBKvt6cbWdK5uCcMYk2bpump"
WSOL = "So11111111111111111111111111111111111111112"
S = Settings(tracker_min_buy_usd=100, tracker_min_liquidity_usd=5_000, live_rpc_url="https://rpc.test/?api-key=x")


def bal(owner, mint, amt):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}


def buy_tx():
    return {"blockTime": 1_794_970_000, "meta": {"err": None,
            "preTokenBalances": [bal(W, WSOL, 5.0)],
            "postTokenBalances": [bal(W, WSOL, 1.0), bal(W, MINT, 1500.0)]}}


def dex(req):
    return httpx.Response(200, json=[{"baseToken": {"address": MINT, "symbol": "GEM", "name": "Gem"},
                                      "priceUsd": "0.5", "liquidity": {"usd": 80_000}}])


def note(sub, sig, err=None):
    return json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
        "subscription": sub, "result": {"value": {"signature": sig, "err": err, "logs": []}}}})


def test_ws_url():
    assert ws_url("https://mainnet.helius-rpc.com/?api-key=k") == "wss://mainnet.helius-rpc.com/?api-key=k"


def test_notification_records_trade_wakes_engines_and_dedupes():
    fetched = []

    async def fetch(sig):
        fetched.append(sig)
        return buy_tx()

    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/fw.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(eng, expire_on_commit=False)
        tracker = WalletTracker(S, http=httpx.AsyncClient(transport=httpx.MockTransport(dex)))
        fw = FastWatcher([W], S, fetch_tx=fetch, tracker=tracker, session_factory=factory)
        ev = asyncio.Event()
        WAKE.append(ev)
        try:
            [req] = fw.subscribe_requests()
            assert json.loads(req)["params"][0] == {"mentions": [W]}
            await fw.handle_message(json.dumps({"jsonrpc": "2.0", "id": 1, "result": 77}))  # sub confirmed
            assert await fw.handle_message(note(77, "SIG1")) == 1
            assert ev.is_set()
            assert await fw.handle_message(note(77, "SIG1")) == 0            # same signature again
            assert await fw.handle_message(note(77, "SIG2", err={"x": 1})) == 0   # failed tx
            assert await fw.handle_message(note(99, "SIG3")) == 0            # unknown subscription
            # the poller later sees SIG1 too: record() skips it (already stored)
            async with factory() as s:
                from app.wallet_tracker import Trade
                from app.models import Chain
                await tracker.record(s, [Trade(Chain.solana, W, MINT, 1500.0, "SIG1")], {})
                await s.commit()
                rows = (await s.execute(select(WalletTrade))).scalars().all()
            return rows
        finally:
            WAKE.remove(ev)
            await fw.close()
            await eng.dispose()

    rows = asyncio.run(go())
    assert fetched == ["SIG1"]
    assert [(r.tx_hash, r.side, r.symbol, r.usd_value) for r in rows] == [("SIG1", "buy", "GEM", 750.0)]


def test_a_busy_wallet_does_not_hold_up_another_and_one_wallet_stays_in_order():
    order = []

    async def go():
        fw = FastWatcher([W, "Busy1111111111111111111111111111111111111111"], S, fetch_tx=lambda s: None)
        fw._subs = {1: "Busy1111111111111111111111111111111111111111", 2: W}

        async def process(wallet, sig):
            if wallet.startswith("Busy"):
                await asyncio.sleep(0.3)                 # slow: DEX price, retries...
            order.append(sig)
            return 1
        fw.process = process

        def n(sub, sig):
            return json.dumps({"jsonrpc": "2.0", "method": "logsNotification",
                               "params": {"subscription": sub, "result": {"value": {"signature": sig, "err": None}}}})
        fw.dispatch(n(1, "BUSY-A"))
        fw.dispatch(n(1, "BUSY-B"))
        fw.dispatch(n(2, "LEADER"))
        await asyncio.sleep(1.0)
        await fw.close()

    asyncio.run(go())
    assert order == ["LEADER", "BUSY-A", "BUSY-B"]       # the leader isn't queued behind; the busy one keeps its order
