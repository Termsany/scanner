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
from app.gecko_budget import GeckoBudget
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
        "target_pct": p.target_pct, "target_share": p.target_share, "stop_pct": p.stop_pct,
        "target_price": p.target_price, "stop_price": p.stop_price, "hands_off": bool(p.hands_off),
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


async def _with_live(prices: dict[str, dict[str, float]], mints: list[str]) -> dict[str, dict[str, float]]:
    """DexScreener lags minutes behind on new pump coins: use the worker's own exact price where it has a fresh
    one (bonding curve / pool, app/live_prices.py)."""
    from app import live_prices

    try:
        fast = await live_prices.read([m for m in mints if m.endswith("pump")])
    except Exception:
        return prices
    if not fast:
        return prices
    out = dict(prices)
    for m, usd in fast.items():
        dec = (prices.get(m) or {}).get("decimals", _decimals.get(m, 6))
        out[m] = {"usd": usd, "decimals": dec}
    return out


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
    prices = await _with_live(await _cached("px:" + ",".join(mints), PRICE_TTL, lambda: jupiter_prices(http, mints)) or {}, mints)
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


HOLD_TTL = 15.0
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


async def wallet_holdings(http: httpx.AsyncClient, pubkey: str) -> dict[str, dict[str, Any]]:
    """{mint: {"raw": int, "decimals": int}} of every token one wallet holds (SPL Token and Token-2022)."""
    s = get_settings()
    out: dict[str, dict[str, Any]] = {}
    for program in TOKEN_PROGRAMS:
        r = await http.post(s.live_rpc_url or s.solana_rpc_url, timeout=10, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [pubkey, {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}]})
        r.raise_for_status()
        for acc in ((r.json().get("result") or {}).get("value") or []):
            info = acc["account"]["data"]["parsed"]["info"]
            amt = int(info["tokenAmount"]["amount"])
            if amt <= 0:
                continue
            dec = int(info["tokenAmount"]["decimals"])
            row = out.setdefault(info["mint"], {"raw": 0, "decimals": dec})
            row["raw"] += amt
            _decimals.setdefault(info["mint"], dec)
    return out


async def token_meta(http: httpx.AsyncClient, mints: list[str]) -> dict[str, dict[str, Any]]:
    """{mint: {"symbol", "price"}} from DexScreener (the pair the scanner would pick)."""
    from app.scanner_worker import _f, select_primary_pair

    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(mints), 30):
        batch = mints[i:i + 30]
        r = await http.get(f"https://api.dexscreener.com/tokens/v1/solana/{','.join(batch)}", timeout=8)
        pairs = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        for m in batch:
            pair = select_primary_pair(Chain.solana, m, pairs)
            if pair:
                base = pair.get("baseToken") or {}
                out[m] = {"symbol": base.get("symbol") if base.get("address") == m else None,
                          "price": _f(pair.get("priceUsd"))}
    return out


@router.get("/holdings")
async def holdings(request: Request, session: Session, _: Auth) -> dict[str, Any]:
    """Every coin in each account's wallet with its value now, and the bot position (and target) it belongs to."""
    from app.live import STABLES, live_status

    st = await live_status(session, get_settings())
    http: httpx.AsyncClient = request.app.state.http
    out = []
    for a in st["accounts"]:
        pk = a.get("pubkey")
        if not pk:
            continue
        held = await _cached(f"hold:{pk}", HOLD_TTL, lambda pk=pk: wallet_holdings(http, pk)) or {}
        mints = sorted(held)
        meta = (await _cached("meta:" + ",".join(mints), HOLD_TTL, lambda m=mints: token_meta(http, m)) or {}) if mints else {}
        by_mint = {p.token_address: p for p in a["open"]}
        coins = []
        for m, h in held.items():
            info = meta.get(m) or {}
            amount = h["raw"] / 10 ** h["decimals"]
            px = 1.0 if m in STABLES else info.get("price")
            p = by_mint.get(m)
            coins.append({
                "mint": m, "symbol": (p.symbol if p else None) or info.get("symbol") or m[:6],
                "raw": str(h["raw"]), "amount": amount, "price_usd": px,
                "value_usd": amount * px if px else None, "stable": m in STABLES,
                "position": _pos(p) if p else None,
            })
        coins.sort(key=lambda c: -(c["value_usd"] or 0))
        out.append({"name": a["name"], "pubkey": pk, "coins": coins})
    return {"mode": st["mode"], "accounts": out, "updated_at": datetime.now(timezone.utc).isoformat()}


