import asyncio
import json
import tempfile

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base, Chain, SmartWallet, TrackerCursor, WalletSource, WalletTrade
from app.smart_money import parse_track_target, set_tracking, tracked_wallets
from app.wallet_tracker import TRANSFER_TOPIC, Buy, WalletTracker, evm_buys_from_logs, solana_buys_from_tx

W = "8vVrVudDd2DvL5jUGFZWDSh95VjhLpeETRAp1kmFnb61"
MINT = "3UNWAGVf5Z8ZDLXmrXZ6uBKvt6cbWdK5uCcMYk2bpump"
WSOL = "So11111111111111111111111111111111111111112"
EW = "0xb5002f252e8153edf930d13f7979fedbbb086401"
S = Settings(tracker_min_buy_usd=100, tracker_min_liquidity_usd=5_000)


def bal(owner, mint, amt):
    return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}


def test_solana_balance_delta_detects_buys_only():
    tx = {"blockTime": 1_794_970_000, "meta": {"err": None,
          "preTokenBalances": [bal(W, WSOL, 5.0), bal("Pool", MINT, 1e6)],
          "postTokenBalances": [bal(W, WSOL, 1.0), bal(W, MINT, 1500.0), bal("Pool", MINT, 998_500.0)]}}
    buys = solana_buys_from_tx(tx, W, "sig1")
    assert [(b.token, b.amount) for b in buys] == [(MINT, 1500.0)]  # wSOL spent is not a buy; pool ignored
    tx["transaction"] = {"message": {"accountKeys": [{"pubkey": "Spammer", "signer": True},
                                                     {"pubkey": W, "signer": False}]}}
    assert solana_buys_from_tx(tx, W, "sig1") == []  # airdrop: wallet did not sign
    tx["transaction"]["message"]["accountKeys"][1]["signer"] = True
    assert len(solana_buys_from_tx(tx, W, "sig1")) == 1
    tx["meta"]["err"] = {"InstructionError": [0, "x"]}
    assert solana_buys_from_tx(tx, W, "sig1") == []


def test_evm_logs_require_wallet_initiated_tx():
    pad = lambda a: "0x" + a.removeprefix("0x").rjust(64, "0")
    tok = "0x00000000000000000000000000000000000000aa"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    logs = [
        {"address": tok, "topics": [TRANSFER_TOPIC, pad("0x01"), pad(EW)], "data": hex(2 * 10**18), "transactionHash": "0xA"},
        {"address": tok, "topics": [TRANSFER_TOPIC, pad("0x01"), pad(EW)], "data": hex(10**18), "transactionHash": "0xA"},
        {"address": tok, "topics": [TRANSFER_TOPIC, pad("0x02"), pad(EW)], "data": hex(10**18), "transactionHash": "0xB"},
        {"address": weth, "topics": [TRANSFER_TOPIC, pad("0x01"), pad(EW)], "data": hex(10**18), "transactionHash": "0xA"},
    ]
    senders = {"0xa": EW, "0xb": "0xspammer"}
    buys = evm_buys_from_logs(Chain.ethereum, logs, senders, {EW}, {tok: 18})  # mainnet WETH is a quote
    assert [(b.token, b.amount, b.tx_hash) for b in buys] == [(tok, 3.0, "0xa")]  # summed; spam + WETH dropped


def test_parse_track_target():
    assert parse_track_target("evm", EW.upper().replace("0X", "0x")) == ([Chain.ethereum, Chain.base, Chain.bsc], EW)
    assert parse_track_target("sol", W) == ([Chain.solana], W)
    for chain, addr in (("solana", "nothex!!"), ("base", "0x123"), ("doge", EW)):
        with pytest.raises(ValueError):
            parse_track_target(chain, addr)


def _db(body):
    async def go():
        tmp = tempfile.mkdtemp()
        eng = create_async_engine(f"sqlite+aiosqlite:///{tmp}/wt.db".replace("\\", "/"))
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        try:
            async with async_sessionmaker(eng, expire_on_commit=False)() as s:
                return await body(s)
        finally:
            await eng.dispose()
    return asyncio.run(go())


