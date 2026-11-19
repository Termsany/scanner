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
        "id": p.id, "mode": p.mode, "account": p.account or "main", "token_address": p.token_address, "symbol": p.symbol,
        "leader_wallet": p.leader_wallet, "status": p.status, "cost_usd": p.cost_usd, "sol_spent": p.sol_spent,
        "proceeds_usd": p.proceeds_usd, "proceeds_sol": p.proceeds_sol, "pnl_usd": p.pnl_usd,
        "tokens_left": p.tokens_raw, "tokens_bought": p.tokens_bought_raw,
        "buy_signature": p.buy_signature, "manual_sell_frac": p.manual_sell_frac, "fills": p.fills or [], "exit_reason": p.exit_reason, "error": p.error,
        "opened_at": utc(p.opened_at).isoformat() if p.opened_at else None,
        "closed_at": utc(p.closed_at).isoformat() if p.closed_at else None,
    }


@router.get("")
async def live(session: Session, _: Auth) -> dict[str, Any]:
    from app.live import live_status

    st = await live_status(session, get_settings())
    return {
        "mode": st["mode"], "paused": st["paused"],
        "accounts": [
            {**{k: v for k, v in a.items() if k not in ("open", "closed", "failed")},
             "open": [_pos(p) for p in a["open"]],
             "closed": [_pos(p) for p in a["closed"]],
             "failed": [_pos(p) for p in a["failed"]][:30]}
            for a in st["accounts"]
        ],
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