class HoldTargetIn(BaseModel):
    account: str
    mint: str
    share: float | None = 1.0
    target_price: float | None = None   # sell `share` once the price reaches this (USD per token)
    stop_price: float | None = None     # sell everything at or below this


@router.post("/holdings/target")
async def holdings_target(body: HoldTargetIn, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    from app.live import hold_with_target

    s = get_settings()
    mode = s.live_mode if s.live_mode in ("dry", "live") else None
    if mode is None:
        raise HTTPException(400, "Copy trading is off on the server")
    from app.live import live_status

    st = await live_status(session, s)
    acct = next((a for a in st["accounts"] if a["name"] == body.account), None)
    if acct is None or not acct.get("pubkey"):
        raise HTTPException(404, "No such account")
    pubkey = acct["pubkey"]
    http: httpx.AsyncClient = request.app.state.http
    held = await wallet_holdings(http, pubkey)                 # fresh: the amount the position will hold
    h = held.get(body.mint)
    meta = (await token_meta(http, [body.mint])).get(body.mint) or {}
    value = h["raw"] / 10 ** h["decimals"] * meta["price"] if h and meta.get("price") else None
    ok, note = await hold_with_target(session, body.account, mode, body.mint, int(h["raw"]) if h else 0, meta.get("symbol"),
                                      value, body.share, body.target_price, body.stop_price)
    if not ok:
        raise HTTPException(400, note)
    _cache.pop(f"hold:{pubkey}", None)
    return {"ok": True, "message": note}


class PeakIn(BaseModel):
    mint: str
    since: int          # unix seconds: the high from then until now (when the position was opened)


async def _pool_of(http: httpx.AsyncClient, mint: str) -> str | None:
    """The pair the scanner would price this coin on (DexScreener), cached an hour."""
    from app.scanner_worker import select_primary_pair

    async def fetch() -> str | None:
        r = await http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}", timeout=8)
        pairs = r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
        pair = select_primary_pair(Chain.solana, mint, pairs)
        return pair.get("pairAddress") if pair else None

    return await _cached(f"pool:{mint}", 3600.0, fetch)


class NoGeckoSlot(Exception):
    """GeckoTerminal's shared budget is used up this minute (or it just sent a 429): keep the last value."""


async def peak_since(http: httpx.AsyncClient, mint: str, since: int,
                     budget: GeckoBudget | None = None) -> dict[str, Any] | None:
    """Highest price (USD per token) since `since`, from GeckoTerminal candles: 5-minute candles for up to 3 days,
    15-minute up to 10, hourly beyond. None when the pool or its candles can't be read. Raises NoGeckoSlot when
    the shared GeckoTerminal budget (app.gecko_budget) has no call left, so the caller keeps its last value."""
    pool = await _pool_of(http, mint)
    if not pool:
        return None
    if budget is not None and not await budget.try_take():
        raise NoGeckoSlot(mint)
    age = max(time.time() - since, 60)
    frame, agg = ("minute", 5) if age < 3 * 86400 else ("minute", 15) if age < 10 * 86400 else ("hour", 1)
    r = await http.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/{frame}",
                       params={"aggregate": agg, "limit": 1000, "currency": "usd"},
                       headers={"Accept": "application/json;version=20230302"}, timeout=10)
    if r.status_code == 429 and budget is not None:
        await budget.block()
        raise NoGeckoSlot(mint)
    if r.status_code != 200:
        return None
    rows = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    step = agg * (60 if frame == "minute" else 3600)
    inside = [c for c in rows if isinstance(c, list) and len(c) >= 5 and c[0] + step > since]
    if not inside:
        return None
    best = max(inside, key=lambda c: float(c[2] or 0))
    return {"price": float(best[2]), "at": datetime.fromtimestamp(int(best[0]), timezone.utc).isoformat()}