def test_set_tracking_watch_only_and_existing_wallets():
    async def body(s):
        s.add(SmartWallet(chain=Chain.solana, address=W, label="BE 60%", source=WalletSource.birdeye, enabled=True))
        await s.flush()
        assert await set_tracking(s, [Chain.solana], W, "", True) == 1
        assert await set_tracking(s, [Chain.ethereum, Chain.base, Chain.bsc], EW, "Whale", True) == 3
        rows = await tracked_wallets(s)
        assert {(w.chain, w.label, w.enabled) for w in rows} == {
            (Chain.solana, "BE 60%", True), (Chain.ethereum, "Whale", False), (Chain.base, "Whale", False),
            (Chain.bsc, "Whale", False)}
        await set_tracking(s, [Chain.ethereum, Chain.base, Chain.bsc], EW, "", False)  # watch-only rows deleted
        await set_tracking(s, [Chain.solana], W, "", False)                             # scoring wallet kept
        left = (await s.execute(select(SmartWallet))).scalars().all()
        assert [(w.address, w.track_buys, w.enabled) for w in left] == [(W, False, True)]
    _db(body)


def tracker_with(handler):
    return WalletTracker(S.model_copy(update={"solana_rpc_url": "https://rpc.test"}),
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_solana_poll_starts_from_now_then_reports_new_buys():
    state = {"sigs": [{"signature": "OLD", "err": None}]}

    def handler(req):
        body = json.loads(req.content)
        if body["method"] == "getSignaturesForAddress":
            until = (body["params"][1] or {}).get("until")
            return httpx.Response(200, json={"result": [x for x in state["sigs"] if x["signature"] != until][:1]
                                             if until is None else [x for x in state["sigs"] if x["signature"] != until]})
        if body["method"] == "getTransaction":
            return httpx.Response(200, json={"result": {"blockTime": 1, "meta": {"err": None, "preTokenBalances": [],
                                                                                  "postTokenBalances": [bal(W, MINT, 42.0)]}}})
        return httpx.Response(404)

    async def body(s):
        t = tracker_with(handler)
        assert await t.poll_solana(s, W) == []                       # first poll: cursor only, no history
        assert (await s.get(TrackerCursor, f"sol:{W}")).value == "OLD"
        state["sigs"] = [{"signature": "NEW", "err": None}, {"signature": "OLD", "err": None}]
        buys = await t.poll_solana(s, W)
        assert [(b.token, b.amount, b.tx_hash) for b in buys] == [(MINT, 42.0, "NEW")]
        assert (await s.get(TrackerCursor, f"sol:{W}")).value == "NEW"
        await t.http.aclose()
    _db(body)


def test_record_prices_filters_and_regroups():
    def handler(req):
        if "dexscreener" in req.url.host:
            pairs = [{"baseToken": {"address": MINT, "symbol": "GEM", "name": "Gem"}, "priceUsd": "0.5",
                      "liquidity": {"usd": 80_000}, "marketCap": 900_000, "url": "https://dexscreener.com/x"},
                     {"baseToken": {"address": "THIN", "symbol": "THIN"}, "priceUsd": "1", "liquidity": {"usd": 900}}]
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)

    async def body(s):
        t = tracker_with(handler)
        labels = {(Chain.solana, W): "BE 60%"}
        n = await t.record(s, [Buy(Chain.solana, W, MINT, 1000.0, "tx1"),      # $500 -> alert
                               Buy(Chain.solana, W, MINT, 10.0, "tx2"),        # $5 -> below min
                               Buy(Chain.solana, W, "THIN", 5000.0, "tx3")],   # liquidity $900 -> dropped
                           labels)
        assert n == 1
        n2 = await t.record(s, [Buy(Chain.solana, W, MINT, 2000.0, "tx4")], labels)  # same token within 60m
        assert n2 == 0
        rows = (await s.execute(select(WalletTrade).order_by(WalletTrade.id))).scalars().all()
        assert [(r.tx_hash, r.usd_value, r.first_buy, r.telegram_sent) for r in rows] == [
            ("tx1", 500.0, True, False), ("tx4", 1000.0, False, True)]
        assert rows[0].symbol == "GEM" and rows[0].wallet_label == "BE 60%" and rows[0].liquidity_usd == 80_000
        await t.http.aclose()
    _db(body)
