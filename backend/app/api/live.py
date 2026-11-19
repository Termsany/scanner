"""Private live copy-trading status. Every request needs the X-Live-Password header (LIVE_DASHBOARD_PASSWORD)."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from typing import Annotated, Any

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
        "id": p.id, "mode": p.mode, "token_address": p.token_address, "symbol": p.symbol,
        "leader_wallet": p.leader_wallet, "status": p.status, "cost_usd": p.cost_usd, "sol_spent": p.sol_spent,
        "proceeds_usd": p.proceeds_usd, "proceeds_sol": p.proceeds_sol, "pnl_usd": p.pnl_usd,
        "tokens_left": p.tokens_raw, "tokens_bought": p.tokens_bought_raw,
        "buy_signature": p.buy_signature, "fills": p.fills or [], "exit_reason": p.exit_reason, "error": p.error,
        "opened_at": utc(p.opened_at).isoformat() if p.opened_at else None,
        "closed_at": utc(p.closed_at).isoformat() if p.closed_at else None,
    }


@router.get("")
async def live(session: Session, _: Auth) -> dict[str, Any]:
    from app.live import live_status

    s = get_settings()
    st = await live_status(session, s)
    return {
        "mode": st["mode"], "paused": st["paused"], "pubkey": st["pubkey"], "wallets": st["wallets"],
        "size_usd": st["size_usd"], "max_open_usd": st["max_open_usd"],
        "max_daily_loss_usd": s.live_max_daily_loss_usd,
        "realized_usd": st["realized_usd"], "realized_24h": st["realized_24h"],
        "open_cost_usd": sum(p.cost_usd for p in st["open"]),
        "open": [_pos(p) for p in st["open"]],
        "closed": [_pos(p) for p in st["closed"]],
        "failed": [_pos(p) for p in st["failed"]][:30],
    }


class PauseIn(BaseModel):
    paused: bool


@router.post("/pause")
async def pause(body: PauseIn, session: Session, _: Auth) -> dict[str, bool]:
    from app.live import set_paused

    await set_paused(session, body.paused)
    await session.commit()
    return {"paused": body.paused}