@router.post("/peaks")
async def peaks(body: list[PeakIn], request: Request, _: Auth) -> dict[str, Any]:
    """{mint: {"price", "at"}}: each coin's highest price since the given time (at most 25 coins, cached a minute)."""
    http: httpx.AsyncClient = request.app.state.http
    budget = GeckoBudget(request.app.state.cache)          # shared with the scanner: same IP, same quota
    out: dict[str, Any] = {}
    for item in body[:25]:
        since = item.since - item.since % 300              # same cache key within 5 minutes
        val = await _cached(f"peak:{item.mint}:{since}", 60.0,
                            lambda i=item, t=since: peak_since(http, i.mint, t, budget))
        if val:
            out[item.mint] = val
    return {"peaks": out}


@router.post("/paper/{strategy}/reset")
async def paper_reset(strategy: str, session: Session, _: Auth) -> dict[str, Any]:
    """Admin: a paper strategy's numbers start from zero (older trades stay in the database, just not counted)."""
    from app.paper import reset_strategy

    try:
        at = await reset_strategy(session, strategy)
    except KeyError:
        raise HTTPException(404, "No such paper strategy") from None
    return {"ok": True, "strategy": strategy, "reset_at": at.isoformat()}


class PaperSwitchIn(BaseModel):
    paused: bool | None = None          # no new paper trades (open ones are still managed)
    hidden: bool | None = None          # left off the /paper page


@router.post("/paper/{strategy}/switch")
async def paper_switch(strategy: str, body: PaperSwitchIn, session: Session, _: Auth) -> dict[str, Any]:
    """Admin: pause a paper strategy or hide it from /paper. Pausing a curve rule on paper does not stop the live
    accounts that trade it."""
    from app import paper_switches
    from app.paper import STRATEGIES, custom_strategies

    if strategy not in STRATEGIES and strategy not in {c.name for c in await custom_strategies(session)}:
        raise HTTPException(404, "No such paper strategy")
    await paper_switches.set_switch(session, strategy, paused=body.paused, hidden=body.hidden)
    await session.commit()
    from app.api.tokens import _PAPER_CACHE

    _PAPER_CACHE.clear()
    return {"ok": True, "strategy": strategy, **body.model_dump(exclude_none=True)}


MAX_CUSTOM_CURVE = 12        # the owner's curve strategies switched on at once


class CurveRuleIn(BaseModel):
    level: float                       # buy the first time the curve is this % sold
    exit_at: float | None = None       # sell all once the curve is this % sold
    tp_pct: float | None = None        # sell all once the price is up this %
    half_at: float | None = None       # sell half at +this %, then a stop at the entry
    sl_pct: float | None = None        # stop loss (read as down: 30 or -30 = -30%)
    skip_drop: float | None = None     # skip the buy if the price fell this % before it landed
    max_secs: float | None = None      # skip a token that took longer than this (from launch) to reach the level
    skip_backslide: bool = False       # skip if the curve slid back below the decision level before the buy
    grad_trail: float | None = None    # at graduation keep riding and sell this % off the high (None = sell)
    size_usd: float = 10.0
    max_hold_min: float = 60.0


def _curve_rule_out(r: Any) -> dict[str, Any]:
    from app.curve import _range_gain, custom_rule

    rule = custom_rule(r)
    return {"id": r.id, "name": r.name, "title": rule.title, "enabled": r.enabled, "level": r.level,
            "exit_at": r.exit_at, "tp_pct": r.tp_pct, "half_at": r.half_at, "sl_pct": r.sl_pct,
            "skip_drop": r.skip_drop, "max_secs": r.max_secs, "skip_backslide": bool(r.skip_backslide),
            "grad_trail": r.grad_trail,
            "size_usd": r.size_usd, "max_hold_min": r.max_hold_min,
            "curve_gain_pct": _range_gain(rule) if r.exit_at else None,
            "created_at": utc(r.created_at).isoformat() if r.created_at else None}


