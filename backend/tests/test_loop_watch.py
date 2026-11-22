import asyncio
import logging
import time

from app.config import Settings
from app.loop_watch import watch_loop


def test_a_blocking_step_is_logged(caplog):
    async def go():
        task = asyncio.create_task(watch_loop(Settings(loop_freeze_log_seconds=0.3, loop_debug=False)))
        await asyncio.sleep(0.3)
        time.sleep(0.6)                       # something blocks the loop
        await asyncio.sleep(0.4)
        task.cancel()

    with caplog.at_level(logging.WARNING, logger="loop_watch"):
        asyncio.run(go())
    assert any("frozen for" in r.message for r in caplog.records)
