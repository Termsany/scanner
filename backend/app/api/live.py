"""Private operator API (live trading, accounts, users, billing).

Sign-in: the operator signs in with their admin email and password (the same session cookie as users, on a user
with role "admin"). Until an admin login exists, the X-Live-Password header (LIVE_DASHBOARD_PASSWORD) still works,
and is what creates the admin login (/admin/setup); after that the header alone is refused.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Chain, LivePosition
from app.repository import utc

router = APIRouter(prefix="/api/live", tags=["live"])
Session = Annotated[AsyncSession, Depends(get_session)]

FAIL_WINDOW = 15 * 60
MAX_FAILS_PER_IP = 8
_fails: dict[str, deque[float]] = defaultdict(deque)


def _ip(request: Request) -> str:
    # the API listens on 127.0.0.1 behind nginx, which sets X-Real-IP
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _legacy_password_ok(request: Request, given: str | None) -> bool:
    """The old shared password (LIVE_DASHBOARD_PASSWORD), with the per-IP lockout. Raises on a wrong one."""
    expected = get_settings().live_dashboard_password
    if not expected:
        raise HTTPException(404, "Live dashboard is not configured")
    ip, now = _ip(request), time.monotonic()
    q = _fails[ip]
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILS_PER_IP:
        raise HTTPException(429, "Too many wrong passwords, try again later")
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        q.append(now)
        raise HTTPException(401, "Wrong password")
    q.clear()
    return True


async def _has_admin(session: AsyncSession) -> bool:
    from sqlalchemy import select

    from app.models import User

    return (await session.execute(select(User.id).where(User.role == "admin", User.status == "active")
                                  .limit(1))).first() is not None


async def _admin_from_cookie(session: AsyncSession, token: str | None):
    from app.models import User, UserSession
    from app.users import _as_utc, _now, _token_hash

    if not token:
        return None
    row = await session.get(UserSession, _token_hash(token))
    if row is None or _as_utc(row.expires_at) < _now():
        return None
    user = await session.get(User, row.user_id)
    return user if user is not None and user.role == "admin" and user.status == "active" else None


async def check_password(request: Request, session: Session,
                         x_live_password: Annotated[str | None, Header()] = None,
                         alpha_session: Annotated[str | None, Cookie()] = None) -> None:
    """Admin session cookie; or, only while no admin login exists yet, the old shared password."""
    if await _admin_from_cookie(session, alpha_session) is not None:
        return
    if await _has_admin(session):
        raise HTTPException(401, "Sign in with your admin email and password")
    if not get_settings().live_dashboard_password:
        raise HTTPException(404, "Live dashboard is not configured")
    if not x_live_password:
        raise HTTPException(401, "Sign in first")
    _legacy_password_ok(request, x_live_password)


Auth = Annotated[None, Depends(check_password)]


# ---- admin sign-in -------------------------------------------------------------------
@router.get("/admin/status")
async def admin_status(session: Session) -> dict[str, Any]:
    """Public: whether the admin email login has been set up (the sign-in page shows setup until it is)."""
    return {"has_admin": await _has_admin(session)}


@router.get("/admin/me")
async def admin_me(session: Session, _: Auth, alpha_session: Annotated[str | None, Cookie()] = None) -> dict[str, Any]:
    user = await _admin_from_cookie(session, alpha_session)
    return {"email": user.email if user else None, "via": "session" if user else "password",
            "totp_enabled": bool(user.totp_enabled) if user else False}


class AdminSetupIn(BaseModel):
    current_password: str      # the old shared admin password (LIVE_DASHBOARD_PASSWORD)
    email: str
    password: str


@router.post("/admin/setup")
async def admin_setup(body: AdminSetupIn, request: Request, response: Response, session: Session) -> dict[str, Any]:
    """One time: turn the shared password into an admin email login. Afterwards the shared password stops working."""
    from app.notify import tell_admin
    from app.users import (EMAIL_RE, audit, hash_password, new_session, password_problem, set_cookie,
                           user_by_email)
    from app.models import User

    if await _has_admin(session):
        raise HTTPException(409, "The admin login is already set up: sign in with it")
    _legacy_password_ok(request, body.current_password)
    email = body.email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "Enter a valid email")
    if problem := password_problem(body.password):
        raise HTTPException(400, problem)
    if hmac.compare_digest(body.password.encode(), (get_settings().live_dashboard_password or "").encode()):
        raise HTTPException(400, "Choose a new password, not the old shared one")
    user = await user_by_email(session, email)
    if user is not None and user.role != "admin":
        raise HTTPException(400, "This email belongs to a user account: use another email for the admin")
    if user is None:
        user = User(email=email, name="Admin", password_hash=hash_password(body.password), status="active", role="admin")
        session.add(user)
    else:
        user.password_hash, user.status = hash_password(body.password), "active"
    await session.flush()
    token = await new_session(session, user)
    await audit(session, "admin", "admin_login_setup", email, ip=_ip(request))
    await session.commit()
    set_cookie(response, token)
    await tell_admin(f"🔐 Admin email login set up for {email} (IP {_ip(request)}). The old shared password no "
                     "longer opens the admin pages.")
    return {"ok": True, "email": email}


def _pos(p: LivePosition) -> dict[str, Any]:
    return {
        "id": p.id, "mode": p.mode, "account": p.account or "main", "token_address": p.token_address, "symbol": p.symbol,
        "leader_wallet": p.leader_wallet, "status": p.status, "cost_usd": p.cost_usd, "sol_spent": p.sol_spent,
        "proceeds_usd": p.proceeds_usd, "proceeds_sol": p.proceeds_sol, "pnl_usd": p.pnl_usd,
        "tokens_left": p.tokens_raw, "tokens_bought": p.tokens_bought_raw,
        "buy_signature": p.buy_signature, "manual_sell_frac": p.manual_sell_frac, "fills": p.fills or [], "exit_reason": p.exit_reason, "error": p.error,
        "opened_at": utc(p.opened_at).isoformat() if p.opened_at else None,
        "closed_at": utc(p.closed_at).isoformat() if p.closed_at else None,
    }


# ---- live market data (cached so a page polling every few seconds costs little) ----
PRICE_TTL = 3.0
BALANCE_TTL = 10.0
SOL_TTL = 60.0
_cache: dict[str, tuple[float, Any]] = {}
_decimals: dict[str, int] = {}   # never changes for a mint


async def _cached(key: str, ttl: float, fetch) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        val = await fetch()
    except Exception:
        return hit[1] if hit else None      # keep showing the last good value
    _cache[key] = (time.monotonic(), val)
    return val


async def jupiter_prices(http: httpx.AsyncClient, mints: list[str]) -> dict[str, dict[str, float]]:
    """{mint: {"usd": price, "decimals": n}}.

    Jupiter's free API is kept for trading (the worker shares its per-IP limit), so the page polls DexScreener
    for token prices and asks Jupiter only for decimals it has not seen (once per mint) and SOL (once a minute).
    """
    from app.live import SOL_MINT
    from app.scanner_worker import _f, select_primary_pair

    s = get_settings()
    tokens = [m for m in mints if m != SOL_MINT]
    need = [m for m in tokens if m not in _decimals]
    sol = _cache.get("solpx")
    want_sol = SOL_MINT in mints and not (sol and time.monotonic() - sol[0] < SOL_TTL)
    if need or want_sol:
        ids = need + ([SOL_MINT] if want_sol else [])
        r = await http.get(f"{s.jupiter_api_url}/price/v3", params={"ids": ",".join(ids[:50])}, timeout=8)
        if r.status_code == 200:
            for mint, d in (r.json() or {}).items():
                if d and d.get("decimals") is not None:
                    _decimals[mint] = int(d["decimals"])
                if mint == SOL_MINT and d and d.get("usdPrice"):
                    _cache["solpx"] = (time.monotonic(), float(d["usdPrice"]))
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(tokens), 30):
        batch = tokens[i:i + 30]
        r = await http.get(f"https://api.dexscreener.com/tokens/v1/solana/{','.join(batch)}", timeout=8)
        pairs = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        for m in batch:
            pair = select_primary_pair(Chain.solana, m, pairs)
            px = _f(pair.get("priceUsd")) if pair else None
            if px and m in _decimals:
                out[m] = {"usd": px, "decimals": _decimals[m]}
    if (sol := _cache.get("solpx")) and SOL_MINT in mints:
        out[SOL_MINT] = {"usd": sol[1], "decimals": 9}
    return out


async def sol_balances(http: httpx.AsyncClient, pubkeys: list[str]) -> dict[str, float]:
    s = get_settings()
    r = await http.post(s.live_rpc_url or s.solana_rpc_url, timeout=8, json={
        "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
        "params": [pubkeys, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]})
    r.raise_for_status()
    vals = (r.json().get("result") or {}).get("value") or []
    return {pk: ((v or {}).get("lamports", 0) / 1e9) for pk, v in zip(pubkeys, vals)}


def _mark(p: dict[str, Any], px: dict[str, float] | None) -> None:
    """Value the tokens still held at the live price; unrealized = value now + already sold - cost."""
    p["price_usd"] = p["value_usd"] = p["unrealized_usd"] = p["unrealized_pct"] = None
    if not px:
        return
    value = int(p["tokens_left"] or 0) / 10 ** px["decimals"] * px["usd"]
    p["price_usd"], p["value_usd"] = px["usd"], value
    p["unrealized_usd"] = value + (p["proceeds_usd"] or 0) - p["cost_usd"]
    p["unrealized_pct"] = p["unrealized_usd"] / p["cost_usd"] * 100 if p["cost_usd"] else None


@router.get("")
async def live(request: Request, session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.live import SOL_MINT, live_status
    from app.models import WalletTrade

    st = await live_status(session, get_settings())
    accounts = [
        {**{k: v for k, v in a.items() if k not in ("open", "closed", "failed")},
         "open": [_pos(p) for p in a["open"]],
         "closed": [_pos(p) for p in a["closed"]],
         "failed": [_pos(p) for p in a["failed"]][:30]}
        for a in st["accounts"]
    ]
    http: httpx.AsyncClient = request.app.state.http
    mints = sorted({p["token_address"] for a in accounts for p in a["open"]} | {SOL_MINT})
    prices = await _cached("px:" + ",".join(mints), PRICE_TTL, lambda: jupiter_prices(http, mints)) or {}
    pubkeys = sorted({a["pubkey"] for a in accounts if a["pubkey"]})
    balances = (await _cached("bal:" + ",".join(pubkeys), BALANCE_TTL, lambda: sol_balances(http, pubkeys))
                if pubkeys else {}) or {}
    sol_usd = (prices.get(SOL_MINT) or {}).get("usd")
    for a in accounts:
        for p in a["open"]:
            _mark(p, prices.get(p["token_address"]))
        marked = [p for p in a["open"] if p["value_usd"] is not None]
        a["open_value_usd"] = sum(p["value_usd"] for p in marked) if marked else None
        a["unrealized_usd"] = sum(p["unrealized_usd"] for p in marked) if marked else None
        a["sol_balance"] = balances.get(a["pubkey"]) if a["pubkey"] else None
        a["sol_balance_usd"] = a["sol_balance"] * sol_usd if a["sol_balance"] is not None and sol_usd else None

    wallets = sorted({w for a in accounts for w in a["wallets"]})
    trades = (await session.execute(
        select(WalletTrade).where(WalletTrade.wallet.in_(wallets)).order_by(WalletTrade.id.desc()).limit(15)
    )).scalars().all() if wallets else []
    return {
        "mode": st["mode"], "paused": st["paused"], "sol_usd": sol_usd,
        "updated_at": datetime.now(timezone.utc).isoformat(), "accounts": accounts,
        "leader_activity": [{
            "id": t.id, "wallet": t.wallet, "label": t.wallet_label, "side": t.side or "buy", "kind": t.kind or "buy",
            "symbol": t.symbol, "token_address": t.token_address, "usd": t.usd_value, "pct_sold": t.pct_sold,
            "tx": t.tx_hash, "at": (utc(t.block_time) or utc(t.detected_at)).isoformat(),
        } for t in trades],
    }


class PauseIn(BaseModel):
    paused: bool


@router.post("/pause")
async def pause(body: PauseIn, session: Session, _: Auth) -> dict[str, bool]:
    from app.live import set_paused

    await set_paused(session, body.paused)
    await session.commit()
    return {"paused": body.paused}


class SellIn(BaseModel):
    share: float   # 0.25 | 0.5 | 1.0


@router.post("/positions/{position_id}/sell")
async def sell(position_id: int, body: SellIn, session: Session, _: Auth) -> dict[str, Any]:
    """Queue a manual sell; the worker executes it within seconds and reports on Telegram."""
    from app.live import request_manual_sell

    ok, note = await request_manual_sell(session, position_id, body.share)
    if not ok:
        raise HTTPException(409, note)
    return {"queued": True, "message": note}


# ---- admin overview (/admin page) ---------------------------------------------------
@router.get("/overview")
async def overview(request: Request, session: Session, _: Auth) -> dict[str, Any]:
    """All accounts and users at a glance: balances, open value, P&L, failures, pending sign-ups. Read-only."""
    from app.live import SOL_MINT
    from app.overview import build_overview

    out = await build_overview(session, get_settings())
    http: httpx.AsyncClient = request.app.state.http
    accounts = out["accounts"]
    mints = sorted({t["token_address"] for a in accounts for t in a["open_tokens"]} | {SOL_MINT})
    prices = await _cached("px:" + ",".join(mints), PRICE_TTL, lambda: jupiter_prices(http, mints)) or {}
    pubkeys = sorted({a["pubkey"] for a in accounts if a["pubkey"]})
    balances = (await _cached("bal:" + ",".join(pubkeys), BALANCE_TTL, lambda: sol_balances(http, pubkeys))
                if pubkeys else {}) or {}
    sol_usd = (prices.get(SOL_MINT) or {}).get("usd")
    for a in accounts:
        value = unreal = 0.0
        priced = 0
        for t in a.pop("open_tokens"):
            px = prices.get(t["token_address"])
            if not px:
                continue
            v = int(t["tokens_left"] or 0) / 10 ** px["decimals"] * px["usd"]
            value += v
            unreal += v + t["proceeds_usd"] - t["cost_usd"]
            priced += 1
        a["open_value_usd"] = value if priced else (0.0 if not a["open_count"] else None)
        a["unrealized_usd"] = unreal if priced else (0.0 if not a["open_count"] else None)
        a["sol_balance"] = balances.get(a["pubkey"]) if a["pubkey"] else None
        a["sol_balance_usd"] = a["sol_balance"] * sol_usd if a["sol_balance"] is not None and sol_usd else None

    def total(k: str) -> float | None:
        vals = [a[k] for a in accounts if a[k] is not None]
        return sum(vals) if vals else None

    out["sol_usd"] = sol_usd
    out["totals"].update({k: total(k) for k in ("open_value_usd", "unrealized_usd", "sol_balance",
                                                 "sol_balance_usd")})
    return out


# ---- accounts (/accounts page) ---------------------------------------------------
class LeaderIn(BaseModel):
    wallet: str
    label: str = ""
    unconditional: bool = False
    new_tokens_only: bool = False
    size_usd: float | None = None
    enabled: bool = True                 # the follow switch: kept when the account is saved
    max_chase_pct: float | None = None   # most paid above its price, %; None = account default
    quick_exit: bool = False             # sell all at +TP / -SL / time limit / its first sell, instead of the 2x rule


class AccountIn(BaseModel):
    name: str
    size_usd: float
    max_open_usd: float
    max_daily_loss_usd: float
    take_capital_at_pct: float | None = 100.0
    max_hold_hours: int | None = None
    leaders: list[LeaderIn] = []
    key_mode: str = "generate"          # generate | import
    private_key: str | None = None      # key_mode=import


class AccountPatch(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    size_usd: float | None = None
    max_open_usd: float | None = None
    max_daily_loss_usd: float | None = None
    take_capital_at_pct: float | None = None
    take_capital_off: bool = False      # explicit off (null alone means "unchanged")
    max_hold_hours: int | None = None
    max_hold_default: bool = False      # back to LIVE_MAX_HOLD_HOURS
    leaders: list[LeaderIn] | None = None
    # admin caps on what the owner may set; a negative number clears a cap
    cap_size_usd: float | None = None
    cap_open_usd: float | None = None
    cap_loss_usd: float | None = None


async def _open_counts(session: AsyncSession) -> dict[str, int]:
    from sqlalchemy import func, select

    s = get_settings()
    rows = (await session.execute(select(LivePosition.account, func.count(LivePosition.id)).where(
        LivePosition.status == "open", LivePosition.mode == s.live_mode).group_by(LivePosition.account))).all()
    return {(a or "main"): n for a, n in rows}


async def _row(session: AsyncSession, name: str):
    from sqlalchemy import select

    from app.models import TradingAccount

    row = (await session.execute(select(TradingAccount).where(TradingAccount.name == name))).scalars().first()
    if row is None:
        raise HTTPException(404, "No such account")
    return row


@router.get("/accounts")
async def list_accounts(session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.accounts import public, rows

    s = get_settings()
    counts = await _open_counts(session)
    by_leader: dict[str, dict[str, int]] = {}
    for acct, wallet, n in (await session.execute(
            select(LivePosition.account, LivePosition.leader_wallet, func.count(LivePosition.id)).where(
                LivePosition.status == "open", LivePosition.mode == s.live_mode)
            .group_by(LivePosition.account, LivePosition.leader_wallet))).all():
        by_leader.setdefault(acct or "main", {})[wallet] = n
    return {
        "accounts": [{**public(r, counts.get(r.name, 0)), "open_by_leader": by_leader.get(r.name, {})}
                     for r in await rows(session, s)],
        "can_store_keys": bool(s.live_key_encryption_key),
        "defaults": {"max_hold_hours": s.live_max_hold_hours, "reload_seconds": s.live_accounts_reload_seconds},
    }


@router.post("/accounts")
async def create_account(body: AccountIn, session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.accounts import (NAME_RE, AccountError, check_limits, clean_leaders, encrypt_key, ensure_tracked,
                              new_wallet, public, rows)
    from app.live import PUBKEY, _get, _key, load_keypair
    from app.models import TradingAccount

    s = get_settings()
    name = body.name.strip().lower()
    try:
        if not NAME_RE.fullmatch(name):
            raise AccountError("name: 1-24 characters, a-z 0-9 _")
        existing = await rows(session, s)
        if any(r.name == name for r in existing):
            raise AccountError("an account with this name already exists")
        check_limits(body.size_usd, body.max_open_usd, body.max_daily_loss_usd, body.take_capital_at_pct,
                     body.max_hold_hours)
        leaders = clean_leaders([ld.model_dump() for ld in body.leaders])
        secret = None
        if body.key_mode == "generate":
            pubkey, secret = new_wallet()
            key = secret
        elif body.key_mode == "import":
            if not body.private_key:
                raise AccountError("paste the wallet's private key")
            try:
                pubkey = str(load_keypair(body.private_key).pubkey())
            except Exception:
                raise AccountError("that is not a valid Solana private key") from None
            key = body.private_key.strip()
        else:
            raise AccountError("key_mode must be generate or import")
        used = {r.pubkey for r in existing} | {await _get(session, _key(PUBKEY, r.name)) for r in existing}
        if pubkey in used:
            raise AccountError("another account already trades with this wallet")
        enc = encrypt_key(s, key)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from None
    row = TradingAccount(name=name, key_source="db", key_enc=enc, pubkey=pubkey, size_usd=body.size_usd,
                         max_open_usd=body.max_open_usd, max_daily_loss_usd=body.max_daily_loss_usd,
                         take_capital_at_pct=body.take_capital_at_pct, max_hold_hours=body.max_hold_hours,
                         leaders=leaders)
    session.add(row)
    await ensure_tracked(session, [ld["wallet"] for ld in leaders])
    await session.commit()
    out = {"account": public(row), "secret": secret}
    if secret:
        out["note"] = ("New wallet. Save this private key now (it is shown only once) and import it in Phantom "
                       f"to see or sell its tokens. Send SOL to {pubkey} to start trading.")
    return out


@router.patch("/accounts/{name}")
async def edit_account(name: str, body: AccountPatch, request: Request, session: Session,
                       _: Auth) -> dict[str, Any]:
    from app.accounts import AccountError, check_limits, clean_leaders, ensure_tracked, public

    row = await _row(session, name)
    counts = await _open_counts(session)
    size = body.size_usd if body.size_usd is not None else row.size_usd
    max_open = body.max_open_usd if body.max_open_usd is not None else row.max_open_usd
    max_loss = body.max_daily_loss_usd if body.max_daily_loss_usd is not None else row.max_daily_loss_usd
    take = None if body.take_capital_off else (body.take_capital_at_pct if body.take_capital_at_pct is not None
                                               else row.take_capital_at_pct)
    hold = None if body.max_hold_default else (body.max_hold_hours if body.max_hold_hours is not None
                                               else row.max_hold_hours)
    try:
        check_limits(size, max_open, max_loss, take, hold)
        leaders = clean_leaders([ld.model_dump() for ld in body.leaders]) if body.leaders is not None else None
        if body.enabled is False and counts.get(row.name, 0):
            raise AccountError(f"{counts[row.name]} positions are still open: pause the account instead "
                               "(sells keep working), and disable it once they are closed")
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from None
    row.size_usd, row.max_open_usd, row.max_daily_loss_usd = size, max_open, max_loss
    row.take_capital_at_pct, row.max_hold_hours = take, hold
    if body.paused is not None:
        row.paused = body.paused
    if body.enabled is not None:
        row.enabled = body.enabled
    if leaders is not None:
        row.leaders = leaders
        await ensure_tracked(session, [ld["wallet"] for ld in leaders])
    for f in ("cap_size_usd", "cap_open_usd", "cap_loss_usd"):
        v = getattr(body, f)
        if v is not None:
            setattr(row, f, None if v < 0 else v)
    from app.users import audit

    await audit(session, "admin", "account_edit", row.name, body.model_dump(exclude_none=True), _ip(request))
    await session.commit()
    return {"account": public(row, counts.get(row.name, 0))}


class LeaderToggleIn(BaseModel):
    wallet: str
    enabled: bool
    close_positions: bool = False   # with enabled=false: also sell what this account holds from it


@router.post("/accounts/{name}/leaders/toggle")
async def toggle_leader(name: str, body: LeaderToggleIn, request: Request, session: Session,
                        _: Auth) -> dict[str, Any]:
    """Follow / stop following one smart wallet on one account, right away (the worker picks it up within
    LIVE_ACCOUNTS_RELOAD_SECONDS). Stopped: no new buys from it; its open positions keep following its sells,
    unless close_positions sells them now."""
    from sqlalchemy import select

    from app.accounts import public
    from app.live import _acct
    from app.users import audit

    row = await _row(session, name)
    leaders = [dict(d) for d in (row.leaders or [])]
    hit = next((d for d in leaders if d.get("wallet") == body.wallet), None)
    if hit is None:
        raise HTTPException(404, "This account does not copy that wallet")
    hit["enabled"] = body.enabled
    row.leaders = leaders                       # new list: the JSON column sees the change
    closing = 0
    if not body.enabled and body.close_positions:
        opened = (await session.execute(select(LivePosition).where(
            _acct(row.name), LivePosition.leader_wallet == body.wallet, LivePosition.status == "open",
            LivePosition.mode == get_settings().live_mode))).scalars().all()
        for p in opened:
            if not p.manual_sell_frac:
                p.manual_sell_frac = 1.0        # the worker sells it on its next step
                closing += 1
    await audit(session, "admin", "leader_on" if body.enabled else "leader_off", row.name,
                {"wallet": body.wallet, "close_positions": body.close_positions, "closing": closing}, _ip(request))
    await session.commit()
    counts = await _open_counts(session)
    return {"account": public(row, counts.get(row.name, 0)), "closing": closing}


# ---- registered users (admin) -------------------------------------------------------
@router.get("/users")
async def list_users(session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import TradingAccount, User

    users = (await session.execute(select(User).where(User.role.is_distinct_from("admin"))
                                   .order_by(User.id.desc()))).scalars().all()
    accts = {r.user_id: r.name for r in (await session.execute(
        select(TradingAccount).where(TradingAccount.user_id.is_not(None)))).scalars().all()}
    return {"users": [{
        "id": u.id, "email": u.email, "name": u.name, "status": u.status, "account": accts.get(u.id),
        "totp_enabled": bool(u.totp_enabled),
        "withdraw_locked_until": u.withdraw_locked_until.isoformat() if u.withdraw_locked_until else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    } for u in users]}


class ApproveIn(BaseModel):
    leaders: list[LeaderIn] | None = None     # None = copy the smart wallets of account "main"
    size_usd: float | None = None
    max_open_usd: float | None = None
    max_daily_loss_usd: float | None = None
    cap_size_usd: float | None = None
    cap_open_usd: float | None = None
    cap_loss_usd: float | None = None
    take_capital_at_pct: float | None = 100.0


@router.post("/users/{user_id}/approve")
async def approve_user(user_id: int, body: ApproveIn, request: Request, session: Session,
                       _: Auth) -> dict[str, Any]:
    """Activate a user and give them a trading account with a fresh wallet (key shown once to them, not here)."""
    from sqlalchemy import select

    from app.accounts import (AccountError, check_limits, clean_leaders, encrypt_key, ensure_tracked, new_wallet,
                              public, rows)
    from app.models import TradingAccount, User
    from app.users import audit

    s = get_settings()
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    existing = (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
                ).scalars().first()
    if existing is None:
        try:
            if body.leaders is None:
                main = next((r for r in await rows(session, s) if r.name == "main"), None)
                leaders = [dict(d) for d in (main.leaders if main else [])]
            else:
                leaders = clean_leaders([ld.model_dump() for ld in body.leaders])
            size = body.size_usd or s.user_default_size_usd
            max_open = body.max_open_usd or s.user_default_open_usd
            loss = body.max_daily_loss_usd or s.user_default_loss_usd
            check_limits(size, max_open, loss, body.take_capital_at_pct, None)
            pubkey, secret = new_wallet()
            enc = encrypt_key(s, secret)
        except AccountError as exc:
            raise HTTPException(400, str(exc)) from None
        existing = TradingAccount(
            name=f"u{user.id}", user_id=user.id, key_source="db", key_enc=enc, pubkey=pubkey, key_revealed=False,
            size_usd=size, max_open_usd=max_open, max_daily_loss_usd=loss,
            take_capital_at_pct=body.take_capital_at_pct, leaders=leaders,
            cap_size_usd=body.cap_size_usd, cap_open_usd=body.cap_open_usd, cap_loss_usd=body.cap_loss_usd)
        session.add(existing)
        await ensure_tracked(session, [d["wallet"] for d in leaders])
    user.status = "active"
    existing.enabled = True
    await audit(session, "admin", "user_approved", user.email, {"account": existing.name}, _ip(request))
    await session.commit()
    return {"user_id": user.id, "status": user.status, "account": public(existing)}


class BlockIn(BaseModel):
    blocked: bool


@router.post("/users/{user_id}/block")
async def block_user(user_id: int, body: BlockIn, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    """Blocked: signed out, can't sign in, and their account stops buying (sells keep working)."""
    from sqlalchemy import select

    from app.models import TradingAccount, User
    from app.users import audit, end_all_sessions

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    acct = (await session.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))
            ).scalars().first()
    if body.blocked:
        user.status = "blocked"
        await end_all_sessions(session, user.id)
        if acct:
            acct.paused = True
    else:
        user.status = "active" if acct else "pending"
    await audit(session, "admin", "user_blocked" if body.blocked else "user_unblocked", user.email,
                ip=_ip(request))
    await session.commit()
    return {"user_id": user.id, "status": user.status}


