"""Alpha Score: a deterministic, explainable 0-100 score.

    Alpha = Security (35) + Momentum (35) + Smart Money (30)

Design rules
------------
* Pure function of its inputs: same MarketMetrics + SecurityReport + SmartMoneyInfo
  always yield the same ScoreBreakdown (except `scored_at`). No I/O here.
* Unknown data never earns points. A missing field scores 0 for that component and
  is listed in `data_gaps`, so an unaudited token cannot look safe by omission.
* Hard fails override everything: a honeypot (or an effective honeypot) scores 0.

Component formulas (all thresholds are module constants below)
--------------------------------------------------------------
SECURITY (max 35)
  S1  Contract safety (15):  15 if honeypot == False AND mintable == False AND freezable == False
                             AND buy/sell tax <= MAX_TAX_PCT AND no owner-balance/hidden-owner/
                             sell-restriction flags; else 0.
  S2  LP locked (10):        10 if lp_locked_pct > 80 ; else 0.
  S3  Holder concentration:  10 if top10_holders_pct < 30 (LP / burn / lock accounts excluded); else 0.

MOMENTUM (max 35)
  M1  Vol/Liq 1h (15):       r = volume_h1 / liquidity_usd
                             r <= 0.05              -> 0
                             0.05 < r < 0.25        -> 15 * (r - 0.05) / 0.20          (ramp up)
                             0.25 <= r <= 2.0       -> 15                              (optimal band)
                             2.0 < r < 6.0          -> 15 * (6.0 - r) / 4.0            (wash-trade decay)
                             r >= 6.0               -> 0
  M2  Buy pressure (10):     requires buys_h1 + sells_h1 >= 20;
                             sells == 0 -> 0 (one-way flow is a honeypot hallmark);
                             buys / sells > 1.4 -> 10 ; else 0.
  M3  Price action (10):     pump  = h1 > 300% OR m5 > 40%
                             dump  = h1 >= 50% AND m5 <= -10%   (pump-and-dump divergence)
                             pump or dump           -> 0
                             m5 > 0 AND h1 > 0      -> 10
                             h1 > 0 AND -5 <= m5 <= 0 -> 5    (healthy consolidation)
                             otherwise              -> 0

SMART MONEY & DISTRIBUTION (max 30)
  W1  Smart wallets (15):    >= 1 tracked smart wallet among holders -> 15
                             else heuristic (flagged `inferred`): avg trade size h1
                             a = volume_h1 / (buys_h1 + sells_h1)
                             a >= 2,000 USD AND buys/sells >= 1.2 -> 7
                             a >= 1,000 USD                       -> 4
                             else 0
  W2  MCap/Liq (15):         m = market_cap (FDV fallback) / liquidity_usd
                             m < 1                  -> 0     (inconsistent / mostly-pooled supply)
                             1 <= m < 3             -> 15 * (m - 1) / 2
                             3 <= m <= 25           -> 15    (healthy)
                             25 < m < 60            -> 15 * (60 - m) / 35
                             m >= 60                -> 0     (valuation unsupported by liquidity)

HARD FAILS -> total = 0
  * is_honeypot == True
  * cannot_sell_all == True
  * sell_tax >= 50%
  * a 'danger' risk named "Rugged" (RugCheck)

BADGES:  80-100 STRONG_ALPHA (green) | 60-79 MODERATE (amber) | <60 HIGH_RISK (red)
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from app.models import Badge
from app.schemas import (
    CategoryScore,
    MarketMetrics,
    ScoreBreakdown,
    ScoreComponent,
    SecurityReport,
    SmartMoneyInfo,
)

SCORE_VERSION = "1.0.0"

# --- Security ---------------------------------------------------------------
CONTRACT_SAFETY_PTS = 15.0
LP_LOCK_PTS = 10.0
HOLDER_PTS = 10.0
LP_LOCK_MIN_PCT = 80.0
TOP10_MAX_PCT = 30.0
MAX_TAX_PCT = 10.0
HONEYPOT_SELL_TAX_PCT = 50.0

# --- Momentum ---------------------------------------------------------------
VOL_LIQ_PTS = 15.0
VOL_LIQ_FLOOR = 0.05
VOL_LIQ_OPT_LOW = 0.25
VOL_LIQ_OPT_HIGH = 2.0
VOL_LIQ_CEIL = 6.0

BUY_PRESSURE_PTS = 10.0
BUY_SELL_MIN_RATIO = 1.4
MIN_TXNS_H1 = 20

PRICE_ACTION_PTS = 10.0
PRICE_ACTION_PARTIAL_PTS = 5.0
PUMP_H1_PCT = 300.0
PUMP_M5_PCT = 40.0
DUMP_H1_PCT = 50.0
DUMP_M5_PCT = -10.0
CONSOLIDATION_M5_PCT = -5.0

# --- Smart money ------------------------------------------------------------
SMART_WALLET_PTS = 15.0
HEURISTIC_STRONG_PTS = 7.0
HEURISTIC_WEAK_PTS = 4.0
HEURISTIC_STRONG_AVG_USD = 2_000.0
HEURISTIC_WEAK_AVG_USD = 1_000.0
HEURISTIC_MIN_BUY_RATIO = 1.2

MCAP_LIQ_PTS = 15.0
MCAP_LIQ_FLOOR = 1.0
MCAP_LIQ_OPT_LOW = 3.0
MCAP_LIQ_OPT_HIGH = 25.0
MCAP_LIQ_CEIL = 60.0

STRONG_ALPHA_MIN = 80
MODERATE_MIN = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _num(value: float | int | None) -> float | None:
    """Coerce to a finite float; None/NaN/inf -> None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ramp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation of x in [x0, x1] onto [y0, y1], clamped."""
    if x1 == x0:
        return y1
    t = min(max((x - x0) / (x1 - x0), 0.0), 1.0)
    return y0 + t * (y1 - y0)


def _band_score(x: float, floor: float, opt_low: float, opt_high: float, ceil: float, max_pts: float) -> float:
    """Trapezoid: 0 at/below floor, ramps to max at opt_low, flat to opt_high, decays to 0 at ceil."""
    if x <= floor or x >= ceil:
        return 0.0
    if x < opt_low:
        return _ramp(x, floor, opt_low, 0.0, max_pts)
    if x <= opt_high:
        return max_pts
    return _ramp(x, opt_high, ceil, max_pts, 0.0)


def _r(x: float) -> float:
    return round(x, 2)


def classify(total: int) -> Badge:
    if total >= STRONG_ALPHA_MIN:
        return Badge.strong_alpha
    if total >= MODERATE_MIN:
        return Badge.moderate
    return Badge.high_risk


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def _hard_fail(sec: SecurityReport | None) -> str | None:
    if sec is None:
        return None
    if sec.is_honeypot is True:
        return "HONEYPOT"
    if sec.cannot_sell_all is True:
        return "CANNOT_SELL_ALL"
    if sec.sell_tax is not None and sec.sell_tax >= HONEYPOT_SELL_TAX_PCT:
        return "PROHIBITIVE_SELL_TAX"
    if any(r.name.lower() == "rugged" and r.level == "danger" for r in sec.risks):
        return "RUGGED"
    return None


def score_security(sec: SecurityReport | None, gaps: list[str]) -> CategoryScore:
    comps: list[ScoreComponent] = []

    # S1 contract safety
    if sec is None or not sec.has_data:
        gaps.append("security_report")
        comps.append(ScoreComponent(key="contract_safety", label="No honeypot / mint / freeze",
                                    points=0, max_points=CONTRACT_SAFETY_PTS, passed=False,
                                    detail="No security data available"))
    else:
        problems: list[str] = []
        unknown: list[str] = []
        for field, label in (("is_honeypot", "honeypot"), ("is_mintable", "mint authority"),
                             ("is_freezable", "freeze authority")):
            v = getattr(sec, field)
            if v is None:
                unknown.append(label)
            elif v:
                problems.append(label)
        for field, label in (("owner_can_change_balance", "owner can change balances"),
                             ("hidden_owner", "hidden owner"),
                             ("transfer_pausable", "transfers pausable")):
            if getattr(sec, field) is True:
                problems.append(label)
        for tax_name, tax in (("buy tax", sec.buy_tax), ("sell tax", sec.sell_tax)):
            if tax is not None and tax > MAX_TAX_PCT:
                problems.append(f"{tax_name} {tax:.1f}%")
        for u in unknown:
            gaps.append(f"security.{u.replace(' ', '_')}")
        passed = not problems and not unknown
        if passed:
            detail = "Not a honeypot, mint and freeze renounced, taxes within limits"
        elif problems:
            detail = "Flags: " + ", ".join(problems)
        else:
            detail = "Unknown: " + ", ".join(unknown)
        comps.append(ScoreComponent(key="contract_safety", label="No honeypot / mint / freeze",
                                    points=CONTRACT_SAFETY_PTS if passed else 0,
                                    max_points=CONTRACT_SAFETY_PTS, passed=passed, detail=detail))

    # S2 LP lock
    lp = _num(sec.lp_locked_pct) if sec else None
    if lp is None:
        gaps.append("security.lp_locked_pct")
        comps.append(ScoreComponent(key="lp_locked", label="LP locked/burned > 80%", points=0,
                                    max_points=LP_LOCK_PTS, passed=False, detail="LP lock status unknown"))
    else:
        ok = lp > LP_LOCK_MIN_PCT
        comps.append(ScoreComponent(key="lp_locked", label="LP locked/burned > 80%",
                                    points=LP_LOCK_PTS if ok else 0, max_points=LP_LOCK_PTS, passed=ok,
                                    value=_r(lp), detail=f"{lp:.1f}% of LP locked or burned"))

    # S3 holder concentration
    t10 = _num(sec.top10_holders_pct) if sec else None
    if t10 is None:
        gaps.append("security.top10_holders_pct")
        comps.append(ScoreComponent(key="top10_holders", label="Top 10 holders < 30%", points=0,
                                    max_points=HOLDER_PTS, passed=False, detail="Holder distribution unknown"))
    else:
        ok = t10 < TOP10_MAX_PCT
        comps.append(ScoreComponent(key="top10_holders", label="Top 10 holders < 30%",
                                    points=HOLDER_PTS if ok else 0, max_points=HOLDER_PTS, passed=ok,
                                    value=_r(t10), detail=f"Top 10 wallets hold {t10:.1f}% (LP/burn excluded)"))

    return CategoryScore(name="Security", points=_r(sum(c.points for c in comps)),
                         max_points=CONTRACT_SAFETY_PTS + LP_LOCK_PTS + HOLDER_PTS, components=comps)


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def score_momentum(m: MarketMetrics, gaps: list[str]) -> CategoryScore:
    comps: list[ScoreComponent] = []

    # M1 volume / liquidity
    vol, liq = _num(m.volume_h1), _num(m.liquidity_usd)
    if vol is None or liq is None or liq <= 0:
        gaps.append("market.volume_h1_or_liquidity")
        comps.append(ScoreComponent(key="vol_liq_ratio", label="1h Volume / Liquidity in optimal range",
                                    points=0, max_points=VOL_LIQ_PTS, passed=False, detail="Missing volume or liquidity"))
    else:
        r = vol / liq
        pts = _band_score(r, VOL_LIQ_FLOOR, VOL_LIQ_OPT_LOW, VOL_LIQ_OPT_HIGH, VOL_LIQ_CEIL, VOL_LIQ_PTS)
        if r >= VOL_LIQ_CEIL:
            detail = f"Ratio {r:.2f}x - likely wash trading"
        elif r > VOL_LIQ_OPT_HIGH:
            detail = f"Ratio {r:.2f}x - overheated"
        elif r >= VOL_LIQ_OPT_LOW:
            detail = f"Ratio {r:.2f}x - optimal"
        else:
            detail = f"Ratio {r:.2f}x - thin activity"
        comps.append(ScoreComponent(key="vol_liq_ratio", label="1h Volume / Liquidity in optimal range",
                                    points=_r(pts), max_points=VOL_LIQ_PTS,
                                    passed=VOL_LIQ_OPT_LOW <= r <= VOL_LIQ_OPT_HIGH, value=_r(r), detail=detail))

    # M2 buy pressure
    buys, sells = m.buys_h1, m.sells_h1
    if buys is None or sells is None:
        gaps.append("market.txns_h1")
        comps.append(ScoreComponent(key="buy_sell_ratio", label="Buy/Sell ratio > 1.4 (1h)", points=0,
                                    max_points=BUY_PRESSURE_PTS, passed=False, detail="Missing 1h transaction counts"))
    else:
        total = buys + sells
        if total < MIN_TXNS_H1:
            comps.append(ScoreComponent(key="buy_sell_ratio", label="Buy/Sell ratio > 1.4 (1h)", points=0,
                                        max_points=BUY_PRESSURE_PTS, passed=False, value=total,
                                        detail=f"Only {total} txns in 1h (min {MIN_TXNS_H1})"))
        elif sells == 0:
            comps.append(ScoreComponent(key="buy_sell_ratio", label="Buy/Sell ratio > 1.4 (1h)", points=0,
                                        max_points=BUY_PRESSURE_PTS, passed=False, value="inf",
                                        detail=f"{buys} buys and zero sells - possible sell restriction"))
        else:
            ratio = buys / sells
            ok = ratio > BUY_SELL_MIN_RATIO
            comps.append(ScoreComponent(key="buy_sell_ratio", label="Buy/Sell ratio > 1.4 (1h)",
                                        points=BUY_PRESSURE_PTS if ok else 0, max_points=BUY_PRESSURE_PTS,
                                        passed=ok, value=_r(ratio), detail=f"{buys} buys / {sells} sells = {ratio:.2f}"))

    # M3 price action
    m5, h1 = _num(m.price_change_m5), _num(m.price_change_h1)
    label = "Positive 5m & 1h trend, no pump/dump divergence"
    if m5 is None or h1 is None:
        gaps.append("market.price_change")
        comps.append(ScoreComponent(key="price_action", label=label, points=0, max_points=PRICE_ACTION_PTS,
                                    passed=False, detail="Missing price change data"))
    else:
        val = f"5m {m5:+.1f}% / 1h {h1:+.1f}%"
        if h1 > PUMP_H1_PCT or m5 > PUMP_M5_PCT:
            pts, ok, detail = 0.0, False, f"{val} - vertical pump, chase risk"
        elif h1 >= DUMP_H1_PCT and m5 <= DUMP_M5_PCT:
            pts, ok, detail = 0.0, False, f"{val} - pump-and-dump divergence"
        elif m5 > 0 and h1 > 0:
            pts, ok, detail = PRICE_ACTION_PTS, True, f"{val} - aligned uptrend"
        elif h1 > 0 and CONSOLIDATION_M5_PCT <= m5 <= 0:
            pts, ok, detail = PRICE_ACTION_PARTIAL_PTS, False, f"{val} - consolidating"
        else:
            pts, ok, detail = 0.0, False, f"{val} - downtrend"
        comps.append(ScoreComponent(key="price_action", label=label, points=pts, max_points=PRICE_ACTION_PTS,
                                    passed=ok, value=val, detail=detail))

    return CategoryScore(name="Momentum", points=_r(sum(c.points for c in comps)),
                         max_points=VOL_LIQ_PTS + BUY_PRESSURE_PTS + PRICE_ACTION_PTS, components=comps)


# ---------------------------------------------------------------------------
# Smart money & distribution
# ---------------------------------------------------------------------------


def score_smart_money(m: MarketMetrics, sm: SmartMoneyInfo | None, gaps: list[str]) -> CategoryScore:
    comps: list[ScoreComponent] = []
    sm = sm or SmartMoneyInfo()

    # W1 smart wallets
    label = "Smart wallet interaction"
    if sm.tracked_wallet_hits:
        hits = sm.tracked_wallet_hits
        comps.append(ScoreComponent(key="smart_wallets", label=label, points=SMART_WALLET_PTS,
                                    max_points=SMART_WALLET_PTS, passed=True, value=len(hits),
                                    detail=f"Tracked wallets holding: {', '.join(hits[:5])}"))
    else:
        vol = _num(m.volume_h1)
        buys, sells = m.buys_h1 or 0, m.sells_h1 or 0
        n = buys + sells
        if vol is None or n == 0:
            comps.append(ScoreComponent(key="smart_wallets", label=label, points=0, max_points=SMART_WALLET_PTS,
                                        passed=False, detail="No tracked wallets; not enough flow to infer"))
        else:
            avg = vol / n
            buy_ratio = buys / sells if sells else 0.0  # zero sells gets no credit (see M2)
            if avg >= HEURISTIC_STRONG_AVG_USD and buy_ratio >= HEURISTIC_MIN_BUY_RATIO:
                pts = HEURISTIC_STRONG_PTS
            elif avg >= HEURISTIC_WEAK_AVG_USD:
                pts = HEURISTIC_WEAK_PTS
            else:
                pts = 0.0
            comps.append(ScoreComponent(key="smart_wallets", label=label, points=pts, max_points=SMART_WALLET_PTS,
                                        passed=False, value=_r(avg),
                                        detail=f"Inferred (no tracked wallet): avg 1h trade ${avg:,.0f}"))

    # W2 mcap / liquidity
    mc, liq = _num(m.effective_mcap), _num(m.liquidity_usd)
    label = "Healthy Market Cap / Liquidity"
    if mc is None or liq is None or liq <= 0 or mc <= 0:
        gaps.append("market.market_cap_or_liquidity")
        comps.append(ScoreComponent(key="mcap_liq_ratio", label=label, points=0, max_points=MCAP_LIQ_PTS,
                                    passed=False, detail="Missing market cap or liquidity"))
    else:
        ratio = mc / liq
        if ratio < MCAP_LIQ_FLOOR:
            pts = 0.0
        else:
            pts = _band_score(ratio, MCAP_LIQ_FLOOR, MCAP_LIQ_OPT_LOW, MCAP_LIQ_OPT_HIGH, MCAP_LIQ_CEIL, MCAP_LIQ_PTS)
        if ratio < MCAP_LIQ_FLOOR:
            detail = f"{ratio:.2f}x - liquidity exceeds market cap (inconsistent data)"
        elif ratio >= MCAP_LIQ_CEIL:
            detail = f"{ratio:.1f}x - valuation far exceeds liquidity"
        elif MCAP_LIQ_OPT_LOW <= ratio <= MCAP_LIQ_OPT_HIGH:
            detail = f"{ratio:.1f}x - healthy"
        else:
            detail = f"{ratio:.1f}x - outside healthy 3-25x band"
        comps.append(ScoreComponent(key="mcap_liq_ratio", label=label, points=_r(pts), max_points=MCAP_LIQ_PTS,
                                    passed=MCAP_LIQ_OPT_LOW <= ratio <= MCAP_LIQ_OPT_HIGH, value=_r(ratio),
                                    detail=detail))

    return CategoryScore(name="Smart Money & Distribution", points=_r(sum(c.points for c in comps)),
                         max_points=SMART_WALLET_PTS + MCAP_LIQ_PTS, components=comps)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_alpha_score(
    metrics: MarketMetrics,
    security: SecurityReport | None,
    smart_money: SmartMoneyInfo | None = None,
) -> ScoreBreakdown:
    gaps: list[str] = []
    sec = score_security(security, gaps)
    mom = score_momentum(metrics, gaps)
    smt = score_smart_money(metrics, smart_money, gaps)

    hard_fail = _hard_fail(security)
    raw_total = sec.points + mom.points + smt.points
    total = 0 if hard_fail else int(min(max(round(raw_total), 0), 100))

    return ScoreBreakdown(
        version=SCORE_VERSION,
        total=total,
        badge=classify(total),
        security=sec,
        momentum=mom,
        smart_money=smt,
        hard_fail=hard_fail,
        data_gaps=sorted(set(gaps)),
    )


def top_reasons(breakdown: ScoreBreakdown, limit: int = 3) -> list[str]:
    """Highest-value passed components, used in signal messages."""
    comps: Iterable[ScoreComponent] = (
        *breakdown.security.components,
        *breakdown.momentum.components,
        *breakdown.smart_money.components,
    )
    ranked = sorted((c for c in comps if c.points > 0), key=lambda c: c.points, reverse=True)
    return [c.detail or c.label for c in ranked[:limit]]
