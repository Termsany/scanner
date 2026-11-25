"""One GeckoTerminal call budget for the whole server.

GeckoTerminal's keyless API allows about 30 calls a minute per IP. The worker (discovery lists, signal performance)
and the API (token charts, the /live peaks) all call it from the same IP, each with its own limit or none, so
together they went over and got 429s (2026-11-25). They now count every call in one Redis window per minute:

- the worker waits for a slot (it needs the data; discovery lists, performance candles);
- the API only takes a slot while `API_SHARE` are left unused, and otherwise falls back (snapshots, no peak);
- after a 429 everyone pauses `BLOCK_SECONDS`, instead of retrying into the same exhausted window.
"""

from __future__ import annotations

import asyncio
import time

from app.cache import Cache, RateLimiter

PER_MINUTE = 26          # under the ~30/min quota, leaving room for clock skew between the window and theirs
API_SHARE = 12           # the API (charts, peaks) may use at most this many a minute; the worker gets the rest
BLOCK_SECONDS = 30
BLOCK_KEY = "gecko:blocked"


def _window() -> tuple[str, float]:
    now = time.time()
    return f"gecko:calls:{int(now // 60)}", 60.0 - now % 60.0


class GeckoBudget:
    def __init__(self, cache: Cache) -> None:
        self.cache = cache
        self._smooth = RateLimiter(PER_MINUTE, 60.0, burst=3)   # GeckoTerminal punishes back-to-back bursts

    async def blocked(self) -> bool:
        return bool(await self.cache.get_json(BLOCK_KEY))

    async def block(self) -> None:
        """Called after a 429: nobody calls for BLOCK_SECONDS."""
        await self.cache.set_json(BLOCK_KEY, True, ttl_seconds=BLOCK_SECONDS)

    async def try_take(self, limit: int = API_SHARE) -> bool:
        """A slot now, or False (never waits): for the API, which has a fallback."""
        if await self.blocked():
            return False
        key, _ = _window()
        # the API's own count first, so a refused try never uses up the worker's share
        if await self.cache.incr(key.replace("calls", "api"), 120) > limit:
            return False
        return await self.cache.incr(key, 120) <= PER_MINUTE

    async def acquire(self) -> None:
        """Wait for a slot: for the worker. Same interface as RateLimiter, so Http.get_json can use it."""
        await self._smooth.acquire()
        while True:
            if await self.blocked():
                await asyncio.sleep(5.0)
                continue
            key, left = _window()
            if await self.cache.incr(key, 120) <= PER_MINUTE:
                return
            await asyncio.sleep(left + 0.5)

    async def penalize(self) -> None:
        await self.block()
