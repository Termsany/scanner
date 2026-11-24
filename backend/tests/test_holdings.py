import asyncio
import json

import httpx

from app.config import Settings
from app.holdings import HoldingsChecker
from app.models import Chain
from app.schemas import MarketMetrics, SecurityReport
from app.smart_money import SmartWalletRegistry
from app.solana_pda import TOKEN_PROGRAM, associated_token_address, b58decode, b58encode, is_on_curve

OWNER = "8vVrVudDd2DvL5jUGFZWDSh95VjhLpeETRAp1kmFnb61"
MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_W = "0xb5002f252e8153edf930d13f7979fedbbb086401"
EVM_T = "0x00000000000000000000000000000000000000aa"
S = Settings(holdings_min_interval_seconds=0.0)


def m(chain, addr, price):
    return MarketMetrics(chain=chain, address=addr, symbol="T", price_usd=price)


def run(handler, tokens, wallets):
    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await HoldingsChecker(S, http=http).check(tokens, wallets)
        finally:
            await http.aclose()
    return asyncio.run(go())


def test_base58_and_pda_helpers():
    assert b58encode(b58decode(OWNER)) == OWNER and len(b58decode(OWNER)) == 32
    ata = associated_token_address(OWNER, MINT_A)
    assert len(b58decode(ata)) == 32 and not is_on_curve(b58decode(ata))
    assert is_on_curve(b58decode(OWNER))  # a real wallet key is an ed25519 point


def test_solana_holdings_respect_min_usd():
    spl_a = associated_token_address(OWNER, MINT_A, TOKEN_PROGRAM)
    spl_b = associated_token_address(OWNER, MINT_B, TOKEN_PROGRAM)

    def handler(req):
        body = json.loads(req.content)
        accounts = []
        for a in body["params"][0]:
            if a in (spl_a, spl_b):
                mint, amount = (MINT_A, 1000.0) if a == spl_a else (MINT_B, 10.0)
                accounts.append({"data": {"parsed": {"info": {"owner": OWNER, "mint": mint,
                                                               "tokenAmount": {"uiAmount": amount}}}}})
            else:
                accounts.append(None)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": accounts}})

    out = run(handler, [m(Chain.solana, MINT_A, 0.5), m(Chain.solana, MINT_B, 0.5)],
              {Chain.solana: {OWNER: "BE 60%"}})
    assert out[(Chain.solana, MINT_A)] == ["BE 60%"]          # 1000 x $0.5 = $500
    assert (Chain.solana, MINT_B) not in out                    # 10 x $0.5 = $5 dust


def test_solana_holdings_use_the_trading_rpc_not_the_public_one():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": []}})

    async def go(s):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await HoldingsChecker(s, http=http).check([m(Chain.solana, MINT_A, 1.0)], {Chain.solana: {OWNER: "x"}})
        finally:
            await http.aclose()

    pub = "https://public.test/"
    asyncio.run(go(Settings(holdings_min_interval_seconds=0.0, solana_rpc_url=pub,
                            live_rpc_url="https://helius.test/?api-key=k")))
    asyncio.run(go(Settings(holdings_min_interval_seconds=0.0, solana_rpc_url=pub, live_rpc_url="https://helius.test/",
                            holdings_solana_rpc_url="https://own.test/")))
    asyncio.run(go(Settings(holdings_min_interval_seconds=0.0, solana_rpc_url=pub, live_rpc_url=None)))
    assert seen == ["https://helius.test/?api-key=k", "https://own.test/", pub]


def test_evm_balance_of_batch():
    def handler(req):
        resp = []
        for call in json.loads(req.content):
            data = call["params"][0]["data"]
            if data.startswith("0x313ce567"):
                result = hex(18)
            elif EVM_W.removeprefix("0x") in data:
                result = hex(500 * 10**18)
            else:
                result = "0x0"
            resp.append({"jsonrpc": "2.0", "id": call["id"], "result": result})
        return httpx.Response(200, json=resp)

    other = "0x" + "1" * 40
    out = run(handler, [m(Chain.base, EVM_T, 1.0)], {Chain.base: {EVM_W: "BE 80%", other: "Other"}})
    assert out[(Chain.base, EVM_T)] == ["BE 80%"]


def test_rpc_failure_is_swallowed():
    def handler(req):
        return httpx.Response(503, text="overloaded")

    assert dict(run(handler, [m(Chain.solana, MINT_A, 1.0)], {Chain.solana: {OWNER: "x"}})) == {}


def test_registry_merges_onchain_hits_with_top_holders():
    reg = SmartWalletRegistry(S)
    reg.wallets = {Chain.solana: {OWNER: "BE 60%", "Other111": "Fund"}}
    sec = SecurityReport(chain=Chain.solana, address=MINT_A, providers=["rugcheck"], top_holders=[])
    info = reg.evaluate(Chain.solana, sec, ["BE 60%"])
    assert info.tracked_wallet_hits == ["BE 60%"] and not info.inferred
    assert reg.evaluate(Chain.solana, sec).tracked_wallet_hits == []