@router.get("/curve-rules")
async def curve_rules(session: Session, _: Auth) -> dict[str, Any]:
    from app.paper import custom_strategies

    return {"rules": [_curve_rule_out(r) for r in await custom_strategies(session)], "max_on": MAX_CUSTOM_CURVE}


@router.post("/curve-rules")
async def curve_rule_create(body: CurveRuleIn, session: Session, _: Auth) -> dict[str, Any]:
    """Admin: a new curve paper strategy. The curve bot picks it up within 30 s; it shows on /paper as cc<id>."""
    from sqlalchemy import func, select

    from app.models import CurveCustom

    if not 1 <= body.level <= 95:
        raise HTTPException(400, "Buy level must be between 1% and 95% sold")
    if body.exit_at is not None and not body.level < body.exit_at <= 100:
        raise HTTPException(400, "Sell level must be above the buy level and at most 100%")
    for v, what in ((body.tp_pct, "Take profit"), (body.half_at, "Half at")):
        if v is not None and not 1 <= v <= 10_000:
            raise HTTPException(400, f"{what} must be between +1% and +10000%")
    sl = -abs(body.sl_pct) if body.sl_pct else None
    if sl is not None and not -95 <= sl <= -1:
        raise HTTPException(400, "Stop loss must be between -1% and -95%")
    if body.skip_drop is not None and not 1 <= body.skip_drop <= 90:
        raise HTTPException(400, "The skip filter must be between 1% and 90%")
    if body.max_secs is not None and not 1 <= body.max_secs <= 86_400:
        raise HTTPException(400, "The speed filter must be between 1 second and 24 hours")
    if body.grad_trail is not None and not 5 <= body.grad_trail <= 90:
        raise HTTPException(400, "The trailing stop after graduation must be between 5% and 90%")
    if not 1 <= body.size_usd <= 1000:
        raise HTTPException(400, "Size must be between $1 and $1000")
    if not 1 <= body.max_hold_min <= 24 * 60:
        raise HTTPException(400, "Max hold must be between 1 minute and 24 hours")
    on = (await session.execute(select(func.count()).select_from(CurveCustom).where(CurveCustom.enabled))).scalar() or 0
    if on >= MAX_CUSTOM_CURVE:
        raise HTTPException(400, f"At most {MAX_CUSTOM_CURVE} of your strategies can be on: switch one off first")
    row = CurveCustom(level=body.level, exit_at=body.exit_at if body.exit_at and body.exit_at < 100 else None,
                      tp_pct=body.tp_pct, half_at=body.half_at, sl_pct=sl, skip_drop=body.skip_drop,
                      max_secs=body.max_secs, skip_backslide=body.skip_backslide or None, grad_trail=body.grad_trail,
                      size_usd=body.size_usd, max_hold_min=body.max_hold_min, enabled=True)
    session.add(row)
    await session.flush()
    row.name = f"cc{row.id}"
    await session.commit()
    return {"ok": True, "rule": _curve_rule_out(row)}


class CurveRuleOnIn(BaseModel):
    enabled: bool


@router.patch("/curve-rules/{rule_id}")
async def curve_rule_switch(rule_id: int, body: CurveRuleOnIn, session: Session, _: Auth) -> dict[str, Any]:
    """Admin: switch one of your curve strategies on or off (off: no new buys; open ones still finish)."""
    from sqlalchemy import func, select

    from app.models import CurveCustom

    row = await session.get(CurveCustom, rule_id)
    if row is None:
        raise HTTPException(404, "No such strategy")
    if body.enabled and not row.enabled:
        on = (await session.execute(select(func.count()).select_from(CurveCustom).where(CurveCustom.enabled))).scalar() or 0
        if on >= MAX_CUSTOM_CURVE:
            raise HTTPException(400, f"At most {MAX_CUSTOM_CURVE} of your strategies can be on: switch one off first")
    row.enabled = body.enabled
    await session.commit()
    return {"ok": True, "rule": _curve_rule_out(row)}


