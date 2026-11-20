"""Registered users' own page (/me): sign up, sign in, their trading account, settings, sells and withdrawals.

Nothing here ever returns which smart wallets an account copies: positions are stripped of the leader, the
leader-activity feed is not exposed, and exit reasons use neutral words.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import LivePosition, TradingAccount, User
from app.notify import tell_admin
from app.users import (EMAIL_RE, COOKIE, CurrentUser, _DUMMY, audit, check_password, clear_cookie, cleared,
                       end_all_sessions, end_session, failed, hash_password, ip_of, new_session, password_problem,
                       set_cookie, too_many, user_by_email)

router = APIRouter(prefix="/api/me", tags=["me"])
Session = Annotated[AsyncSession, Depends(get_session)]

EXIT_WORDS = {"leader_sold": "exit signal", "leader_trim": "partial exit", "take_capital": "capital taken",
              "sold_by_hand": "sold by hand", "manual": "sold from page", "max_hold": "max hold time"}
_withdraw_lock = asyncio.Lock()


def _enabled() -> None:
    if not get_settings().users_enabled:
        raise HTTPException(404, "Accounts are not open")


# ---- sign up / in / out ------------------------------------------------------------
class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/register")
async def register(body: RegisterIn, request: Request, response: Response, session: Session) -> dict[str, Any]:
    _enabled()
    s = get_settings()
    ip = ip_of(request)
    if not s.users_registration_open:
        raise HTTPException(403, "Sign-ups are closed")
    if too_many("reg:" + ip):
        raise HTTPException(429, "Too many sign-ups from here, try again later")
    email = body.email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "Enter a valid email")
    if problem := password_problem(body.password):
        raise HTTPException(400, problem)
    if await user_by_email(session, email):
        raise HTTPException(400, "This email is already registered: sign in instead")
    failed("reg:" + ip)                       # counts sign-ups per IP (limits mass registration)
    user = User(email=email, name=body.name.strip()[:80], password_hash=hash_password(body.password))
    session.add(user)
    await session.flush()
    await audit(session, f"user:{user.id}", "register", email, ip=ip)
    token = await new_session(session, user)
    await session.commit()
    set_cookie(response, token)
    await tell_admin(f"👤 New sign-up waiting for approval: {email} (IP {ip}). Approve or block it on /accounts.")
    return {"ok": True, "status": user.status}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, session: Session) -> dict[str, Any]:
    _enabled()
    ip, email = ip_of(request), body.email.strip().lower()
    if too_many("ip:" + ip, "em:" + email):
        raise HTTPException(429, "Too many wrong passwords, try again in 15 minutes")
    user = await user_by_email(session, email)
    if user is None:
        check_password(body.password, _DUMMY)
        failed("ip:" + ip, "em:" + email)
        raise HTTPException(401, "Wrong email or password")
    if not check_password(body.password, user.password_hash):
        failed("ip:" + ip, "em:" + email)
        await audit(session, f"user:{user.id}", "login_failed", ip=ip)
        await session.commit()
        raise HTTPException(401, "Wrong email or password")
    if user.status == "blocked":
        raise HTTPException(403, "This account is blocked")
    cleared("ip:" + ip, "em:" + email)
    from datetime import datetime, timezone

    user.last_login_at = datetime.now(timezone.utc)
    token = await new_session(session, user)
    await audit(session, f"user:{user.id}", "login", ip=ip)
    await session.commit()
    set_cookie(response, token)
    return {"ok": True, "status": user.status}


@router.post("/logout")
async def logout(response: Response, session: Session,
                 alpha_session: Annotated[str | None, Cookie()] = None) -> dict[str, bool]:
    await end_session(session, alpha_session)
    await session.commit()
    clear_cookie(response)
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/password")
async def change_password(body: PasswordIn, user: CurrentUser, request: Request, response: Response,
                          session: Session) -> dict[str, bool]:
    if not check_password(body.current, user.password_hash):
        raise HTTPException(401, "Current password is wrong")
    if problem := password_problem(body.new):
        raise HTTPException(400, problem)
    user.password_hash = hash_password(body.new)
    await end_all_sessions(session, user.id)          # signs out every other device
    token = await new_session(session, user)
    await audit(session, f"user:{user.id}", "password_changed", ip=ip_of(request))
    await session.commit()
    await tell_admin(f"🔐 {user.email} changed their password (IP {ip_of(request)}).")
    set_cookie(response, token)
    return {"ok": True}


# ---- my account ----------------------------------------------------------------------
async def _my_account(session: AsyncSession, user: User) -> TradingAccount | None:
    return (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id)
                                  .order_by(TradingAccount.id))).scalars().first()


def _clean(p: dict[str, Any]) -> dict[str, Any]:
    """A position as the owner may see it: no leader, neutral exit words."""
    p = {k: v for k, v in p.items() if k not in ("leader_wallet", "fills", "error", "mode", "account")}
    p["exit_reason"] = EXIT_WORDS.get(p.get("exit_reason") or "", p.get("exit_reason"))
    return p


def _settings_out(a: TradingAccount) -> dict[str, Any]:
    return {"size_usd": a.size_usd, "max_open_usd": a.max_open_usd, "max_daily_loss_usd": a.max_daily_loss_usd,
            "take_capital_at_pct": a.take_capital_at_pct, "paused": a.paused,
            "caps": {"size_usd": a.cap_size_usd, "max_open_usd": a.cap_open_usd,
                     "max_daily_loss_usd": a.cap_loss_usd}}


@router.get("")
async def me(user: CurrentUser, request: Request, session: Session) -> dict[str, Any]:
    from app.api.live import BALANCE_TTL, PRICE_TTL, _cached, _mark, _pos, jupiter_prices, sol_balances
    from app.live import SOL_MINT, realized_24h

    s = get_settings()
    from app.users import withdraw_lock_left

    out: dict[str, Any] = {"user": {"email": user.email, "name": user.name, "status": user.status},
                           "security": {"totp_enabled": bool(user.totp_enabled),
                                        "withdraw_lock_hours": round(withdraw_lock_left(user), 1)},
                           "account": None}
    a = await _my_account(session, user)
    if a is None or user.status != "active":
        return out
    mode = s.live_mode if s.live_mode in ("dry", "live") else "off"
    rows = (await session.execute(select(LivePosition).where(
        LivePosition.account == a.name, LivePosition.mode == mode, LivePosition.status != "failed")
        .order_by(LivePosition.opened_at.desc()).limit(200))).scalars().all()
    opened = [_pos(p) for p in rows if p.status == "open"]
    closed = [_pos(p) for p in rows if p.status == "closed"]
    http: httpx.AsyncClient = request.app.state.http
    mints = sorted({p["token_address"] for p in opened} | {SOL_MINT})
    prices = await _cached("px:" + ",".join(mints), PRICE_TTL, lambda: jupiter_prices(http, mints)) or {}
    for p in opened:
        _mark(p, prices.get(p["token_address"]))
    sol_usd = (prices.get(SOL_MINT) or {}).get("usd")
    bal = None
    if a.pubkey:
        bal = ((await _cached("bal:" + a.pubkey, BALANCE_TTL, lambda: sol_balances(http, [a.pubkey]))) or {}
               ).get(a.pubkey)
    marked = [p for p in opened if p.get("unrealized_usd") is not None]
    out["account"] = {
        "name": a.name, "enabled": a.enabled, "pubkey": a.pubkey, "mode": mode,
        "key_revealed": bool(a.key_revealed), "sol_balance": bal, "sol_usd": sol_usd,
        "sol_balance_usd": bal * sol_usd if bal is not None and sol_usd else None,
        "settings": _settings_out(a),
        "realized_usd": sum(p["pnl_usd"] or 0 for p in closed),
        "realized_24h": await realized_24h(session, mode, a.name),
        "unrealized_usd": sum(p["unrealized_usd"] for p in marked) if marked else None,
        "open_cost_usd": sum(p["cost_usd"] for p in opened),
        "open": [_clean(p) for p in opened], "closed": [_clean(p) for p in closed[:100]],
    }
    return out


class MySettingsIn(BaseModel):
    size_usd: float | None = None
    max_open_usd: float | None = None
    max_daily_loss_usd: float | None = None
    take_capital_at_pct: float | None = None
    take_capital_off: bool = False
    paused: bool | None = None


@router.patch("/account")
async def edit_my_account(body: MySettingsIn, user: CurrentUser, request: Request,
                          session: Session) -> dict[str, Any]:
    from app.accounts import AccountError, check_limits

    a = await _need_account(session, user)
    size = body.size_usd if body.size_usd is not None else a.size_usd
    max_open = body.max_open_usd if body.max_open_usd is not None else a.max_open_usd
    loss = body.max_daily_loss_usd if body.max_daily_loss_usd is not None else a.max_daily_loss_usd
    take = None if body.take_capital_off else (
        body.take_capital_at_pct if body.take_capital_at_pct is not None else a.take_capital_at_pct)
    try:
        check_limits(size, max_open, loss, take, a.max_hold_hours)
        for v, cap, what in ((size, a.cap_size_usd, "entry per trade"), (max_open, a.cap_open_usd, "trading budget"),
                             (loss, a.cap_loss_usd, "max loss")):
            if cap is not None and v > cap + 1e-9:
                raise AccountError(f"{what} can be at most ${cap:,.0f} for your account")
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from None
    before = _settings_out(a)
    a.size_usd, a.max_open_usd, a.max_daily_loss_usd, a.take_capital_at_pct = size, max_open, loss, take
    if body.paused is not None:
        a.paused = body.paused
    await audit(session, f"user:{user.id}", "settings", a.name,
                {"before": before, "after": _settings_out(a)}, ip_of(request))
    await session.commit()
    return {"settings": _settings_out(a)}


async def _need_account(session: AsyncSession, user: User) -> TradingAccount:
    if user.status != "active":
        raise HTTPException(403, "Your account is waiting for approval")
    a = await _my_account(session, user)
    if a is None:
        raise HTTPException(404, "No trading account yet")
    return a


class ConfirmIn(BaseModel):
    password: str
    code: str | None = None       # two-step code, when 2FA is on


@router.post("/account/reveal-key")
async def reveal_key(body: ConfirmIn, user: CurrentUser, request: Request, session: Session) -> dict[str, Any]:
    """The wallet's private key, once, so the owner can import it in Phantom."""
    from app.accounts import decrypt_key

    a = await _need_account(session, user)
    _confirm(user, body.password, request)
    if user.totp_enabled:
        _check_code(user, body.code, request)
    if a.key_revealed:
        raise HTTPException(409, "The key was already shown once")
    if a.key_source != "db" or not a.key_enc:
        raise HTTPException(409, "This account's key is not stored here")
    secret = decrypt_key(get_settings(), a.key_enc)
    a.key_revealed = True
    await audit(session, f"user:{user.id}", "key_revealed", a.name, ip=ip_of(request))
    await session.commit()
    await tell_admin(f"🔑 {user.email} [{a.name}] viewed their wallet private key (IP {ip_of(request)}).")
    return {"secret": secret, "pubkey": a.pubkey}