@router.post("/users/{user_id}/reset-link")
async def admin_reset_link(user_id: int, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    """A one-time password reset link for a user who lost their password (send it to them yourself)."""
    from app.models import User
    from app.users import audit, new_reset_link

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    link = await new_reset_link(session, user, by="admin")
    await audit(session, "admin", "reset_link", user.email, ip=_ip(request))
    await session.commit()
    return {"link": link, "minutes": get_settings().password_reset_minutes}


@router.post("/users/{user_id}/reset-2fa")
async def admin_reset_2fa(user_id: int, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    """User lost their phone: turn their two-step codes off. Withdrawals wait LIVE withdraw_lock_hours."""
    from app.models import User
    from app.users import audit, lock_withdrawals

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    user.totp_enabled, user.totp_secret_enc, user.totp_last_step = False, None, None
    lock_withdrawals(user)
    await audit(session, "admin", "2fa_reset", user.email, ip=_ip(request))
    await session.commit()
    return {"ok": True, "withdraw_lock_hours": get_settings().withdraw_lock_hours}


@router.get("/audit")
async def audit_log(session: Session, _: Auth, limit: int = 100, q: str = "") -> dict[str, Any]:
    from sqlalchemy import or_, select

    from app.models import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 500))
    if q.strip():
        like = f"%{q.strip()[:60]}%"
        stmt = stmt.where(or_(AuditLog.actor.ilike(like), AuditLog.action.ilike(like),
                              AuditLog.target.ilike(like), AuditLog.ip.ilike(like)))
    rows = (await session.execute(stmt)).scalars().all()
    return {"entries": [{"at": r.at.isoformat() if r.at else None, "actor": r.actor, "action": r.action,
                         "target": r.target, "details": r.details, "ip": r.ip} for r in rows]}