@router.delete("/curve-rules/{rule_id}")
async def curve_rule_delete(rule_id: int, session: Session, _: Auth) -> dict[str, Any]:
    """Admin: remove one of your curve strategies (its paper trades stay in the database)."""
    from app.models import CurveCustom

    row = await session.get(CurveCustom, rule_id)
    if row is None:
        raise HTTPException(404, "No such strategy")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


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


class TargetIn(BaseModel):
    target_pct: float | None = None   # sell `share` once the position is up this % (None = no target)
    share: float | None = 1.0         # 0.05 .. 1.0 of what is left
    stop_pct: float | None = None     # sell everything once it is down this % (negative or positive, read as down)
    target_price: float | None = None  # or by the coin's price, USD per token: sell `share` at this price
    stop_price: float | None = None    # ... and everything at or below this price
    hands_off: bool | None = None      # no bot rule sells it, only the owner's target / stop


@router.post("/positions/{position_id}/target")
async def position_target(position_id: int, body: TargetIn, session: Session, _: Auth) -> dict[str, Any]:
    from app.live import set_position_target

    ok, note = await set_position_target(session, position_id, body.target_pct, body.share, body.stop_pct,
                                         target_price=body.target_price, stop_price=body.stop_price,
                                         hands_off=body.hands_off)
    if not ok:
        raise HTTPException(400, note)
    return {"ok": True, "message": note}


# ---- price orders (/live, Orders tab): buy once a coin reaches a price ---------------------------------
class OrderIn(BaseModel):
    account: str
    mint: str
    condition: str                      # below (buy the dip) | above (buy the breakout)
    trigger_price: float                # USD per token
    amount_usd: float
    max_over_pct: float | None = None   # never pay more than trigger + this % (default LIVE_ORDER_MAX_OVER_PCT)
    target_price: float | None = None   # then sell target_share at this price
    target_share: float | None = None
    stop_price: float | None = None     # then sell everything at / below this
    expires_hours: float | None = None


@router.get("/orders")
async def list_orders(session: Session, _: Auth, all: bool = False) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import PriceOrder
    from app.orders import order_row

    q = select(PriceOrder).order_by(PriceOrder.created_at.desc()).limit(200)
    if not all:
        q = q.where(PriceOrder.status == "open")
    return {"orders": [order_row(o) for o in (await session.execute(q)).scalars().all()]}


@router.post("/orders")
async def place_order(body: OrderIn, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    from app.live import live_status
    from app.orders import create_order, order_row

    s = get_settings()
    mode = s.live_mode if s.live_mode in ("dry", "live") else None
    if mode is None:
        raise HTTPException(400, "Copy trading is off on the server")
    st = await live_status(session, s)
    if not any(a["name"] == body.account for a in st["accounts"]):
        raise HTTPException(404, "No such account")
    http: httpx.AsyncClient = request.app.state.http
    mint = body.mint.strip()
    meta = (await token_meta(http, [mint])).get(mint) or {} if mint else {}
    o, note = await create_order(
        session, body.account, mode, mint, meta.get("symbol"), body.condition, body.trigger_price,
        body.amount_usd, s.live_order_max_over_pct if body.max_over_pct is None else body.max_over_pct,
        body.target_price, body.target_share, body.stop_price, body.expires_hours, meta.get("price"))
    if o is None:
        raise HTTPException(400, note)
    await session.commit()
    return {"ok": True, "message": note, "order": order_row(o)}


@router.post("/orders/{order_id}/cancel")
async def cancel(order_id: int, session: Session, _: Auth) -> dict[str, Any]:
    from app.orders import cancel_order

    ok, note = await cancel_order(session, order_id)
    if not ok:
        raise HTTPException(409, note)
    await session.commit()
    return {"ok": True, "message": note}


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
    prices = await _with_live(await _cached("px:" + ",".join(mints), PRICE_TTL, lambda: jupiter_prices(http, mints)) or {}, mints)
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
    ignore_sells: bool = False           # do not sell when it sells (fast wallets); pair it with quick_exit
    quick_tp_pct: float | None = None    # quick exit for this wallet: +% take profit (None = default)
    quick_sl_pct: float | None = None    # -% stop loss, negative (None = default)
    quick_hours: float | None = None     # time limit in hours (None = default)


class AccountIn(BaseModel):
    name: str
    size_usd: float
    max_open_usd: float
    max_daily_loss_usd: float
    take_capital_at_pct: float | None = 100.0
    max_hold_hours: int | None = None
    leaders: list[LeaderIn] = []
    curve_strategy: str | None = None   # trade this curve rule for real (app/curve_live.py)
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
    curve_strategy: str | None = None   # a curve rule to trade for real; "none" stops it (null: unchanged)
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
        "curve_strategies": _curve_choices(),
    }


