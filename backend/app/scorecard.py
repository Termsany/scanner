"""Paper strategy scorecard: is a strategy's edge real enough to trade with money?

/paper shows running totals. This judges each strategy on its closed trades only (open ones have no result yet):

  expectancy     mean net P&L % per trade (what one more trade is worth, on average)
  median         the typical trade; far below the mean = a few big winners carry the strategy
  profit factor  gross wins / gross losses (> 1 makes money)
  ci95           95% bootstrap range of the expectancy. Its low end above 0 = the edge is unlikely to be luck
  max drawdown   worst peak-to-trough fall of cumulative P&L, trades in exit order
  loss streak    most losing trades in a row
  green days     share of UTC days that ended with positive P&L
  graduation     curve strategies: the same trades if, instead of the actual exit, the coin had been held to
                 1 / 5 / 10 min after graduation (grad_prices), with the same slippage

Verdict:  too_few (< MIN_TRADES)  ->  unreliable (too many restart / feed-lost exits)  ->
          edge (ci95 low > 0)  |  losing (ci95 high < 0)  |  unproven (the range spans 0)
"""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import GradPrice, PaperPosition
from app.repository import utc

MIN_TRADES = 30          # fewer closed trades than this: no verdict
BOOT_SAMPLES = 2000
GRAD_OFFSETS = ("60", "300", "600")   # seconds after graduation (curve.PaperCurve.GRAD_SAMPLE_S)


def _ci95(xs: list[float], seed: int = 7) -> tuple[float, float]:
    """95% bootstrap interval of the mean (seeded: the same trades always give the same range)."""
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choices(xs, k=n)) / n for _ in range(BOOT_SAMPLES))
    return means[int(BOOT_SAMPLES * 0.025)], means[int(BOOT_SAMPLES * 0.975) - 1]


def _max_drawdown(pnls_usd: list[float]) -> float:
    peak = cum = dd = 0.0
    for v in pnls_usd:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _loss_streak(pnls: list[float]) -> int:
    best = run = 0
    for v in pnls:
        run = run + 1 if v <= 0 else 0
        best = max(best, run)
    return best


def _trades_needed(xs: list[float]) -> int | None:
    """Rough closed-trade count at which today's average would clear the 95% bar (positive average only)."""
    mean = statistics.fmean(xs)
    if len(xs) < 2 or mean <= 0:
        return None
    sd = statistics.stdev(xs)
    return max(len(xs), int((1.96 * sd / mean) ** 2) + 1) if sd > 0 else len(xs)


def _grad_compare(trades: list[PaperPosition], grads: dict[str, GradPrice], slippage_pct: float) -> dict[str, Any] | None:
    from app.paper import pnl

    rows = []
    for t in trades:
        g = grads.get(t.token_address)
        if g is None or not t.entry_price or utc(g.grad_at) < utc(t.entry_time):
            continue
        hold = {}
        for o in GRAD_OFFSETS:
            px = (g.prices or {}).get(o)
            hold[o] = pnl(t.size_usd, t.entry_price, px, slippage_pct)[1] if px else None
        rows.append((t.pnl_pct or 0.0, hold))
    if not rows:
        return None
    out: dict[str, Any] = {"trades": len(rows), "actual_avg_pct": round(statistics.fmean(r[0] for r in rows), 2),
                           "hold_avg_pct": {}}
    for o in GRAD_OFFSETS:
        got = [(a, h[o]) for a, h in rows if h[o] is not None]
        out["hold_avg_pct"][o] = None if not got else {
            "trades": len(got),
            "actual": round(statistics.fmean(a for a, _ in got), 2),
            "held": round(statistics.fmean(h for _, h in got), 2),
        }
    return out


