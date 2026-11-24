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
    target_ratio: float | None   # price at the rule's exit level / price now (classic curve); None: no exit level
    sl_pct: float           # stop, % from our own fill (negative)
    max_hold_s: float
    why: str
    trail_pct: float | None = None   # rides graduation: from the graduation price, sell this % under the high
    grad_ratio: float | None = None  # price at graduation / price now (classic curve)


SIGNALS: deque[CurveSignal] = deque(maxlen=500)
_ids = itertools.count(1)


def price_ratio(from_pct: float, to_pct: float) -> float:
    """Price at `to_pct` sold / price at `from_pct` sold on a classic curve (price ~ 1 / virtual tokens^2)."""
    v = lambda pct: V_TOKENS_START - REAL_TOKENS_START * min(max(pct, 0.0), 100.0) / 100
    return (v(from_pct) / v(to_pct)) ** 2


def live_rules() -> dict[str, Any]:
    """Curve rules that can be traded for real: they need a stop and an exit (an exit level, or riding the
    graduation on a trailing stop)."""
    from app.curve import CURVE_RULES

    return {k: r for k, r in CURVE_RULES.items()
            if r.mode in ("custom", "range") and r.sl is not None
            and (r.exit_at is not None or getattr(r, "grad_trail", None) is not None)
            and getattr(r, "sl_after_s", None) is None            # a later stop isn't done for real yet
            and not getattr(r, "paper_only", False)}


def publish(strategy: str, rule: Any, mint: str, symbol: str, px_sol: float, progress: float, why: str,
            max_hold_s: float, now: float | None = None) -> CurveSignal | None:
    """Called by the paper engine when it decides to enter `strategy`. Wakes the live engines."""
    if strategy not in live_rules() or not px_sol:
        return None
    sig = CurveSignal(next(_ids), strategy, mint, symbol, time.monotonic() if now is None else now, px_sol,
                      progress, price_ratio(progress, rule.exit_at) if rule.exit_at is not None else None,
                      float(rule.sl), float(max_hold_s), why,
                      trail_pct=getattr(rule, "grad_trail", None), grad_ratio=price_ratio(progress, 100.0))
    SIGNALS.append(sig)
    try:
        from app.fast_watch import WAKE

        for ev in list(WAKE):
            ev.set()
    except Exception:
        pass
    return sig


def take(after_id: int, strategy: str | set[str], max_age_s: float,
         now: float | None = None) -> tuple[list[CurveSignal], int]:
    """New signals for `strategy` (one rule or a set) after `after_id`, newest id seen; older than `max_age_s`
    are dropped (too late)."""
    now = time.monotonic() if now is None else now
    want = {strategy} if isinstance(strategy, str) else strategy
    out, last = [], after_id
    for sig in list(SIGNALS):
        if sig.id <= after_id:
            continue
        last = max(last, sig.id)
        if sig.strategy in want and now - sig.at <= max_age_s:
            out.append(sig)
    return out, last


def last_id() -> int:
    return SIGNALS[-1].id if SIGNALS else 0


# ---- exits of real curve positions on the paper engine's own curve price (2026-11-23: stops were 7-10 s late) --
# The paper engine reads every bonding-curve change of the coins it watches (Helius). For coins a live account
# holds, it hands the price here and wakes the live engines the moment it crosses a stop or an exit level, so
# the sell starts on that update, not on the next Jupiter price check (every 3 s, two readings in a row).
PRICES: dict[str, tuple[float, float]] = {}                  # mint -> (SOL per token, monotonic time)
EXITS: dict[str, dict[str, tuple[float | None, float | None]]] = {}   # mint -> {account: (stop, target) in SOL}
_crossed: set[str] = set()


def watch_exit(mint: str, account: str, stop_sol: float | None, target_sol: float | None) -> None:
    EXITS.setdefault(mint, {})[account] = (stop_sol, target_sol)


def unwatch(account: str, keep: set[str]) -> None:
    """Drop this account's lines on coins it no longer holds."""
    for mint in list(EXITS):
        if mint not in keep and account in EXITS[mint]:
            del EXITS[mint][account]
            if not EXITS[mint]:
                del EXITS[mint]
                PRICES.pop(mint, None)
                _crossed.discard(mint)


def crossed(mint: str, px: float) -> bool:
    return any((stop is not None and px <= stop) or (target is not None and px >= target)
               for stop, target in EXITS.get(mint, {}).values())


def on_price(mint: str, px_sol: float | None, graduated: bool = False, now: float | None = None) -> None:
    """Called by the paper engine on every curve update of a coin in EXITS."""
    if mint not in EXITS:
        return
    if graduated or not px_sol:
        PRICES.pop(mint, None)           # off the curve: the normal price checks take over
        return
    PRICES[mint] = (px_sol, time.monotonic() if now is None else now)
    if crossed(mint, px_sol):
        if mint not in _crossed:         # wake once per crossing, not on every update while it sells
            _crossed.add(mint)
            _wake()
    else:
        _crossed.discard(mint)


def price(mint: str, max_age_s: float = 120.0, now: float | None = None) -> float | None:
    """The coin's curve price (SOL per token) if the paper engine saw it lately. A curve only changes with a
    trade, so a quiet coin keeps its price; the age limit only guards against a dead feed."""
    got = PRICES.get(mint)
    now = time.monotonic() if now is None else now
    return got[0] if got and now - got[1] <= max_age_s else None


def _wake() -> None:
    try:
        from app.fast_watch import WAKE

        for ev in list(WAKE):
            ev.set()
    except Exception:
        pass