def _confirm(user: User, password: str, request: Request) -> None:
    ip = ip_of(request)
    if too_many("cf:" + str(user.id)):
        raise HTTPException(429, "Too many wrong passwords, try again in 15 minutes")
    if not check_password(password, user.password_hash):
        failed("cf:" + str(user.id), "ip:" + ip)
        raise HTTPException(401, "Wrong password")
    cleared("cf:" + str(user.id))


def _check_code(user: User, code: str | None, request: Request) -> None:
    from app.accounts import decrypt_key
    from app.users import totp_check

    if too_many("cf:" + str(user.id)):
        raise HTTPException(429, "Too many wrong codes, try again in 15 minutes")
    if not user.totp_enabled or not user.totp_secret_enc:
        raise HTTPException(403, "Turn on two-step codes first (Security, on this page)")
    step = totp_check(decrypt_key(get_settings(), user.totp_secret_enc), code or "", user.totp_last_step)
    if step is None:
        failed("cf:" + str(user.id), "ip:" + ip_of(request))
        raise HTTPException(401, "Wrong or already used code: wait for the next one in your authenticator app")
    user.totp_last_step = step


class SellIn(BaseModel):
    share: float


@router.post("/positions/{position_id}/sell")
async def sell_mine(position_id: int, body: SellIn, user: CurrentUser, request: Request,
                    session: Session) -> dict[str, Any]:
    from app.live import request_manual_sell

    a = await _need_account(session, user)
    pos = await session.get(LivePosition, position_id)
    if pos is None or pos.account != a.name:
        raise HTTPException(404, "No such position")
    ok, note = await request_manual_sell(session, position_id, body.share)
    if not ok:
        raise HTTPException(409, note)
    await audit(session, f"user:{user.id}", "sell", a.name, {"position": position_id, "share": body.share},
                ip_of(request))
    await session.commit()
    return {"queued": True, "message": note}


