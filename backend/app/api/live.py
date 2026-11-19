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
from app.models import LivePosition
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
_cache: dict[str, tuple[float, Any]] = {}


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
    """{mint: {"usd": price, "decimals": n}} from Jupiter's price API (50 ids per call)."""
    s = get_settings()
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(mints), 50):
        r = await http.get(f"{s.jupiter_api_url}/price/v3", params={"ids": ",".join(mints[i:i + 50])}, timeout=8)
        r.raise_for_status()
        for mint, d in (r.json() or {}).items():
            if d and d.get("usdPrice") is not None:
                out[mint] = {"usd": float(d["usdPrice"]), "decimals": int(d.get("decimals") or 0)}
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
