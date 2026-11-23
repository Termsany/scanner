import asyncio
import base64
import json
import struct

import httpx

from app.config import Settings
from app.pool_prices import WSOL, Book, PoolPrices, curve_address

MINT = "HuN5y4fJU7LNkaPjV9LhxkY7LUwpynW7yhL4qoVhpump"     # a real pump.fun mint (valid 32-byte key)


def _curve_b64(v_sol_sol: float, v_tok_tokens: float, complete: bool = False) -> str:
    raw = b"\x00" * 8 + struct.pack("<5Q", int(v_tok_tokens * 1e6), int(v_sol_sol * 1e9), 0, 0, 10**15)
    return base64.b64encode(raw + bytes([1 if complete else 0])).decode()


def _vault(amount: float) -> dict:
    return {"data": {"parsed": {"info": {"tokenAmount": {"uiAmountString": str(amount)}}}}}


def _s(**kw):
    return Settings(live_rpc_url="https://rpc.test/?api-key=x", live_pool_prices=True, **kw)


def test_curve_address_is_the_pump_pda():
    from solders.pubkey import Pubkey

    a = curve_address("So11111111111111111111111111111111111111112")
    assert len(a) >= 32 and a == curve_address("So11111111111111111111111111111111111111112")
    assert not Pubkey.from_string(a).is_on_curve()            # a PDA is off the ed25519 curve


def test_curve_and_pool_updates_reprice_and_completion_asks_to_resolve_again():
    pp = PoolPrices(_s())
    pp.connected = True
    pp.books[MINT] = b = Book(MINT, "curve", ["CURVE"], {"vsol": 30.0, "vtok": 1_000_000_000.0})
    b.reprice()
    pp._pending[1] = (MINT, "CURVE")
    assert pp.price_sol(MINT) is None                                   # not subscribed yet: caller uses Jupiter
    pp.on_message(json.dumps({"jsonrpc": "2.0", "id": 1, "result": 77}))
    assert pp.price_sol(MINT) == 3e-8
    pp.on_message(json.dumps({"method": "accountNotification", "params": {"subscription": 77, "result": {
        "value": {"data": [_curve_b64(60.0, 500_000_000.0), "base64"]}}}}))
    assert abs(pp.price_sol(MINT) - 1.2e-7) < 1e-18 and pp.notifications == 1
    redo = pp.on_message(json.dumps({"method": "accountNotification", "params": {"subscription": 77, "result": {
        "value": {"data": [_curve_b64(85.0, 280_000_000.0, complete=True), "base64"]}}}}))
    assert redo == MINT                                                 # migrated: look it up again as a pool

    pool = Book("P2pump", "amm", ["VBASE", "VSOL"], {"base": 80_000_000.0, "quote": 200.0}, {5: "VBASE", 6: "VSOL"})
    pool.reprice()
    pp.books["P2pump"] = pool
    pp.on_message(json.dumps({"method": "accountNotification", "params": {"subscription": 6, "result": {
        "value": _vault(240.0)}}}))
    assert abs(pp.price_sol("P2pump") - 240.0 / 80_000_000.0) < 1e-18
    pp.connected = False
    assert pp.price_sol("P2pump") is None                               # connection down: no stale prices


def test_resolve_finds_the_curve_or_the_pumpswap_vaults():
    curve = curve_address(MINT)
    state = {"curve": _curve_b64(45.0, 700_000_000.0)}

    def handler(req: httpx.Request) -> httpx.Response:
        if "dexscreener" in req.url.host:
            return httpx.Response(200, json=[
                {"dexId": "raydium", "pairAddress": "RAY", "quoteToken": {"address": WSOL}, "liquidity": {"usd": 9e9}},
                {"dexId": "pumpswap", "pairAddress": "POOL", "quoteToken": {"address": WSOL}, "liquidity": {"usd": 5e4}}])
        body = json.loads(req.content)
        if body["method"] == "getAccountInfo":
            assert body["params"][0] == curve
            v = {"data": [state["curve"], "base64"]} if state["curve"] else None
            return httpx.Response(200, json={"result": {"value": v}})
        if body["method"] == "getTokenAccountsByOwner":
            owner, mint = body["params"][0], body["params"][1]["mint"]
            assert owner == "POOL"
            if mint == WSOL:
                return httpx.Response(200, json={"result": {"value": [{"pubkey": "VSOL", "account": _vault(150.0)}]}})
            return httpx.Response(200, json={"result": {"value": [{"pubkey": "VBASE", "account": _vault(60e6)}]}})
        return httpx.Response(404)

    async def go():
        pp = PoolPrices(_s(live_pool_prices_amm=True))
        off = PoolPrices(_s())                          # default: PumpSwap coins are left to Jupiter
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            on_curve = await pp.resolve(http, MINT)
            state["curve"] = None
            on_pool = await pp.resolve(http, MINT)
            skipped = await off.resolve(http, MINT)
        return on_curve, on_pool, skipped

    on_curve, on_pool, skipped = asyncio.run(go())
    assert skipped is None
    assert on_curve.kind == "curve" and on_curve.accounts == [curve] and abs(on_curve.price - 45 / 7e8) < 1e-18
    assert on_pool.kind == "amm" and on_pool.accounts == ["VBASE", "VSOL"] and on_pool.price == 150.0 / 60e6


def test_executor_prefers_on_chain_prices(monkeypatch):
    import app.api.live as api_live
    import app.pool_prices as pool_prices
    from app.live import JupiterExecutor

    pp = PoolPrices(_s())
    pp.connected = True
    pp.books[MINT] = Book(MINT, "curve", ["C"], {"vsol": 30.0, "vtok": 1e9}, {1: "C"})
    pp.books[MINT].reprice()
    monkeypatch.setattr(pool_prices, "POOL", pp)
    asked = []

    async def fake_dex(http, mints):
        asked.append(list(mints))
        return {m: {"usd": 9.0, "decimals": 6} for m in mints}

    monkeypatch.setattr(api_live, "jupiter_prices", fake_dex)

    async def go():
        x = JupiterExecutor(Settings(), live=False)

        async def sol_price():
            return 100.0

        async def decimals(mint):
            return 6

        x.sol_price, x.decimals = sol_price, decimals
        try:
            return await x.token_prices([MINT, "Otherpump"])
        finally:
            await x.close()

    out = asyncio.run(go())
    assert abs(out[MINT]["usd"] - 3e-6) < 1e-15 and asked == [["Otherpump"]]
    assert "Otherpump" in pp._asked                     # asked for: it gets watched from the next sync
