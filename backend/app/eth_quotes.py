"""What a buy-and-sell-back of a token really costs on Ethereum, from KyberSwap's aggregator (no key needed).

2026-11-26: signal_eth's paper profit leaned on mihao ($238 of $315), whose quoted round trip at $200 gives back
22% of the ETH; VAR 22-67%. JIMMY, GMPEPE and SP lose 0-1%. GoPlus said 0% tax on all of them and showed $100k+
liquidity, so only a real quote tells them apart.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

KYBER = "https://aggregator-api.kyberswap.com/ethereum/api/v1/routes"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
CACHE_S = 600.0
_cache: dict[tuple[str, int], tuple[float, float | None]] = {}
_eth_usd: tuple[float, float] = (0.0, 0.0)
_lock = asyncio.Lock()


async def _route(c: httpx.AsyncClient, token_in: str, token_out: str, amount: int) -> dict | None:
    for attempt in range(3):
        r = await c.get(KYBER, params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount)})
        if r.status_code in (403, 429):                  # their rate limit: back off and try again
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        d = r.json() if r.status_code == 200 else {}
        return (d.get("data") or {}).get("routeSummary")
    return None


async def roundtrip_loss_pct(token: str, usd: float) -> float | None:
    """% of the ETH lost buying `usd` of `token` and selling it straight back (gas included), or None when no route
    either way. Cached 10 minutes per token and size; calls are serialized to stay under KyberSwap's limit."""
    key = (token.lower(), int(usd))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    global _eth_usd
    async with _lock:
        try:
            async with httpx.AsyncClient(timeout=15, headers={"x-client-id": "alpha-scanner"}) as c:
                if time.monotonic() - _eth_usd[0] > 300 or not _eth_usd[1]:
                    probe = await _route(c, NATIVE, "0xdAC17F958D2ee523a2206206994597C13D831ec7", 10 ** 16)
                    px = float(probe["amountInUsd"]) * 100 if probe else 0.0
                    if px > 0:
                        _eth_usd = (time.monotonic(), px)
                if not _eth_usd[1]:
                    return None
                wei = int(usd / _eth_usd[1] * 1e18)
                buy = await _route(c, NATIVE, token, wei)
                if not buy or int(buy.get("amountOut") or 0) <= 0:
                    loss = None
                else:
                    sell = await _route(c, token, NATIVE, int(buy["amountOut"]))
                    if not sell:
                        loss = None
                    else:
                        gas_usd = float(buy.get("gasUsd") or 0) + float(sell.get("gasUsd") or 0)
                        back = int(sell["amountOut"]) / wei * usd - gas_usd
                        loss = round((1 - back / usd) * 100, 2)
        except Exception as exc:
            log.warning("eth round trip %s: %s", token[:10], exc)
            return None
    _cache[key] = (time.monotonic(), loss)
    return loss
