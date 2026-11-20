"""Private live copy-trading status. Every request needs the X-Live-Password header (LIVE_DASHBOARD_PASSWORD)."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
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


def check_password(request: Request, x_live_password: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().live_dashboard_password
    if not expected:
        raise HTTPException(404, "Live dashboard is not configured")
    ip, now = _ip(request), time.monotonic()
    q = _fails[ip]
    while q and now - q[0] > FAIL_WINDOW:
        q.popleft()
    if len(q) >= MAX_FAILS_PER_IP:
        raise HTTPException(429, "Too many wrong passwords, try again later")
    if not x_live_password or not hmac.compare_digest(x_live_password.encode(), expected.encode()):
        q.append(now)
        raise HTTPException(401, "Wrong password")
    q.clear()


Auth = Annotated[None, Depends(check_password)]


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


# ---- accounts (/accounts page) ---------------------------------------------------
class LeaderIn(BaseModel):
    wallet: str
    label: str = ""
    unconditional: bool = False
    new_tokens_only: bool = False
    size_usd: float | None = None


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
    from app.accounts import public, rows

    s = get_settings()
    counts = await _open_counts(session)
    return {
        "accounts": [public(r, counts.get(r.name, 0)) for r in await rows(session, s)],
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


# ---- registered users (admin) -------------------------------------------------------
@router.get("/users")
async def list_users(session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import TradingAccount, User

    users = (await session.execute(select(User).order_by(User.id.desc()))).scalars().all()
    accts = {r.user_id: r.name for r in (await session.execute(
        select(TradingAccount).where(TradingAccount.user_id.is_not(None)))).scalars().all()}
    return {"users": [{
        "id": u.id, "email": u.email, "name": u.name, "status": u.status, "account": accts.get(u.id),
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


@router.get("/audit")
async def audit_log(session: Session, _: Auth, limit: int = 100) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import AuditLog

    rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))
                                  )).scalars().all()
    return {"entries": [{"at": r.at.isoformat() if r.at else None, "actor": r.actor, "action": r.action,
                         "target": r.target, "details": r.details, "ip": r.ip} for r in rows]}