def _curve_choices() -> list[dict[str, str]]:
    from app.curve_live import live_rules
    from app.paper import STRATEGIES

    return [{"name": k, "title": STRATEGIES[k].title if k in STRATEGIES else k} for k in live_rules()]


class CurveBotIn(BaseModel):
    enabled: bool
    size_usd: float | None = None       # entry per trade; None = the account's "Entry per trade"
    exit_at: float | None = None        # sell once the curve is this % sold; None = the rule's own exit level
    tp_pct: float | None = None         # and / or at +this % from our fill; None = no profit target


@router.get("/curve-bots")
async def curve_bots(session: Session, _: Auth) -> dict[str, Any]:
    """Curve rules that can trade real money (app/curve_live.py), each account's switch and entry per trade,
    and the last 24 h of each rule on paper and for real."""
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.accounts import bots_of, rows
    from app.curve_live import live_rules
    from app.models import PaperPosition
    from app.paper import STRATEGIES

    s = get_settings()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rules = live_rules()
    paper = {k: (n, pnl) for k, n, pnl in (await session.execute(
        select(PaperPosition.strategy, func.count(PaperPosition.id), func.sum(PaperPosition.pnl_usd)).where(
            PaperPosition.strategy.in_(list(rules)), PaperPosition.entry_time >= since)
        .group_by(PaperPosition.strategy))).all()}
    real: dict[tuple[str, str], tuple[int, float]] = {}
    for acct, lw, n, pnl in (await session.execute(
            select(LivePosition.account, LivePosition.leader_wallet, func.count(LivePosition.id),
                   func.sum(LivePosition.pnl_usd)).where(
                LivePosition.leader_wallet.like("curve:%"), LivePosition.mode == s.live_mode,
                LivePosition.opened_at >= since).group_by(LivePosition.account, LivePosition.leader_wallet))).all():
        real[(acct or "main", lw.split(":", 1)[1])] = (n, float(pnl or 0))
    out_rules = []
    for k, r in rules.items():
        n, pnl = paper.get(k, (0, 0.0))
        out_rules.append({"name": k, "title": STRATEGIES[k].title if k in STRATEGIES else k, "level": r.level,
                          "exit_at": r.exit_at, "sl": r.sl, "max_hold_s": r.max_hold_s,
                          "grad_trail": getattr(r, "grad_trail", None),
                          "paper_24h": {"trades": n, "pnl_usd": round(float(pnl or 0), 2)}})
    accounts = []
    for row in await rows(session, s):
        if not row.enabled:
            continue
        bots = bots_of(row)
        accounts.append({"name": row.name, "size_usd": row.size_usd, "bots": {
            k: {**bots.get(k, {"enabled": False, "size_usd": None, "exit_at": None, "tp_pct": None}),
                "live_24h": dict(zip(("trades", "pnl_usd"), real.get((row.name, k), (0, 0.0))))}
            for k in rules}})
    return {"rules": out_rules, "accounts": accounts}


