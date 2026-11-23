import asyncio
import json
import time

from app.config import Settings
from app.pump_prices import PumpPrices, is_pump_coin, price_from

A = "AAAA1111111111111111111111111111111111pump"
B = "BBBB2222222222222222222222222222222222pump"
NOT_PUMP = "So11111111111111111111111111111111111111112"


def _s(**kw):
    return Settings(pumpportal_api_key="k", live_pump_prices=True, **kw)


def test_price_from_curve_pumpswap_and_marketcap():
    assert price_from({"vSolInBondingCurve": 30.0, "vTokensInBondingCurve": 1_000_000_000.0}) == 3e-8
    assert price_from({"solAmount": 0.5, "tokenAmount": 1_000_000}) == 5e-7          # trade price, last resort
    assert price_from({"marketCapSol": 40.0}) == 4e-8
    # real PumpSwap message (2026-11-23): pool reserves win over the trade's own (fee-inclusive) price
    real = {"txType": "sell", "tokenAmount": 202796.42104499042, "solAmount": 0.5716637000000162,
            "tokensInPool": 81351851.772133, "solInPool": 214.142361546, "marketCapSol": 2632.2985510620456,
            "pool": "pump-amm"}
    assert abs(price_from(real) - 214.142361546 / 81351851.772133) < 1e-15
    assert abs(price_from(real) - 2632.2985510620456 / 1e9) / price_from(real) < 1e-6   # agrees with market cap
    assert price_from({"solAmount": "x"}) is None and price_from({}) is None


def test_only_asked_coins_are_subscribed_then_dropped():
    pp = PumpPrices(_s(live_pump_prices_keep_seconds=60, live_pump_prices_max_coins=1))
    pp.request([A, NOT_PUMP])                       # non-pump mints are never streamed
    [sub] = pp.sync_messages()
    assert json.loads(sub) == {"method": "subscribeTokenTrade", "keys": [A]}
    assert pp.sync_messages() == []                 # nothing changed
    pp._asked[A] = time.monotonic() - 5             # A asked earlier
    pp.request([B])                                 # cap 1: the newest ask wins
    msgs = [json.loads(m) for m in pp.sync_messages()]
    assert {"method": "subscribeTokenTrade", "keys": [B]} in msgs
    assert {"method": "unsubscribeTokenTrade", "keys": [A]} in msgs
    pp._asked[A] = pp._asked[B] = time.monotonic() - 120   # nobody asked for either in a while
    assert [json.loads(m)["method"] for m in pp.sync_messages()] == ["unsubscribeTokenTrade"]


def test_trades_update_price_and_old_prices_expire():
    pp = PumpPrices(_s(live_pump_prices_max_age_seconds=90))
    pp.request([A])
    pp.sync_messages()
    pp.on_message(json.dumps({"mint": B, "txType": "buy", "solAmount": 1, "tokenAmount": 1}))   # not subscribed
    pp.on_message(json.dumps({"message": "Successfully subscribed to keys."}))
    pp.on_message(json.dumps({"mint": A, "txType": "sell", "vSolInBondingCurve": 60.0,
                              "vTokensInBondingCurve": 600_000_000.0}))
    assert pp.price_sol(A) == 1e-7 and pp.price_sol(B) is None and pp.events == 1
    pp._px[A] = (1e-7, time.monotonic() - 200)
    assert pp.price_sol(A) is None


def test_executor_uses_pumpportal_first_and_jupiter_for_the_rest(monkeypatch):
    import app.api.live as api_live
    import app.pump_prices as pump_prices
    from app.live import JupiterExecutor

    pp = PumpPrices(_s())
    pp.request([A])
    pp.sync_messages()
    pp.on_message(json.dumps({"mint": A, "txType": "buy", "vSolInBondingCurve": 30.0,
                              "vTokensInBondingCurve": 1_000_000_000.0}))
    monkeypatch.setattr(pump_prices, "PUMP", pp)
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
            return await x.token_prices([A, B])
        finally:
            await x.close()

    out = asyncio.run(go())
    assert abs(out[A]["usd"] - 3e-6) < 1e-12 and out[A]["decimals"] == 6     # 3e-8 SOL x $100
    assert out[B]["usd"] == 9.0 and asked == [[B]]                          # only B went to DexScreener
