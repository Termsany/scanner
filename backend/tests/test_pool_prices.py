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


VBASE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"      # any valid keys stand in for the vaults
VSOL = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def _pool_b64(vq_sol: float) -> str:
    from solders.pubkey import Pubkey

    raw = bytearray(301)
    raw[139:171] = bytes(Pubkey.from_string(VBASE))
    raw[171:203] = bytes(Pubkey.from_string(VSOL))
    raw[245:261] = int(vq_sol * 1e9).to_bytes(16, "little", signed=True)
    return base64.b64encode(bytes(raw)).decode()


def test_resolve_finds_the_curve_or_the_canonical_pumpswap_pool():
    from app.pool_prices import AMM_PROGRAM, parse_pool, pool_address

    curve, pool = curve_address(MINT), pool_address(MINT)
    state = {"curve": _curve_b64(45.0, 700_000_000.0)}
    assert parse_pool(_pool_b64(17.5)) == (VBASE, VSOL, 17.5) and parse_pool(base64.b64encode(b"x" * 40).decode()) is None

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["method"] == "getAccountInfo":
            if body["params"][0] == curve:
                v = {"data": [state["curve"], "base64"]} if state["curve"] else None
                return httpx.Response(200, json={"result": {"value": v}})
            assert body["params"][0] == pool
            return httpx.Response(200, json={"result": {"value": {"owner": AMM_PROGRAM,
                                                                  "data": [_pool_b64(17.5), "base64"]}}})
        if body["method"] == "getMultipleAccounts":
            assert body["params"][0] == [VBASE, VSOL]
            return httpx.Response(200, json={"result": {"value": [_vault(60e6), _vault(150.0)]}})
        return httpx.Response(404)

    async def go():
        pp = PoolPrices(_s())
        off = PoolPrices(_s(live_pool_prices_amm=False))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            on_curve = await pp.resolve(http, MINT)
            state["curve"] = None
            on_pool = await pp.resolve(http, MINT)
            skipped = await off.resolve(http, MINT)
        return on_curve, on_pool, skipped

    on_curve, on_pool, skipped = asyncio.run(go())
    assert skipped is None
    assert on_curve.kind == "curve" and on_curve.accounts == [curve] and abs(on_curve.price - 45 / 7e8) < 1e-18
    assert on_pool.kind == "amm" and on_pool.accounts == [VBASE, VSOL, pool]
    assert abs(on_pool.price - (150.0 + 17.5) / 60e6) < 1e-18            # vault SOL + the pool's virtual reserve


def test_pool_account_update_moves_the_virtual_reserve():
    pp = PoolPrices(_s())
    pp.connected = True
    b = Book(MINT, "amm", [VBASE, VSOL, "POOL"], {"base": 60e6, "quote": 150.0, "vq": 17.5},
             {1: VBASE, 2: VSOL, 3: "POOL"})
    b.reprice()
    pp.books[MINT] = b
    pp.on_message(json.dumps({"method": "accountNotification", "params": {"subscription": 3, "result": {
        "value": {"data": [_pool_b64(20.0), "base64"]}}}}))
    assert abs(pp.price_sol(MINT) - 170.0 / 60e6) < 1e-18


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