# ---- billing (admin) -------------------------------------------------------------------
class BillingConfigIn(BaseModel):
    enabled: bool | None = None
    fee_usd: float | None = None
    profit_pct: float | None = None
    trial_days: int | None = None
    grace_days: int | None = None
    wallet: str | None = None


@router.get("/telegram")
async def telegram_prefs(session: Session, _: Auth) -> dict[str, Any]:
    from app.tg_prefs import KINDS, get_prefs

    s = get_settings()
    prefs = await get_prefs(session, s.telegram_signal_alerts, fresh=True)
    return {"configured": bool(s.telegram_bot_token and s.telegram_chat_id),
            "kinds": [{"key": k, "title": t, "help": h, "on": prefs[k]} for k, t, h in KINDS]}


@router.put("/telegram")
async def telegram_prefs_set(body: dict[str, bool], request: Request, session: Session, _: Auth) -> dict[str, Any]:
    from app.tg_prefs import set_prefs
    from app.users import audit

    await set_prefs(session, body, get_settings().telegram_signal_alerts)
    await audit(session, "admin", "telegram_prefs", "telegram", body, _ip(request))
    await session.commit()
    return await telegram_prefs(session, _)


@router.get("/billing")
async def billing(session: Session, _: Auth) -> dict[str, Any]:
    from datetime import timedelta

    from sqlalchemy import select

    from app.billing import Bill, bill_out, get_config
    from app.models import TradingAccount, User

    cfg = await get_config(session)
    bills = (await session.execute(select(Bill).order_by(Bill.id.desc()).limit(300))).scalars().all()
    emails = {u.id: u.email for u in (await session.execute(select(User))).scalars().all()}
    held = [r.name for r in (await session.execute(select(TradingAccount).where(
        TradingAccount.billing_hold.is_(True)))).scalars().all()]
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    paid = [b for b in bills if b.status == "paid" and b.paid_at and utc(b.paid_at) >= month_ago]
    return {
        "config": cfg, "live": get_settings().live_mode == "live", "held": held,
        "totals": {"paid_30d_usd": sum(b.total_usd for b in paid), "paid_30d_sol": sum(b.sol_amount or 0 for b in paid),
                   "due_usd": sum(b.total_usd for b in bills if b.status == "due"),
                   "check": sum(1 for b in bills if b.status in ("check", "sending"))},
        "bills": [{**bill_out(b), "email": emails.get(b.user_id)} for b in bills],
    }