class WithdrawIn(BaseModel):
    password: str
    code: str | None = None       # two-step code (required)
    to: str
    sol: float | None = None      # None = everything that can go
    all: bool = False


FEE_LAMPORTS = 5_000
KEEP_WITH_OPEN = 10_000_000       # 0.01 SOL stays while positions are open: their sells need fees


@router.post("/account/withdraw")
async def withdraw(body: WithdrawIn, user: CurrentUser, request: Request, session: Session) -> dict[str, Any]:
    from app.accounts import WALLET_RE, private_key_for
    from app.live import LAMPORTS, JupiterExecutor

    from app.users import withdraw_lock_left

    a = await _need_account(session, user)
    _confirm(user, body.password, request)
    _check_code(user, body.code, request)
    await session.commit()                       # the code is spent even if the withdrawal stops below
    if (left := withdraw_lock_left(user)) > 0:
        raise HTTPException(423, f"Withdrawals are locked for {left:.0f} more hours after a password or "
                                 "two-step reset (a safety wait)")
    to = body.to.strip()
    if not WALLET_RE.fullmatch(to):
        raise HTTPException(400, "Enter a valid Solana address to send to")
    if to == a.pubkey:
        raise HTTPException(400, "That is the trading wallet itself")
    s = get_settings()
    if s.live_mode != "live":
        raise HTTPException(409, "Withdrawals work only when trading is live")
    open_n = len((await session.execute(select(LivePosition.id).where(
        LivePosition.account == a.name, LivePosition.status == "open", LivePosition.mode == "live"))).all())
    async with _withdraw_lock:
        x = JupiterExecutor(s, live=True, private_key=private_key_for(a, s))
        try:
            bal = int(round(await x.sol_balance() * LAMPORTS))
            keep = FEE_LAMPORTS + (KEEP_WITH_OPEN if open_n else 0)
            room = bal - keep
            amount = room if (body.all or body.sol is None) else int(round(body.sol * LAMPORTS))
            if amount <= 0 or amount > room:
                raise HTTPException(400, f"You can send at most {max(room, 0) / LAMPORTS:.4f} SOL"
                                    + (" (0.01 SOL stays for the fees of your open positions)" if open_n else ""))
            await audit(session, f"user:{user.id}", "withdraw_start", a.name,
                        {"to": to, "sol": amount / LAMPORTS}, ip_of(request))
            await session.commit()
            try:
                sig = await x.transfer_sol(to, amount)
            except Exception as exc:
                await audit(session, f"user:{user.id}", "withdraw_failed", a.name,
                            {"to": to, "sol": amount / LAMPORTS, "error": str(exc)[:200]}, ip_of(request))
                await session.commit()
                await tell_admin(f"⚠️ Withdrawal by {user.email} [{a.name}] failed: {amount / LAMPORTS:.4f} SOL "
                                 f"to {to}\n{str(exc)[:160]}")
                raise HTTPException(502, f"Withdrawal failed: {str(exc)[:160]}") from None
            await audit(session, f"user:{user.id}", "withdraw", a.name,
                        {"to": to, "sol": amount / LAMPORTS, "sig": sig}, ip_of(request))
            await session.commit()
            await tell_admin(f"💸 {user.email} [{a.name}] withdrew {amount / LAMPORTS:.4f} SOL to {to} "
                             f"(IP {ip_of(request)})\nhttps://solscan.io/tx/{sig}")
            return {"ok": True, "sol": amount / LAMPORTS, "signature": sig}
        finally:
            await x.close()


