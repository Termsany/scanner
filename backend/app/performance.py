"""Signal performance: what each signal's price did 1h / 6h / 24h after it fired.

Prices come from GeckoTerminal 15-minute candles of the token's pool, not from the scanner's own snapshots: a
token that crashes drops out of the scanner's liquidity filters and stops being snapshotted, which would hide
exactly the losers (survivorship bias). One candle call covers 24h after a signal; each signal is measured at
most three times (when it is 1h, 6h and 24h old), spending at most PERF_MAX_CALLS_PER_RUN GeckoTerminal calls
per run (the free API allows 30/min, shared with the scanner).
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Signal, SignalOutcome, Token
from app.repository import utc

log = logging.getLogger("performance")

HORIZONS = (1, 6, 24)
CANDLE_MINUTES = 15
WIN_PEAK_PCT = 50.0            # "hit": price reached +50% at some point within 24h
MAX_ATTEMPTS = 6               # give up on a signal whose pool returns nothing after this many tries


def _pct(price: float | None, entry: float | None) -> float | None:
    if not price or not entry or entry <= 0:
        return None
    return round((price / entry - 1.0) * 100.0, 2)


def compute_outcome(entry: float, t0: int, candles: list[list[float]], now: int) -> dict[str, Any]:
    """Pure: returns at each reached horizon, peak / trough within 24h (or so far), minutes to peak.

    candles: [ts_start, open, high, low, close, ...] oldest first, CANDLE_MINUTES each.
    """
    step = CANDLE_MINUTES * 60
    end = min(now, t0 + 24 * 3600)
    window = [c for c in candles if c[0] + step > t0 and c[0] < end]
    out: dict[str, Any] = {"horizon_hours": 0}
    if not window or not entry:
        return out
    for h in HORIZONS:
        target = t0 + h * 3600
        if target > now:
            break
        at = [c for c in window if c[0] + step <= target]  # price at the horizon = close of the candle ending then
        if not at:
            break
        out[f"r_{h}h"] = _pct(float(at[-1][4]), entry)
        out["horizon_hours"] = h
    highs = [(float(c[2]), c[0]) for c in window]
    lows = [float(c[3]) for c in window]
    peak, peak_ts = max(highs)
    out["peak_pct"] = _pct(peak, entry)
    out["trough_pct"] = _pct(min(lows), entry)
    out["peak_minutes"] = max(0, int((peak_ts + step / 2 - t0) // 60))
    return out


def _due(o: SignalOutcome | None, age_h: float) -> bool:
    if o is None:
        return age_h >= 1
    if o.attempts >= MAX_ATTEMPTS and o.horizon_hours == 0:
        return False
    nxt = next((h for h in HORIZONS if h > o.horizon_hours), None)
    return nxt is not None and age_h >= nxt


async def update_outcomes(session: AsyncSession, gecko, settings: Settings | None = None) -> dict[str, int]:
    """Measure due signals (first signal per token per day, newest horizons first)."""
    s = settings or get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=26)
    rows = (await session.execute(
        select(Signal, Token).join(Token, Token.id == Signal.token_id).where(Signal.created_at >= since)
        .order_by(Signal.created_at.asc())
    )).all()
    have = {o.signal_id: o for o in (await session.execute(
        select(SignalOutcome).where(SignalOutcome.signal_id.in_([sig.id for sig, _ in rows] or [-1]))
    )).scalars()}
    due = [(sig, tok) for sig, tok in rows
           if tok.pair_address and _due(have.get(sig.id), (now - utc(sig.created_at)).total_seconds() / 3600)]
    # Oldest first: a signal about to pass 24h must not miss its last measurement.
    calls = measured = 0
    for sig, tok in due[: s.perf_max_calls_per_run]:
        t0 = int(utc(sig.created_at).timestamp())
        o = have.get(sig.id) or SignalOutcome(
            signal_id=sig.id, chain=tok.chain, symbol=tok.symbol, token_address=tok.address, score=sig.score,
            signal_time=utc(sig.created_at), entry_price=sig.price_usd, horizon_hours=0, attempts=0)
        if sig.id not in have:
            session.add(o)
        o.attempts += 1
        calls += 1
        try:
            candles = await gecko.ohlcv(tok.chain, tok.pair_address, aggregate_minutes=CANDLE_MINUTES,
                                        limit=110, before_ts=min(int(now.timestamp()), t0 + 25 * 3600))
        except Exception as exc:  # noqa: BLE001 - one pool failing must not stop the run
            o.error = f"{type(exc).__name__}: {exc}"[:160]
            continue
        res = compute_outcome(sig.price_usd or 0.0, t0, candles, int(now.timestamp()))
        if res["horizon_hours"] == 0 and not candles:
            o.error = "no candles for pool"
            continue
        for k, v in res.items():
            setattr(o, k, v)
        o.error = None
        measured += 1
    await session.flush()
    return {"due": len(due), "calls": calls, "measured": measured}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

BUCKETS = ((90, 101, "90+"), (80, 90, "80–89"), (75, 80, "75–79"), (0, 75, "<75"))


def _stats(rows: list[SignalOutcome]) -> dict[str, Any]:
    def med(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.median(vals), 1) if vals else None

    def share(vals, cond):
        vals = [v for v in vals if v is not None]
        return round(100.0 * sum(1 for v in vals if cond(v)) / len(vals), 1) if vals else None

    r24 = [o.r_24h for o in rows]
    return {
        "n": len(rows),
        "n_24h": sum(1 for v in r24 if v is not None),
        "median_1h": med([o.r_1h for o in rows]),
        "median_6h": med([o.r_6h for o in rows]),
        "median_24h": med(r24),
        "positive_6h_pct": share([o.r_6h for o in rows], lambda v: v > 0),
        "positive_24h_pct": share(r24, lambda v: v > 0),
        "hit_50_pct": share([o.peak_pct for o in rows], lambda v: v >= WIN_PEAK_PCT),
        "median_peak": med([o.peak_pct for o in rows]),
        "median_trough": med([o.trough_pct for o in rows]),
    }


async def performance_summary(session: AsyncSession, hours: int = 24 * 7, min_score: int = 0,
                              recent_limit: int = 100) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await session.execute(
        select(SignalOutcome).where(SignalOutcome.signal_time >= since, SignalOutcome.score >= min_score,
                                    SignalOutcome.horizon_hours > 0)
        .order_by(SignalOutcome.signal_time.desc())
    )).scalars().all()
    pending = (await session.execute(
        select(Signal.id).where(Signal.created_at >= since, Signal.score >= min_score)
    )).all()
    buckets = [{"label": label, **_stats([o for o in rows if lo <= o.score < hi])} for lo, hi, label in BUCKETS]
    chains: dict[str, list[SignalOutcome]] = {}
    for o in rows:
        chains.setdefault(o.chain.value, []).append(o)
    best = sorted((o for o in rows if o.peak_pct is not None), key=lambda o: -o.peak_pct)[:5]
    return {
        "window_hours": hours,
        "signals_in_window": len(pending),
        "measured": len(rows),
        "overall": _stats(rows),
        "by_score": [b for b in buckets if b["n"]],
        "by_chain": [{"chain": c, **_stats(v)} for c, v in sorted(chains.items())],
        "best": [{"symbol": o.symbol, "chain": o.chain.value, "score": o.score, "peak_pct": o.peak_pct,
                  "peak_minutes": o.peak_minutes, "token_address": o.token_address} for o in best],
        "recent": [{"symbol": o.symbol, "chain": o.chain.value, "token_address": o.token_address, "score": o.score,
                    "signal_time": utc(o.signal_time), "entry_price": o.entry_price, "r_1h": o.r_1h, "r_6h": o.r_6h,
                    "r_24h": o.r_24h, "peak_pct": o.peak_pct, "trough_pct": o.trough_pct,
                    "peak_minutes": o.peak_minutes} for o in rows[:recent_limit]],
    }


class PerformanceTracker:
    def __init__(self, gecko, settings: Settings | None = None) -> None:
        self.gecko = gecko
        self.s = settings or get_settings()

    async def run_forever(self) -> None:
        from app.db import SessionLocal

        log.info("Performance tracker started (every %ss, <= %d GeckoTerminal calls/run)",
                 self.s.perf_interval_seconds, self.s.perf_max_calls_per_run)
        while True:
            t0 = time.monotonic()
            try:
                async with SessionLocal() as session:
                    st = await update_outcomes(session, self.gecko, self.s)
                    await session.commit()
                if st["calls"]:
                    log.info("performance: %s", st)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("performance update failed")
            await asyncio.sleep(max(self.s.perf_interval_seconds - (time.monotonic() - t0), 10))