def score_strategy(closed: list[PaperPosition], grads: dict[str, GradPrice] | None = None,
                   slippage_pct: float = 0.0) -> dict[str, Any]:
    """Scorecard numbers for one strategy's closed trades."""
    from app.paper import INTERRUPTED, UNRELIABLE_INTERRUPTED

    trades = sorted(closed, key=lambda r: utc(r.exit_time) or utc(r.entry_time))
    n = len(trades)
    out: dict[str, Any] = {"trades": n}
    if not n:
        out["verdict"] = "too_few"
        return out
    pct = [t.pnl_pct or 0.0 for t in trades]
    usd = [t.pnl_usd or 0.0 for t in trades]
    wins = [p for p in pct if p > 0]
    losses = [p for p in pct if p <= 0]
    gross_win = sum(u for u in usd if u > 0)
    gross_loss = -sum(u for u in usd if u < 0)
    invested = sum(t.size_usd for t in trades)
    by_day: dict[str, float] = defaultdict(float)
    for t, u in zip(trades, usd):
        by_day[(utc(t.exit_time) or utc(t.entry_time)).date().isoformat()] += u
    interrupted = sum(t.exit_reason in INTERRUPTED for t in trades) / n
    lo, hi = _ci95(pct) if n >= 2 else (pct[0], pct[0])

    out.update({
        "win_rate": round(len(wins) / n, 3),
        "expectancy_pct": round(statistics.fmean(pct), 2),
        "median_pct": round(statistics.median(pct), 2),
        "avg_win_pct": round(statistics.fmean(wins), 2) if wins else None,
        "avg_loss_pct": round(statistics.fmean(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "total_usd": round(sum(usd), 2),
        "return_pct": round(sum(usd) / invested * 100, 2) if invested else None,
        "ci95_low_pct": round(lo, 2), "ci95_high_pct": round(hi, 2),
        "max_drawdown_usd": round(_max_drawdown(usd), 2),
        "loss_streak": _loss_streak(pct),
        "days": len(by_day), "green_days": sum(v > 0 for v in by_day.values()),
        "interrupted_pct": round(interrupted * 100, 1),
        "trades_needed": _trades_needed(pct) if lo <= 0 else None,
        "graduation": _grad_compare(trades, grads or {}, slippage_pct),
    })
    if n < MIN_TRADES:
        out["verdict"] = "too_few"
    elif interrupted > UNRELIABLE_INTERRUPTED:
        out["verdict"] = "unreliable"
    elif lo > 0:
        out["verdict"] = "edge"
    elif hi < 0:
        out["verdict"] = "losing"
    else:
        out["verdict"] = "unproven"
    return out


VERDICT_ORDER = {"edge": 0, "unproven": 1, "unreliable": 2, "too_few": 3, "losing": 4}


async def paper_scorecard(session: AsyncSession, hours: int = 24 * 30,
                          settings: Settings | None = None) -> dict[str, Any]:
    from app import paper_switches
    from app.paper import STRATEGIES, _custom_strategy, _now, custom_strategies, paper_resets

    s = settings or get_settings()
    since = _now() - timedelta(hours=hours)
    rows = (await session.execute(
        select(PaperPosition).where(PaperPosition.status == "closed", PaperPosition.entry_time >= since)
    )).scalars().all()
    resets = await paper_resets(session)
    rows = [r for r in rows if r.strategy not in resets or utc(r.entry_time) >= resets[r.strategy]]
    _, hidden = await paper_switches.read(session)

    mints = {r.token_address for r in rows if r.strategy.startswith(("curve", "cc"))}
    grads: dict[str, GradPrice] = {}
    if mints:
        for g in (await session.execute(select(GradPrice).where(GradPrice.mint.in_(mints)))).scalars():
            grads.setdefault(g.mint, g)

    by_strat: dict[str, list[PaperPosition]] = defaultdict(list)
    for r in rows:
        by_strat[r.strategy].append(r)
    out = []
    for strat in [*STRATEGIES.values(), *(_custom_strategy(c) for c in await custom_strategies(session))]:
        card = score_strategy(by_strat.get(strat.name, []), grads, s.paper_slippage_pct)
        if not card["trades"]:
            continue
        out.append({"name": strat.name, "title": strat.title, "hidden": strat.name in hidden, **card})
    out.sort(key=lambda c: (VERDICT_ORDER[c["verdict"]], -(c.get("expectancy_pct") or 0)))
    return {"hours": hours, "min_trades": MIN_TRADES, "generated_at": _now().isoformat(), "strategies": out}
