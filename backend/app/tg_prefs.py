"""Which Telegram messages the operator's chat gets: one switch per kind, set on /admin.

Stored in tracker_cursors["telegram:prefs"] as "off=kind,kind" (the column is short); read by the API process (signals, wallet alerts, daily
report) and the worker (the trading bot's notes), each with a short cache so a change applies within seconds.
Account and security notes (sign-ups, withdrawals, key reveals, watchdog) always go out.
"""

from __future__ import annotations

import time
from typing import Any

from app.models import TrackerCursor

KEY = "telegram:prefs"
CACHE_SECONDS = 10.0

# key, title, help
KINDS: list[tuple[str, str, str]] = [
    ("live_trades", "Bot buys and sells", "Every buy and sell the bot makes, with the transaction link"),
    ("live_skips", "Skipped buys", "A wallet bought but the bot did not (price ran away, limits, checks)"),
    ("live_info", "Position notes", "Ignored partial sells, hold-only tokens, hand trades found in the wallet"),
    ("live_errors", "Errors", "Failed buys or sells, not enough SOL"),
    ("wallet_buys", "Tracked wallets: buys", "\"Tracked wallet bought\" alerts"),
    ("wallet_sells", "Tracked wallets: sells", "\"Tracked wallet sold\" alerts"),
    ("signals", "Scanner signals", "Tokens the scanner scores high"),
    ("daily_report", "Daily report", "The daily performance summary"),
]
DEFAULTS: dict[str, bool] = {k: True for k, _, _ in KINDS}

_cache: dict[str, Any] = {"at": -1e9, "prefs": None}


def merge(raw: str | None, signals_default: bool = True) -> dict[str, bool]:
    """Nothing saved yet: all on (signals as TELEGRAM_SIGNAL_ALERTS). Saved: everything not listed as off is on."""
    if not raw or not raw.startswith("off="):
        return dict(DEFAULTS, signals=signals_default)
    off = {k for k in raw[4:].split(",") if k}
    return {k: k not in off for k in DEFAULTS}


def dump(prefs: dict[str, bool]) -> str:
    return "off=" + ",".join(k for k, _, _ in KINDS if not prefs.get(k, True))


async def get_prefs(session, signals_default: bool = True, fresh: bool = False) -> dict[str, bool]:
    if not fresh and _cache["prefs"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["prefs"]
    row = await session.get(TrackerCursor, KEY)
    prefs = merge(row.value if row is not None else None, signals_default)
    _cache.update(at=time.monotonic(), prefs=prefs)
    return prefs


async def set_prefs(session, changes: dict[str, Any], signals_default: bool = True) -> dict[str, bool]:
    prefs = await get_prefs(session, signals_default, fresh=True)
    prefs.update({k: bool(v) for k, v in changes.items() if k in DEFAULTS})
    row = await session.get(TrackerCursor, KEY)
    if row is None:
        session.add(TrackerCursor(key=KEY, value=dump(prefs)))
    else:
        row.value = dump(prefs)
    _cache.update(at=time.monotonic(), prefs=prefs)
    return prefs


def live_kind(text: str) -> str:
    """The trading bot's note -> its kind (the notes are written in app/live.py with these markers)."""
    if "✅ Bought" in text or "💰 Sold" in text:
        return "live_trades"
    if "⏭ Skipped" in text or "🛡 Skipped" in text:
        return "live_skips"
    if "❌" in text or "⚠️" in text:
        return "live_errors"
    return "live_info"
