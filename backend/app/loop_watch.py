"""Event-loop freeze monitor for the worker.

Everything in the worker (scanner, copy trading, WebSocket watchers, paper, scout) shares one asyncio loop: one
step that blocks it delays every WebSocket message, so a trade can look "late from Helius" when it was waiting
for us. This logs every freeze longer than LOOP_FREEZE_LOG_SECONDS and a summary every 10 minutes. With
LOOP_DEBUG on, asyncio itself names the step that blocked (slow callback) so the culprit can be found.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import Settings, get_settings

log = logging.getLogger("loop_watch")

TICK = 0.25


async def watch_loop(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    if s.loop_debug:
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        loop.slow_callback_duration = s.loop_freeze_log_seconds
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        log.info("loop watch: asyncio debug on (steps over %.1fs are named)", s.loop_freeze_log_seconds)
    worst, freezes, frozen_total = 0.0, 0, 0.0
    since = time.monotonic()
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(TICK)
        lag = time.monotonic() - t0 - TICK
        if lag >= s.loop_freeze_log_seconds:
            freezes += 1
            frozen_total += lag
            worst = max(worst, lag)
            log.warning("loop watch: the worker was frozen for %.1fs", lag)
        if time.monotonic() - since >= 600:
            log.info("loop watch: last 10 min: %d freezes over %.1fs, %.0fs frozen in all, worst %.1fs",
                     freezes, s.loop_freeze_log_seconds, frozen_total, worst)
            worst, freezes, frozen_total, since = 0.0, 0, 0.0, time.monotonic()