# ---- forgot password ---------------------------------------------------------------
class ForgotIn(BaseModel):
    email: str


@router.post("/forgot")
async def forgot(body: ForgotIn, request: Request, session: Session) -> dict[str, Any]:
    """Always answers the same, so it never tells whether an email is registered."""
    from app.users import new_reset_link, send_email

    _enabled()
    ip = ip_of(request)
    if too_many("fg:" + ip):
        raise HTTPException(429, "Too many requests, try again later")
    failed("fg:" + ip)
    email = body.email.strip().lower()
    user = await user_by_email(session, email) if EMAIL_RE.fullmatch(email) else None
    if user is not None and user.status != "blocked":
        link = await new_reset_link(session, user)
        await audit(session, f"user:{user.id}", "reset_requested", ip=ip)
        await session.commit()
        mins = get_settings().password_reset_minutes
        sent = await send_email(email, "Reset your Alpha Scanner password",
                                f"<p>Someone asked to reset the password of {email}.</p>"
                                f'<p><a href="{link}">Choose a new password</a> (works once, for {mins} minutes).</p>'
                                "<p>If it wasn't you, ignore this email.</p>")
        await tell_admin(f"🔁 {email} asked to reset their password (IP {ip}). "
                         + ("A link was emailed to them." if sent else
                            f"No email service is set up: if this is really them, send them this link "
                            f"(one use, {mins} min):\n{link}"))
    return {"ok": True, "message": "If this email is registered, a reset link is on its way."}


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/reset")
async def reset_password(body: ResetIn, request: Request, response: Response, session: Session) -> dict[str, Any]:
    from app.users import lock_withdrawals, use_reset_token

    _enabled()
    ip = ip_of(request)
    if too_many("rs:" + ip):
        raise HTTPException(429, "Too many tries, try again later")
    if problem := password_problem(body.password):
        raise HTTPException(400, problem)
    user = await use_reset_token(session, body.token)
    if user is None or user.status == "blocked":
        failed("rs:" + ip)
        raise HTTPException(400, "This link is invalid, used or expired: ask for a new one")
    user.password_hash = hash_password(body.password)
    lock_withdrawals(user)
    await end_all_sessions(session, user.id)
    token = await new_session(session, user)
    await audit(session, f"user:{user.id}", "password_reset", ip=ip)
    await session.commit()
    set_cookie(response, token)
    h = get_settings().withdraw_lock_hours
    await tell_admin(f"🔁 {user.email} reset their password (IP {ip}). Withdrawals locked for {h} h.")
    return {"ok": True, "withdraw_lock_hours": h}


