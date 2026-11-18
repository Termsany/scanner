"""Cache, pub/sub and rate limiting.

Redis is used when `REDIS_URL` is set; otherwise everything falls back to an
in-process implementation so the MVP runs with zero infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class Cache:
    """JSON key/value cache with TTL + a publish() for live signals."""

    def __init__(self, redis_url: str | None) -> None:
        self._redis = None
        self._mem: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()
        if redis_url:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(redis_url, decode_responses=True)
            except Exception:  # pragma: no cover - import/connection issues
                log.exception("Redis unavailable, using in-memory cache")
                self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    async def get_json(self, key: str) -> Any | None:
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                return json.loads(raw) if raw else None
            except Exception:
                log.warning("Redis GET failed for %s", key, exc_info=True)
                return None
        async with self._lock:
            item = self._mem.get(key)
            if item is None:
                return None
            expires_at, raw = item
            if expires_at < time.monotonic():
                self._mem.pop(key, None)
                return None
            return json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        raw = json.dumps(value, default=str)
        if self._redis is not None:
            try:
                await self._redis.set(key, raw, ex=ttl_seconds)
                return
            except Exception:
                log.warning("Redis SET failed for %s", key, exc_info=True)
        async with self._lock:
            self._mem[key] = (time.monotonic() + ttl_seconds, raw)
            if len(self._mem) > 20_000:  # bound memory in long-running workers
                now = time.monotonic()
                self._mem = {k: v for k, v in self._mem.items() if v[0] >= now}

    async def set_if_absent(self, key: str, ttl_seconds: int) -> bool:
        """Atomic 'claim' used for signal de-duplication. True if the key was newly set."""
        if self._redis is not None:
            try:
                return bool(await self._redis.set(key, "1", ex=ttl_seconds, nx=True))
            except Exception:
                log.warning("Redis SETNX failed for %s", key, exc_info=True)
        async with self._lock:
            item = self._mem.get(key)
            if item and item[0] >= time.monotonic():
                return False
            self._mem[key] = (time.monotonic() + ttl_seconds, "1")
            return True

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        """Publish to Redis pub/sub (consumed by the Phase 2 WebSocket server)."""
        if self._redis is None:
            return  # API reads Signal rows from the DB when Redis is absent
        try:
            await self._redis.publish(channel, json.dumps(payload, default=str))
        except Exception:
            log.warning("Redis PUBLISH failed on %s", channel, exc_info=True)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


class RateLimiter:
    """Async token bucket. `rate` tokens per `per` seconds; `burst` caps back-to-back calls."""

    def __init__(self, rate: int, per: float, burst: int | None = None) -> None:
        if rate <= 0 or per <= 0:
            raise ValueError("rate and per must be positive")
        self.capacity = float(min(burst or rate, rate))
        self.tokens = self.capacity
        self.fill_rate = rate / per
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.fill_rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.fill_rate)
