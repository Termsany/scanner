"""Per-strategy switches on /paper (2026-11-23): paused = no new paper trades (open ones are still managed),
hidden = left off the page. Kept in tracker_cursors, one row per strategy and switch.

The engines (app/paper.py, app/curve.py, app/sniper.py, app/grad.py) all run in the worker and read PAUSED; any of
them refreshes it from the database (at most every REFRESH_S). Pausing a curve rule on paper never stops the live
accounts that trade it: its real-money signal is still sent.
"""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TrackerCursor

PAUSE, HIDE = "paper_pause:", "paper_hide:"
REFRESH_S = 10.0

PAUSED: set[str] = set()
_loaded = -1e9


def paused(strategy: str) -> bool:
    return strategy in PAUSED


async def read(session: AsyncSession) -> tuple[set[str], set[str]]:
    rows = (await session.execute(select(TrackerCursor).where(
        TrackerCursor.key.like(PAUSE + "%") | TrackerCursor.key.like(HIDE + "%")))).scalars().all()
    p = {r.key[len(PAUSE):] for r in rows if r.key.startswith(PAUSE) and r.value == "1"}
    h = {r.key[len(HIDE):] for r in rows if r.key.startswith(HIDE) and r.value == "1"}
    return p, h


async def refresh(session: AsyncSession, force: bool = False) -> None:
    global _loaded, PAUSED
    if not force and time.monotonic() - _loaded < REFRESH_S:
        return
    _loaded = time.monotonic()
    try:
        PAUSED = (await read(session))[0]
    except Exception:
        pass                                  # keep the last known switches


async def set_switch(session: AsyncSession, strategy: str, *, paused: bool | None = None,
                     hidden: bool | None = None) -> None:
    for prefix, val in ((PAUSE, paused), (HIDE, hidden)):
        if val is None:
            continue
        key = (prefix + strategy)[:160]
        row = await session.get(TrackerCursor, key)
        if row is None:
            session.add(TrackerCursor(key=key, value="1" if val else "0"))
        else:
            row.value = "1" if val else "0"