@router.put("/billing/config")
async def billing_config(body: BillingConfigIn, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    from app.billing import BillingError, set_config
    from app.users import audit

    try:
        cfg = await set_config(session, body.model_dump(exclude_none=True))
    except BillingError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit(session, "admin", "billing_config", "billing", body.model_dump(exclude_none=True), _ip(request))
    await session.commit()
    return {"config": cfg}


class BillActionIn(BaseModel):
    action: str                 # waive | paid | due
    signature: str | None = None


@router.post("/billing/bills/{bill_id}")
async def bill_action(bill_id: int, body: BillActionIn, request: Request, session: Session,
                      _: Auth) -> dict[str, Any]:
    """waive: forgive it. paid: a "check" transfer did land (with its signature). due: it did not, try again."""
    from app.billing import Bill, bill_out, get_config, update_holds
    from app.users import audit

    b = await session.get(Bill, bill_id)
    if b is None:
        raise HTTPException(404, "No such bill")
    allowed = {"waive": ("due", "check", "sending"), "paid": ("check", "sending", "due"), "due": ("check", "sending")}
    if body.action not in allowed or b.status not in allowed[body.action]:
        raise HTTPException(409, f"Cannot {body.action} a bill that is {b.status}")
    now = datetime.now(timezone.utc)
    if body.action == "waive":
        b.status, b.error = "waived", None
    elif body.action == "paid":
        b.status, b.paid_at, b.error = "paid", now, None
        if body.signature:
            b.signature = body.signature.strip()[:128]
    else:
        b.status, b.last_try_at = "due", None
    await audit(session, "admin", f"bill_{body.action}", b.account, {"bill": b.id, "usd": b.total_usd}, _ip(request))
    await update_holds(session, await get_config(session), now)
    await session.commit()
    return {"bill": bill_out(b)}


# ---- coupons (admin) --------------------------------------------------------------------
class CouponCreate(BaseModel):
    code: str
    percent: float
    months: int | None = None         # billed months discounted; None = every month
    max_uses: int | None = None
    expires_at: datetime | None = None
    note: str = ""


class CouponPatch(BaseModel):
    active: bool | None = None


def _coupon(c, users: list[Any]) -> dict[str, Any]:
    return {"id": c.id, "code": c.code, "percent": c.percent, "months": c.months, "max_uses": c.max_uses,
            "uses": c.uses or 0, "expires_at": utc(c.expires_at).isoformat() if c.expires_at else None,
            "active": c.active, "note": c.note, "created_at": utc(c.created_at).isoformat() if c.created_at else None,
            "users": [u.email for u in users if u.coupon_code == c.code]}


@router.get("/coupons")
async def list_coupons(session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import Coupon, User

    cs = (await session.execute(select(Coupon).order_by(Coupon.id.desc()))).scalars().all()
    users = (await session.execute(select(User).where(User.coupon_code.is_not(None)))).scalars().all()
    return {"coupons": [_coupon(c, users) for c in cs]}


@router.post("/coupons")
async def create_coupon(body: CouponCreate, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.billing import CODE_OK, norm_code
    from app.models import Coupon
    from app.users import audit

    code = norm_code(body.code)
    if not 3 <= len(code) <= 32 or not set(code) <= CODE_OK:
        raise HTTPException(400, "code: 3-32 characters, letters, digits, - and _")
    if not 1 <= body.percent <= 100:
        raise HTTPException(400, "discount must be between 1% and 100%")
    if body.months is not None and not 1 <= body.months <= 120:
        raise HTTPException(400, "months: 1-120, or empty for every month")
    if body.max_uses is not None and body.max_uses < 1:
        raise HTTPException(400, "max uses must be at least 1, or empty for no limit")
    if (await session.execute(select(Coupon).where(Coupon.code == code))).scalars().first():
        raise HTTPException(400, "a coupon with this code already exists")
    c = Coupon(code=code, percent=round(body.percent, 2), months=body.months, max_uses=body.max_uses,
               expires_at=body.expires_at, note=body.note.strip()[:120])
    session.add(c)
    await audit(session, "admin", "coupon_create", code, body.model_dump(mode="json"), _ip(request))
    await session.commit()
    return {"coupon": _coupon(c, [])}


@router.patch("/coupons/{coupon_id}")
async def edit_coupon(coupon_id: int, body: CouponPatch, request: Request, session: Session,
                      _: Auth) -> dict[str, Any]:
    """Turn a code off (or on again). Users who already applied it keep their discount."""
    from app.models import Coupon
    from app.users import audit

    c = await session.get(Coupon, coupon_id)
    if c is None:
        raise HTTPException(404, "No such coupon")
    if body.active is not None:
        c.active = body.active
    await audit(session, "admin", "coupon_edit", c.code, body.model_dump(exclude_none=True), _ip(request))
    await session.commit()
    return {"coupon": _coupon(c, [])}
