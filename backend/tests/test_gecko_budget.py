import asyncio
from unittest.mock import patch

import httpx

from app import gecko_budget as gb
from app.cache import Cache
from app.config import Settings
from app.gecko_budget import GeckoBudget
from app.scanner_worker import Http


def test_the_api_gets_its_share_and_never_uses_up_the_workers():
    async def go():
        cache = Cache(None)
        api, worker = GeckoBudget(cache), GeckoBudget(cache)
        with patch.object(gb.time, "time", return_value=600.0):     # all inside one minute window
            taken = [await api.try_take() for _ in range(gb.API_SHARE + 10)]
            assert sum(taken) == gb.API_SHARE                         # refused tries are not counted
            for _ in range(gb.PER_MINUTE - gb.API_SHARE):
                await asyncio.wait_for(worker.acquire(), 5)          # the rest is still the worker's
            assert await cache.incr("gecko:calls:10", 120) == gb.PER_MINUTE + 1
    with patch.object(gb, "RateLimiter", lambda *a, **k: _NoWait()):
        asyncio.run(go())


def test_a_429_pauses_every_caller_and_the_worker_stops_retrying_into_it():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "0"})

    async def go():
        cache = Cache(None)
        budget = GeckoBudget(cache)
        http = Http(Settings())
        http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(http.get_json("https://gecko.test/x", limiter=budget, retries=2))
        await asyncio.sleep(0.2)
        assert len(calls) == 1 and await budget.blocked()            # paused after the first 429
        assert not await GeckoBudget(cache).try_take()               # the API backs off too
        task.cancel()
        await http.close()
    with patch.object(gb, "RateLimiter", lambda *a, **k: _NoWait()):
        asyncio.run(go())


class _NoWait:
    async def acquire(self):
        return None