# ---- two-step codes ----------------------------------------------------------------
@router.post("/2fa/setup")
async def totp_setup(body: ConfirmIn, user: CurrentUser, request: Request, session: Session) -> dict[str, Any]:
    """New secret + QR code; it takes effect only after /2fa/enable with a valid code."""
    from app.accounts import encrypt_key
    from app.users import qr_data_uri, totp_new_secret, totp_uri

    _confirm(user, body.password, request)
    if user.totp_enabled:
        raise HTTPException(409, "Two-step codes are already on")
    secret = totp_new_secret()
    user.totp_secret_enc = encrypt_key(get_settings(), secret)
    user.totp_last_step = None
    await session.commit()
    uri = totp_uri(secret, user.email)
    return {"secret": secret, "uri": uri, "qr": qr_data_uri(uri)}


class CodeIn(BaseModel):
    code: str


@router.post("/2fa/enable")
async def totp_enable(body: CodeIn, user: CurrentUser, request: Request, session: Session) -> dict[str, Any]:
    from app.accounts import decrypt_key
    from app.users import totp_check

    if user.totp_enabled:
        raise HTTPException(409, "Two-step codes are already on")
    if not user.totp_secret_enc:
        raise HTTPException(400, "Start the setup first")
    step = totp_check(decrypt_key(get_settings(), user.totp_secret_enc), body.code, None)
    if step is None:
        raise HTTPException(401, "That code doesn't match: check the time on your phone and try the next code")
    user.totp_enabled, user.totp_last_step = True, step
    await audit(session, f"user:{user.id}", "2fa_enabled", ip=ip_of(request))
    await session.commit()
    await tell_admin(f"🛡 {user.email} turned on two-step codes.")
    return {"ok": True}


class DisableIn(BaseModel):
    password: str
    code: str


@router.post("/2fa/disable")
async def totp_disable(body: DisableIn, user: CurrentUser, request: Request, session: Session) -> dict[str, Any]:
    from app.users import lock_withdrawals

    _confirm(user, body.password, request)
    _check_code(user, body.code, request)
    user.totp_enabled, user.totp_secret_enc, user.totp_last_step = False, None, None
    lock_withdrawals(user)
    await audit(session, f"user:{user.id}", "2fa_disabled", ip=ip_of(request))
    await session.commit()
    await tell_admin(f"⚠️ {user.email} turned OFF two-step codes (IP {ip_of(request)}). Withdrawals locked "
                     f"for {get_settings().withdraw_lock_hours} h.")
    return {"ok": True}
