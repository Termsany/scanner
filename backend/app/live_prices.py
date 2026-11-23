"""Live prices of the coins the live accounts hold, shared from the worker with the /live page (2026-11-23).

The page used DexScreener, which lags far behind on a pump.fun coin minutes old (a curve trade at +130 % showed
a stale value). The worker already sees those coins' exact bonding-curve price (the paper curve engine, Helius) and,
for other pump coins, the on-chain pool feed; it publishes them here (Redis, shared by the api and the worker)
and the page prefers them while they are fresh.
"""

from __future__ import annotations

import time
from typing import Any

from app.cache import Cache
from app.config import get_settings

KEY = "livepx:"
TTL_S = 120                 # kept in Redis
FRESH_S = 20.0              # the page uses a price at most this old

_cache: Cache | None = None


def cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(get_settings().redis_url)
    return _cache


async def publish(prices: dict[str, float], now: float | None = None) -> None:
    """{mint: USD per whole token}."""
    at = time.time() if now is None else now
    for mint, usd in prices.items():
        if usd and usd > 0:
            await cache().set_json(KEY + mint, {"usd": float(usd), "at": at}, TTL_S)


async def read(mints: list[str], now: float | None = None) -> dict[str, float]:
    """Fresh published prices for these mints (USD per whole token)."""
    now = time.time() if now is None else now
    out: dict[str, float] = {}
    for mint in mints:
        got: Any = await cache().get_json(KEY + mint)
        if got and got.get("usd") and now - float(got.get("at") or 0) <= FRESH_S:
            out[mint] = float(got["usd"])
    return out