@router.put("/curve-bots/{account}/{strategy}")
async def set_curve_bot(account: str, strategy: str, body: CurveBotIn, request: Request, session: Session,
                        _: Auth) -> dict[str, Any]:
    """Switch one curve bot on / off for an account and set its entry per trade (real money)."""
    from app.accounts import bots_of
    from app.curve_live import live_rules

    if strategy not in live_rules():
        raise HTTPException(400, f"not a curve rule that can trade for real: {strategy}")
    if body.size_usd is not None and not 1 <= body.size_usd <= 10_000:
        raise HTTPException(400, "entry per trade must be between $1 and $10,000")
    level = live_rules()[strategy].level
    if body.exit_at is not None and not level < body.exit_at <= 100:
        raise HTTPException(400, f"the exit must be a curve level above the entry ({level:g}%) and at most 100%")
    if body.tp_pct is not None and not 1 <= body.tp_pct <= 100_000:
        raise HTTPException(400, "the profit target must be between +1% and +100000%")
    row = await _row(session, account)
    if row.cap_size_usd is not None and (body.size_usd or row.size_usd) > row.cap_size_usd:
        raise HTTPException(400, f"entry per trade is capped at ${row.cap_size_usd:,.0f} on this account")
    bots = bots_of(row)
    bots[strategy] = {"enabled": body.enabled, "size_usd": body.size_usd, "exit_at": body.exit_at,
                      "tp_pct": body.tp_pct}
    row.curve_bots = bots
    row.curve_strategy = None            # the bots replace the single rule
    from app.users import audit

    await audit(session, "admin", "curve_bot", row.name, {"strategy": strategy, **body.model_dump()}, _ip(request))
    await session.commit()
    return {"ok": True, "bots": bots,
            "message": f"{strategy} {'ON' if body.enabled else 'off'} on {row.name}"
                       + (f" · ${body.size_usd:,.2f} per trade" if body.size_usd else "")
                       + (f" · exit at {body.exit_at:g}% of the curve" if body.exit_at else "")
                       + (f" · or +{body.tp_pct:g}% profit" if body.tp_pct else "")}


