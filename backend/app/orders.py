"""Price orders (user's request 2026-11-23): "buy this coin once it reaches a price, then sell at a price" -
like limit / stop orders on an exchange, for pump.fun / PumpSwap / any Solana coin.

Pump coins have no order book, so nothing waits on-chain: the worker watches the price (on-chain bonding-curve
state when app.pool_prices is on, otherwise Jupiter) and, once it crosses the trigger on two checks in a row
(one when far past), buys at market through the best route (PumpPortal or Jupiter, whichever is cheaper after
fees). The fill is capped at trigger + max_over_pct, so a coin that jumps far past the trigger is not chased:
the order then simply keeps waiting.

The position a filled order opens is owner-managed (hands_off: no leader, no 2x rule, no max hold); the order's
own sell price / stop price are set on it, so the existing target check sells it at those prices.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LivePosition, PriceOrder

log = logging.getLogger("orders")

ORDER = "order"          # leader_wallet of a position a price order opened
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def order_row(o: PriceOrder) -> dict[str, Any]:
    return {
        "id": o.id, "account": o.account, "mint": o.mint, "symbol": o.symbol, "condition": o.condition,
        "trigger_price": o.trigger_price, "amount_usd": o.amount_usd, "max_over_pct": o.max_over_pct,
        "then_target_price": o.then_target_price, "then_target_share": o.then_target_share,
        "then_stop_price": o.then_stop_price, "status": o.status, "last_price": o.last_price,
        "position_id": o.position_id, "note": o.note,
        "expires_at": _utc(o.expires_at).isoformat() if o.expires_at else None,
        "created_at": _utc(o.created_at).isoformat() if o.created_at else None,
        "done_at": _utc(o.done_at).isoformat() if o.done_at else None,
    }


def validate(mint: str, condition: str, trigger_price: float, amount_usd: float, max_over_pct: float,
             target_price: float | None, target_share: float | None, stop_price: float | None,
             expires_hours: float | None, price_now: float | None) -> str | None:
    """Why this order can't be accepted (None = fine)."""
    if not MINT_RE.match(mint or ""):
        return "That is not a Solana coin address"
    if condition not in ("below", "above"):
        return "Choose: buy when the price falls to the level, or rises to it"
    if not trigger_price or trigger_price <= 0:
        return "Set the price that triggers the buy"
    if not 1 <= amount_usd <= 10_000:
        return "Amount must be between $1 and $10,000"
    if not 0 <= max_over_pct <= 50:
        return "Max over the trigger must be between 0% and 50%"
    if target_price is not None and target_price <= trigger_price:
        return "The sell price must be above the trigger price"
    if stop_price is not None and stop_price >= trigger_price:
        return "The stop price must be below the trigger price"
    if target_share is not None and not 0.05 <= target_share <= 1.0:
        return "Sell share must be between 5% and 100%"
    if expires_hours is not None and not 0 < expires_hours <= 24 * 30:
        return "Expiry must be within 30 days"
    if price_now:
        # an order that would trigger right away is a market buy: say so rather than surprise the owner
        if condition == "below" and price_now <= trigger_price:
            return f"The price is already at or below that level (${price_now:.10g}): buy it now from the page instead"
        if condition == "above" and price_now >= trigger_price:
            return f"The price is already at or above that level (${price_now:.10g}): buy it now from the page instead"
    return None


async def create_order(session: AsyncSession, account: str, mode: str, mint: str, symbol: str | None,
                       condition: str, trigger_price: float, amount_usd: float, max_over_pct: float,
                       target_price: float | None, target_share: float | None, stop_price: float | None,
                       expires_hours: float | None, price_now: float | None) -> tuple[PriceOrder | None, str]:
    why = validate(mint, condition, trigger_price, amount_usd, max_over_pct, target_price, target_share,
                   stop_price, expires_hours, price_now)
    if why:
        return None, why
    o = PriceOrder(account=account, mode=mode, mint=mint, symbol=(symbol or mint[:6])[:60], condition=condition,
                   trigger_price=float(trigger_price), amount_usd=float(amount_usd), max_over_pct=float(max_over_pct),
                   then_target_price=target_price, then_target_share=(target_share or 1.0) if target_price else None,
                   then_stop_price=stop_price, status="open", last_price=price_now,
                   expires_at=_now() + timedelta(hours=expires_hours) if expires_hours else None)
    session.add(o)
    await session.flush()
    return o, "Order placed"


async def cancel_order(session: AsyncSession, order_id: int) -> tuple[bool, str]:
    o = await session.get(PriceOrder, order_id)
    if o is None or o.status != "open":
        return False, "That order is not open"
    o.status, o.done_at, o.note = "cancelled", _now(), "cancelled by the owner"
    return True, "Order cancelled"


# ---- worker side (called from CopyEngine._housekeeping) -------------------------------------------------
def crossed(o: PriceOrder, px: float) -> tuple[bool, bool]:
    """(triggered, far past it): far past needs one reading, otherwise two in a row."""
    if o.condition == "below":
        return px <= o.trigger_price, px <= o.trigger_price * 0.97
    return px >= o.trigger_price, px >= o.trigger_price * 1.03


