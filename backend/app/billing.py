"""Monthly billing of users' accounts: a fixed fee + a share of the month's profit above a high-water mark.

How it works
  * billing_anchor = start of the account's next unbilled period. It starts `trial_days` after the account was
    made, and never before billing was turned on (turning it on never bills past months). Each period is one
    calendar month.
  * At the end of each period a Bill is written: fee + profit_pct x (cumulative realized P&L - high-water mark),
    the share never below 0. The high-water mark then rises to the new cumulative P&L, so a losing month is never
    charged again later: the user pays a share only once they are above their best charged level.
    Profit made during the trial counts as the starting level (never charged).
  * The worker collects due bills in SOL from the account's own wallet to the admin wallet, keeping the fee
    reserve (and 0.01 SOL for open positions' sells). Not enough SOL: it retries every hour and tells the user;
    still unpaid `grace_days` after the period ended: the account stops new buys (sells and withdrawals keep
    working) until the bill is paid or waived.
  * A transfer that errors after it was sent is marked "check" and never retried automatically, so a user is
    never charged twice; the admin looks it up on Solscan and marks it paid or due again.
  * Off until the admin turns it on (and sets the receiving wallet) on /admin; only in LIVE_MODE=live.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Bill, LivePosition, TrackerCursor, TradingAccount, User

log = logging.getLogger("billing")

KEYS = ("enabled", "fee_usd", "profit_pct", "trial_days", "grace_days", "wallet")
RETRY_EVERY = timedelta(hours=1)
FEE_LAMPORTS = 5_000
KEEP_WITH_OPEN = 10_000_000          # 0.01 SOL stays while positions are open: their sells need fees
LAMPORTS = 1_000_000_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def add_months(d: datetime, n: int) -> datetime:
    """Same day and time n months later (the 31st becomes the month's last day)."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


# ---- config (admin-editable, stored as small key/value rows) ----------------------
async def get_config(session: AsyncSession, s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    cfg: dict[str, Any] = {"enabled": False, "fee_usd": s.billing_fee_usd, "profit_pct": s.billing_profit_pct,
                           "trial_days": s.billing_trial_days, "grace_days": s.billing_grace_days, "wallet": ""}
    rows = (await session.execute(select(TrackerCursor).where(TrackerCursor.key.like("billing:%")))).scalars().all()
    for r in rows:
        k = r.key.split(":", 1)[1]
        if k == "enabled":
            cfg[k] = r.value == "1"
        elif k == "wallet":
            cfg[k] = r.value
        elif k in ("fee_usd", "profit_pct"):
            cfg[k] = float(r.value)
        elif k in ("trial_days", "grace_days"):
            cfg[k] = int(r.value)
    return cfg


class BillingError(ValueError):
    pass


async def set_config(session: AsyncSession, changes: dict[str, Any]) -> dict[str, Any]:
    from app.accounts import WALLET_RE

    cfg = await get_config(session)
    new = {**cfg, **{k: v for k, v in changes.items() if k in KEYS and v is not None}}
    if not 0 <= float(new["fee_usd"]) <= 10_000:
        raise BillingError("monthly fee must be between $0 and $10,000")
    if not 0 <= float(new["profit_pct"]) <= 50:
        raise BillingError("profit share must be between 0% and 50%")
    if not 0 <= int(new["trial_days"]) <= 90 or not 0 <= int(new["grace_days"]) <= 30:
        raise BillingError("trial: 0-90 days, grace: 0-30 days")
    new["wallet"] = str(new["wallet"] or "").strip()
    if new["wallet"] and not WALLET_RE.fullmatch(new["wallet"]):
        raise BillingError("receiving wallet is not a Solana address")
    if new["enabled"] and not new["wallet"]:
        raise BillingError("set the wallet that receives the payments first")
    if new["enabled"] and not cfg["enabled"]:
        # turning it on: periods start now (or when the trial ends), never in the past
        now = _now()
        for row in await _user_accounts(session):
            if row.billing_anchor is None or _utc(row.billing_anchor) < now:
                row.billing_anchor = max((_utc(row.created_at) or now) + timedelta(days=int(new["trial_days"])), now)
            if row.billing_hwm_usd is not None:
                # profit made while billing was off is never charged; a drawdown still has to be won back first
                cum = await cum_pnl(session, row.name, get_settings().live_mode, now)
                row.billing_hwm_usd = max(row.billing_hwm_usd, cum)
    for k in KEYS:
        v = new[k]
        val = ("1" if v else "0") if k == "enabled" else str(v)
        row = await session.get(TrackerCursor, f"billing:{k}")
        if row is None:
            session.add(TrackerCursor(key=f"billing:{k}", value=val))
        else:
            row.value = val
    return new


# ---- numbers ---------------------------------------------------------------------
async def cum_pnl(session: AsyncSession, account: str, mode: str, before: datetime) -> float:
    v = (await session.execute(select(func.sum(LivePosition.pnl_usd)).where(
        LivePosition.account == account, LivePosition.mode == mode, LivePosition.status == "closed",
        LivePosition.closed_at < before))).scalar()
    return float(v or 0.0)


def anchor_of(row: TradingAccount, cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    if row.billing_anchor:
        return _utc(row.billing_anchor)
    return max((_utc(row.created_at) or _now()) + timedelta(days=int(cfg["trial_days"])), now or _now())


def charge(fee: float, pct: float, cum: float, hwm: float) -> tuple[float, float, float]:
    """(share, total, new high-water mark) for a period ending at cumulative P&L `cum`."""
    share = round(max(0.0, cum - hwm) * pct / 100, 2)
    return share, round(fee + share, 2), max(hwm, cum)


async def _user_accounts(session: AsyncSession) -> list[TradingAccount]:
    return list((await session.execute(select(TradingAccount).where(TradingAccount.user_id.is_not(None))
                                        .order_by(TradingAccount.id))).scalars().all())


async def create_bills(session: AsyncSession, cfg: dict[str, Any], mode: str, now: datetime) -> list[Bill]:
    """Write a bill for every period that has ended (catching up if the worker was down)."""
    made = []
    for row in await _user_accounts(session):
        if not row.enabled:
            continue
        start = anchor_of(row, cfg, now)
        hwm = row.billing_hwm_usd
        if hwm is None:
            hwm = await cum_pnl(session, row.name, mode, start)      # profit before billing: the starting level
        n = 0
        while add_months(start, 1) <= now and n < 120:
            end = add_months(start, 1)
            cum = await cum_pnl(session, row.name, mode, end)
            share, total, new_hwm = charge(float(cfg["fee_usd"]), float(cfg["profit_pct"]), cum, hwm)
            b = Bill(account=row.name, user_id=row.user_id, period_start=start, period_end=end,
                     fee_usd=float(cfg["fee_usd"]), profit_pct=float(cfg["profit_pct"]), cum_pnl_usd=cum,
                     hwm_before_usd=hwm, hwm_after_usd=new_hwm, share_usd=share, total_usd=total,
                     status="due" if total > 0 else "paid", paid_at=None if total > 0 else now)
            session.add(b)
            made.append(b)
            hwm, start, n = new_hwm, end, n + 1
        row.billing_anchor, row.billing_hwm_usd = start, hwm
    return made


def estimate(row: TradingAccount, cfg: dict[str, Any], cum_now: float, cum_at_anchor: float,
             now: datetime) -> dict[str, Any]:
    """What the user's current period will cost if it ended now (shown on /me)."""
    anchor = anchor_of(row, cfg, now)
    if now < anchor:
        return {"trial": True, "trial_ends": anchor.isoformat(), "period_start": None, "period_end": anchor.isoformat(),
                "fee_usd": float(cfg["fee_usd"]), "profit_pct": float(cfg["profit_pct"]), "profit_usd": 0.0,
                "share_usd": 0.0, "total_usd": 0.0, "hwm_usd": cum_at_anchor}
    hwm = row.billing_hwm_usd if row.billing_hwm_usd is not None else cum_at_anchor
    share, total, _ = charge(float(cfg["fee_usd"]), float(cfg["profit_pct"]), cum_now, hwm)
    return {"trial": False, "trial_ends": None, "period_start": anchor.isoformat(),
            "period_end": add_months(anchor, 1).isoformat(), "fee_usd": float(cfg["fee_usd"]),
            "profit_pct": float(cfg["profit_pct"]), "profit_usd": round(cum_now - hwm, 2),
            "share_usd": share, "total_usd": total, "hwm_usd": round(hwm, 2)}


def bill_out(b: Bill) -> dict[str, Any]:
    iso = lambda d: _utc(d).isoformat() if d else None  # noqa: E731
    return {"id": b.id, "account": b.account, "user_id": b.user_id, "period_start": iso(b.period_start),
            "period_end": iso(b.period_end), "fee_usd": b.fee_usd, "profit_pct": b.profit_pct,
            "profit_usd": round(max(0.0, b.cum_pnl_usd - b.hwm_before_usd), 2), "share_usd": b.share_usd,
            "total_usd": b.total_usd, "status": b.status, "sol_amount": b.sol_amount, "signature": b.signature,
            "attempts": b.attempts, "error": b.error, "paid_at": iso(b.paid_at), "created_at": iso(b.created_at)}


# ---- collection --------------------------------------------------------------------
Notify = Callable[[str | None, str], Awaitable[None]]   # (user chat id or None = admin, text)

TEXT = {
    "paid": {"en": "🧾 Your bill for {period} was paid: ${total:.2f} ({sol:.4f} SOL) from your trading wallet.",
             "ar": "🧾 تم دفع فاتورة {period}: ${total:.2f} ({sol:.4f} SOL) من محفظة التداول."},
    "short": {"en": "⚠️ Your bill of ${total:.2f} is due but your trading wallet has only {have:.4f} SOL free. "
                    "Add SOL to it; new buys pause on {hold}.",
              "ar": "⚠️ فاتورتك ${total:.2f} مستحقة ولكن محفظة التداول فيها {have:.4f} SOL متاحة فقط. "
                    "أضف SOL؛ الشراء الجديد سيتوقف يوم {hold}."},
}


def _period(b: Bill) -> str:
    return f"{_utc(b.period_start):%Y-%m-%d} → {_utc(b.period_end):%Y-%m-%d}"


async def collect(session: AsyncSession, s: Settings, cfg: dict[str, Any], now: datetime,
                  make_executor: Callable[[str], Any], notify: Notify) -> dict[str, int]:
    from app.accounts import private_key_for

    out = {"paid": 0, "short": 0, "check": 0}
    due = (await session.execute(select(Bill).where(Bill.status == "due").order_by(Bill.id))).scalars().all()
    for b in due:
        if b.last_try_at and now - _utc(b.last_try_at) < RETRY_EVERY:
            continue
        row = (await session.execute(select(TradingAccount).where(TradingAccount.name == b.account))
               ).scalars().first()
        user = await session.get(User, b.user_id) if b.user_id else None
        chat = user.tg_chat_id if user else None
        lang = "en" if user and user.lang == "en" else "ar"
        key = private_key_for(row, s) if row else None
        b.attempts, b.last_try_at = (b.attempts or 0) + 1, now
        if not key:
            b.error = "no wallet key for this account"
            await session.commit()
            continue
        x = make_executor(key)
        try:
            price = await x.sol_price()
            bal = int(round(await x.sol_balance() * LAMPORTS))
            open_n = (await session.execute(select(func.count(LivePosition.id)).where(
                LivePosition.account == b.account, LivePosition.status == "open"))).scalar() or 0
            keep = FEE_LAMPORTS + int(s.live_sol_reserve * LAMPORTS) + (KEEP_WITH_OPEN if open_n else 0)
            lamports = math.ceil(b.total_usd / price * LAMPORTS)
            if bal - keep < lamports:
                b.error = f"not enough SOL: needs {lamports / LAMPORTS:.4f}, free {max(bal - keep, 0) / LAMPORTS:.4f}"
                await session.commit()
                out["short"] += 1
                if b.attempts == 1 or b.attempts % 24 == 0:        # on the first try, then once a day
                    hold = _utc(b.period_end) + timedelta(days=int(cfg["grace_days"]))
                    if chat:
                        await notify(chat, TEXT["short"][lang].format(total=b.total_usd, have=max(bal - keep, 0)
                                                                      / LAMPORTS, hold=f"{hold:%Y-%m-%d}"))
                    await notify(None, f"🧾 Bill #{b.id} [{b.account}] ${b.total_usd:.2f} unpaid: {b.error}")
                continue
            b.status, b.sol_usd, b.sol_amount = "sending", price, lamports / LAMPORTS
            await session.commit()                       # a crash from here on leaves "sending": never resent
            try:
                sig = await x.transfer_sol(cfg["wallet"], lamports)
            except Exception as exc:
                b.status, b.error = "check", f"transfer error, check on Solscan before retrying: {str(exc)[:200]}"
                await session.commit()
                out["check"] += 1
                await notify(None, f"⚠️ Bill #{b.id} [{b.account}] ${b.total_usd:.2f}: transfer error, it may or "
                                   f"may not have landed. Check the wallet on Solscan, then mark it on /admin.\n"
                                   f"{str(exc)[:160]}")
                continue
            b.status, b.signature, b.paid_at, b.error = "paid", sig, now, None
            await session.commit()
            out["paid"] += 1
            if chat:
                await notify(chat, TEXT["paid"][lang].format(period=_period(b), total=b.total_usd,
                                                             sol=b.sol_amount))
            await notify(None, f"💰 Bill #{b.id} [{b.account}] paid: ${b.total_usd:.2f} "
                               f"(fee ${b.fee_usd:.2f} + share ${b.share_usd:.2f}) = {b.sol_amount:.4f} SOL\n"
                               f"https://solscan.io/tx/{sig}")
        except Exception as exc:          # price or balance lookup failed: try again next hour
            b.error = f"lookup failed: {str(exc)[:200]}"
            await session.commit()
        finally:
            await x.close()
    return out


async def update_holds(session: AsyncSession, cfg: dict[str, Any], now: datetime) -> None:
    """Hold new buys of accounts with a bill unpaid `grace_days` after its period ended."""
    grace = timedelta(days=int(cfg["grace_days"]))
    late = {b.account for b in (await session.execute(select(Bill).where(Bill.status == "due"))).scalars().all()
            if cfg["enabled"] and _utc(b.period_end) + grace <= now}
    for row in await _user_accounts(session):
        want = row.name in late
        if bool(row.billing_hold) != want:
            row.billing_hold = want


async def run_once(session: AsyncSession, s: Settings, make_executor: Callable[[str], Any], notify: Notify,
                   now: datetime | None = None) -> dict[str, int]:
    now = now or _now()
    cfg = await get_config(session, s)
    if not cfg["enabled"] or s.live_mode != "live" or not cfg["wallet"]:
        await update_holds(session, cfg | {"enabled": False}, now)      # off: nobody is held
        await session.commit()
        return {"made": 0, "paid": 0, "short": 0, "check": 0}
    made = await create_bills(session, cfg, s.live_mode, now)
    await session.commit()
    for b in made:
        await notify(None, f"🧾 New bill #{b.id} [{b.account}] {_period(b)}: ${b.total_usd:.2f} "
                           f"(fee ${b.fee_usd:.2f} + {b.profit_pct:g}% of ${max(0, b.cum_pnl_usd - b.hwm_before_usd):.2f}"
                           f" profit)")
    res = await collect(session, s, cfg, now, make_executor, notify)
    await update_holds(session, cfg, now)
    await session.commit()
    return {"made": len(made), **res}


class BillingLoop:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    async def _notify(self, chat: str | None, text: str) -> None:
        s = self.s
        chat = chat or s.telegram_chat_id
        if not (s.telegram_bot_token and chat):
            return
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                             json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
        except Exception:
            log.warning("billing: telegram notify failed", exc_info=True)

    def _executor(self, key: str):
        from app.live import JupiterExecutor

        return JupiterExecutor(self.s, live=True, private_key=key)

    async def run_forever(self) -> None:
        from app.db import SessionLocal

        while True:
            try:
                async with SessionLocal() as session:
                    res = await run_once(session, self.s, self._executor, self._notify)
                if any(res.values()):
                    log.info("billing: %s", res)
            except Exception:
                log.exception("billing: run failed")
            await asyncio.sleep(self.s.billing_interval_seconds)
