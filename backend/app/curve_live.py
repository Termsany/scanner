"""Real-money curve trades (user's request 2026-11-23): a trading account can follow ONE curve rule from app/curve.py
(e.g. curve_fast34) with real buys and sells. The paper engine stays the signal source: the moment it decides to
enter a rule, it publishes a signal here; every live engine whose account follows that rule picks it up on its next
step (the publish wakes them) and buys at the account's entry size. What it buys is owner-style and hands-off
(no leader, no 2x rule): sold at the rule's exit level (as a coin price), at its stop (% from our own fill), or when
its max hold runs out.

Only rules with an exit level and a stop can be traded for real (custom / range rules: curve_fast19, curve_fast34,
curve_93_fast, curve_30_70_sl ...). Everything stays in this one process (the worker runs both engines).
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

REAL_TOKENS_START = 793_100_000.0          # same constants as app/curve.py (classic pump.fun curve)
V_TOKENS_START = 1_073_000_000.0


@dataclass(frozen=True)
class CurveSignal:
    id: int
    strategy: str
    mint: str
    symbol: str
    at: float               # monotonic time of the paper decision
    px_sol: float           # coin price (SOL) at the decision
    progress: float         # curve % sold at the decision
    target_ratio: float     # price at the rule's exit level / price now (classic curve)
    sl_pct: float           # stop, % from our own fill (negative)
    max_hold_s: float
    why: str


SIGNALS: deque[CurveSignal] = deque(maxlen=500)
_ids = itertools.count(1)


def price_ratio(from_pct: float, to_pct: float) -> float:
    """Price at `to_pct` sold / price at `from_pct` sold on a classic curve (price ~ 1 / virtual tokens^2)."""
    v = lambda pct: V_TOKENS_START - REAL_TOKENS_START * min(max(pct, 0.0), 100.0) / 100
    return (v(from_pct) / v(to_pct)) ** 2


def live_rules() -> dict[str, Any]:
    """Curve rules that can be traded for real: they need an exit level and a stop."""
    from app.curve import CURVE_RULES

    return {k: r for k, r in CURVE_RULES.items()
            if r.mode in ("custom", "range") and r.exit_at is not None and r.sl is not None}


def publish(strategy: str, rule: Any, mint: str, symbol: str, px_sol: float, progress: float, why: str,
            max_hold_s: float, now: float | None = None) -> CurveSignal | None:
    """Called by the paper engine when it decides to enter `strategy`. Wakes the live engines."""
    if strategy not in live_rules() or not px_sol:
        return None
    sig = CurveSignal(next(_ids), strategy, mint, symbol, time.monotonic() if now is None else now, px_sol,
                      progress, price_ratio(progress, rule.exit_at), float(rule.sl), float(max_hold_s), why)
    SIGNALS.append(sig)
    try:
        from app.fast_watch import WAKE

        for ev in list(WAKE):
            ev.set()
    except Exception:
        pass
    return sig


def take(after_id: int, strategy: str, max_age_s: float, now: float | None = None) -> tuple[list[CurveSignal], int]:
    """New signals for `strategy` after `after_id`, newest id seen; older than `max_age_s` are dropped (too late)."""
    now = time.monotonic() if now is None else now
    out, last = [], after_id
    for sig in list(SIGNALS):
        if sig.id <= after_id:
            continue
        last = max(last, sig.id)
        if sig.strategy == strategy and now - sig.at <= max_age_s:
            out.append(sig)
    return out, last


def last_id() -> int:
    return SIGNALS[-1].id if SIGNALS else 0