async def check_orders(eng, session: AsyncSession) -> None:
    from app.live import ChaseError

    s = eng.s
    if not s.live_orders_enabled:
        return
    if time.monotonic() - eng._orders_at < s.live_orders_check_seconds:
        return
    eng._orders_at = time.monotonic()
    rows = (await session.execute(select(PriceOrder).where(
        PriceOrder.status == "open", PriceOrder.mode == eng.x.mode, PriceOrder.account == eng.a.name))).scalars().all()
    if not rows:
        return
    now = _now()
    for o in [o for o in rows if o.expires_at and _utc(o.expires_at) <= now]:
        o.status, o.done_at, o.note = "expired", now, "expired before the price got there"
        await eng.notify(f"{eng._tag()} ⌛ Price order #{o.id} for ${o.symbol} expired (never reached ${o.trigger_price:.10g})")
    await session.commit()
    rows = [o for o in rows if o.status == "open"]
    if not rows or not hasattr(eng.x, "token_prices"):
        return
    mints = sorted({o.mint for o in rows})
    prices: dict[str, dict[str, float]] = {}
    if hasattr(eng.x, "token_prices_fast"):
        try:
            prices = dict(await eng.x.token_prices_fast(mints) or {})
        except Exception as exc:
            log.warning("orders: prices failed: %s", exc)
    missing = [m for m in mints if m not in prices]
    if missing:
        try:
            prices.update(await eng.x.token_prices(missing) or {})
        except Exception as exc:
            log.warning("orders: backup prices failed: %s", exc)
    for o in rows:
        px = (prices.get(o.mint) or {}).get("usd")
        if not px:
            continue
        o.last_price = float(px)
        hit, far = crossed(o, float(px))
        if not hit:
            eng._order_seen.pop(o.id, None)
            continue
        eng._order_seen[o.id] = eng._order_seen.get(o.id, 0) + 1
        if eng._order_seen[o.id] < 2 and not far:
            continue
        eng._order_seen.pop(o.id, None)
        await _fill(eng, session, o, float(px), ChaseError)
    await session.commit()


async def _fill(eng, session: AsyncSession, o: PriceOrder, px: float, ChaseError) -> None:
    s, sym = eng.s, o.symbol or o.mint[:6]
    if eng.x.mode == "live":
        try:
            bal, sol_usd = await eng.x.sol_balance(), await eng.x.sol_price()
        except Exception as exc:
            log.warning("orders: balance check for #%s failed: %s", o.id, exc)
            return                                      # try again on the next check
        if bal - s.live_sol_reserve < o.amount_usd / sol_usd:
            o.status, o.done_at, o.note = "failed", _now(), f"not enough SOL ({bal:.3f}) when it triggered"
            await eng.notify(f"{eng._tag()} ❌ Price order #{o.id} for ${sym} triggered at ${px:.10g} but the wallet "
                             f"has only {bal:.3f} SOL")
            return
    max_price = o.trigger_price * (1 + o.max_over_pct / 100)
    try:
        f = await eng.x.buy(o.mint, o.amount_usd, max_price=max_price)
    except ChaseError as exc:
        # it jumped past trigger + max_over_pct before we could fill: like an unfilled limit order, keep waiting
        o.note = f"triggered but the fill would cost ${exc.pay:.10g} (> ${max_price:.10g}); still waiting"
        log.info("orders: #%s %s: %s", o.id, sym, o.note)
        return
    except Exception as exc:
        o.status, o.done_at, o.note = "failed", _now(), str(exc)[:300]
        log.warning("orders: #%s %s buy failed: %s", o.id, sym, exc)
        await eng.notify(f"{eng._tag()} ❌ Price order #{o.id} for ${sym}: buy failed: {str(exc)[:160]}")
        return
    pos = LivePosition(
        mode=eng.x.mode, account=eng.a.name, token_address=o.mint, symbol=sym[:60], leader_wallet=ORDER,
        leader_trade_id=0, status="open", cost_usd=f.usd, sol_spent=f.sol, tokens_raw=str(f.tokens_raw),
        tokens_bought_raw=str(f.tokens_raw), buy_signature=f.signature, hands_off=True,
        target_price=o.then_target_price, target_share=o.then_target_share if o.then_target_price else None,
        stop_price=o.then_stop_price,
        fills=[{"side": "buy", "sig": f.signature, "sol": f.sol, "tokens_raw": str(f.tokens_raw),
                "usd": round(f.usd, 2), "at": _now().isoformat(), "order": o.id,
                "timing": getattr(f, "timing", None) or {}}])
    session.add(pos)
    await session.flush()
    route = ((getattr(f, "timing", None) or {}).get("route") or "")
    o.status, o.done_at, o.position_id = "filled", _now(), pos.id
    o.note = f"bought at ~${px:.10g}" + (f" via {route}" if route else "")
    then = []
    if o.then_target_price:
        then.append(f"sell {round((o.then_target_share or 1) * 100)}% at ${o.then_target_price:.10g}")
    if o.then_stop_price:
        then.append(f"stop at ${o.then_stop_price:.10g}")
    log.info("orders: #%s %s filled: %.4f SOL ($%.2f) %s", o.id, sym, f.sol, f.usd, f.signature)
    await eng.notify(f"{eng._tag()} 🎯 Price order #{o.id} filled: bought ${sym} for {f.sol:.4f} SOL (${f.usd:,.2f})"
                     f" at ~${px:.10g}" + (f" via {route}" if route else "")
                     + (f"\nThen: {', '.join(then)}" if then else "") + f"\n{eng._tx(f.signature)}")