@router.post("/accounts")
async def create_account(body: AccountIn, session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.accounts import (NAME_RE, AccountError, check_limits, clean_leaders, curve_choice, encrypt_key,
                              ensure_tracked,
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
        curve = curve_choice(body.curve_strategy)
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
                         leaders=leaders, curve_strategy=curve)
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
    from app.accounts import AccountError, check_limits, clean_leaders, curve_choice, ensure_tracked, public

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
        curve = curve_choice(body.curve_strategy) if body.curve_strategy is not None else row.curve_strategy
        if body.enabled is False and counts.get(row.name, 0):
            raise AccountError(f"{counts[row.name]} positions are still open: pause the account instead "
                               "(sells keep working), and disable it once they are closed")
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from None
    row.size_usd, row.max_open_usd, row.max_daily_loss_usd = size, max_open, max_loss
    row.take_capital_at_pct, row.max_hold_hours = take, hold
    row.curve_strategy = curve
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


@router.get("/scout")
async def scout_list(session: Session, _: Auth) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import PaperPosition, WalletCandidate
    from app.wallet_scout import TRIAL_STRATEGY, trial_result

    s = get_settings()
    rows = (await session.execute(select(WalletCandidate).order_by(WalletCandidate.id.desc()).limit(150))).scalars().all()
    out = []
    for c in rows:
        trial = c.trial
        if c.status == "trial" and c.trial_start:          # live numbers while the trial runs
            pos = (await session.execute(select(PaperPosition).where(
                PaperPosition.strategy == TRIAL_STRATEGY, PaperPosition.trigger_wallet == c.address,
                PaperPosition.entry_time >= c.trial_start))).scalars().all()
            trial = trial_result(pos)
        out.append({"id": c.id, "address": c.address, "source": c.source, "hits": c.hits, "status": c.status,
                    "reason": c.reason, "metrics": c.metrics, "trial": trial,
                    "found_at": c.found_at.isoformat() if c.found_at else None,
                    "trial_end": c.trial_end.isoformat() if c.trial_end else None})
    return {"enabled": s.scout_enabled, "hour_utc": s.scout_hour_utc, "candidates": out}


class ScoreIn(BaseModel):
    wallet: str


def _score_out(r: Any) -> dict[str, Any]:
    return {"address": r.address, "passed": r.passed, "reason": r.reason, "metrics": r.metrics, "checks": r.checks,
            "scored_at": r.scored_at.isoformat() if r.scored_at else None}


@router.get("/scorecard")
async def scorecard_list(session: Session, _: Auth) -> dict[str, Any]:
    """The copied wallets (every account) and any looked-up wallet, with their last scorecard (if any)."""
    from sqlalchemy import select

    from app.models import TradingAccount, WalletScore

    leaders: dict[str, dict[str, Any]] = {}
    for a in (await session.execute(select(TradingAccount).order_by(TradingAccount.id))).scalars().all():
        for l in a.leaders or []:
            w = l.get("wallet")
            if not w:
                continue
            d = leaders.setdefault(w, {"label": l.get("label") or "", "accounts": []})
            d["accounts"].append({"account": a.name, "enabled": l.get("enabled", True) is not False})
            d["label"] = d["label"] or l.get("label") or ""
    scores = {r.address: r for r in (await session.execute(select(WalletScore))).scalars().all()}
    out = [{"address": w, "label": d["label"], "accounts": d["accounts"],
            "score": _score_out(scores[w]) if w in scores else None} for w, d in leaders.items()]
    out += [{"address": w, "label": "", "accounts": [], "score": _score_out(r)}
            for w, r in sorted(scores.items(), key=lambda x: x[1].scored_at or 0, reverse=True) if w not in leaders]
    return {"wallets": out}


@router.post("/scorecard")
async def scorecard_run(body: ScoreIn, session: Session, _: Auth) -> dict[str, Any]:
    """Score one wallet now (its swap history from Helius; read-only)."""
    from solders.pubkey import Pubkey

    from app.wallet_scout import score_wallet

    w = body.wallet.strip()
    try:
        Pubkey.from_string(w)
    except Exception:
        raise HTTPException(400, "not a Solana address") from None
    try:
        row = await score_wallet(session, w)
    except Exception as exc:
        raise HTTPException(502, f"could not read the wallet's history: {exc}"[:200]) from None
    return _score_out(row)


class ScoutAddIn(BaseModel):
    account: str = "main"


@router.post("/scout/{cid}/add")
async def scout_add(cid: int, body: ScoutAddIn, request: Request, session: Session, _: Auth) -> dict[str, Any]:
    """Add a candidate to an account's smart wallets, switched OFF: the operator turns it on."""
    from sqlalchemy import select

    from app.accounts import AccountError, clean_leaders, ensure_tracked
    from app.models import TradingAccount, WalletCandidate
    from app.users import audit

    c = await session.get(WalletCandidate, cid)
    a = (await session.execute(select(TradingAccount).where(TradingAccount.name == body.account))).scalars().first()
    if c is None or a is None:
        raise HTTPException(404, "not found")
    leaders = [dict(l) for l in a.leaders or []]
    if not any(l.get("wallet") == c.address for l in leaders):
        net = ((c.trial or {}).get("net_usd") or 0) + ((c.trial or {}).get("open_usd") or 0)
        leaders.append({"wallet": c.address, "label": f"scout {net:+.0f}$ paper"[:40], "enabled": False,
                        "unconditional": True, "new_tokens_only": False, "size_usd": None, "max_chase_pct": 30.0})
        try:
            a.leaders = clean_leaders(leaders)
        except AccountError as exc:
            raise HTTPException(400, str(exc)) from None
        await ensure_tracked(session, [c.address])
        await session.flush()
    from app.models import SmartWallet, WalletSource
    sw = (await session.execute(select(SmartWallet).where(SmartWallet.address == c.address))).scalars().first()
    if sw is not None and sw.source == WalletSource.scout:       # a copied wallet now: normal alerts and label
        sw.source, sw.label, sw.track_buys = WalletSource.manual, "copied", True
    c.status = "added"
    await audit(session, "admin", "scout_add", a.name, {"wallet": c.address}, _ip(request))
    await session.commit()
    return {"ok": True}


@router.post("/scout/{cid}/dismiss")
async def scout_dismiss(cid: int, session: Session, _: Auth) -> dict[str, Any]:
    from app.models import WalletCandidate

    c = await session.get(WalletCandidate, cid)
    if c is None:
        raise HTTPException(404, "not found")
    c.status, c.reason = "rejected", "dismissed by the operator"
    await session.commit()
    return {"ok": True}


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
