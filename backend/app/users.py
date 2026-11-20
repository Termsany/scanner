"""Registered users: password hashing, login sessions (httpOnly cookie), and the audit log.

Users register, then wait until the operator approves them on /accounts; approval gives them a trading account
with a fresh wallet. They never see which smart wallets their account copies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import AuditLog, User, UserSession

COOKIE = "alpha_session"
EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}")
MIN_PASSWORD = 10
_N, _R, _P = 2**14, 8, 1           # scrypt cost


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


_DUMMY = hash_password(secrets.token_hex(8))   # equal work for unknown emails (no user enumeration by timing)


def password_problem(pw: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if len(pw) > 200:
        return "password is too long"
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def new_session(session: AsyncSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(UserSession(token_hash=_token_hash(token), user_id=user.id,
                            expires_at=_now() + timedelta(days=get_settings().user_session_days)))
    await session.execute(delete(UserSession).where(UserSession.expires_at < _now()))
    return token


async def end_session(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))


async def end_all_sessions(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))


def set_cookie(response, token: str) -> None:
    s = get_settings()
    response.set_cookie(COOKIE, token, max_age=s.user_session_days * 86400, httponly=True,
                        secure=s.auth_cookie_secure, samesite="strict", path="/")


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, path="/")


# ---- brute-force limits (per IP and per email) ----------------------------------
WINDOW = 15 * 60
MAX_FAILS = 8
_fails: dict[str, deque[float]] = defaultdict(deque)


def ip_of(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def too_many(*keys: str) -> bool:
    now = time.monotonic()
    for k in keys:
        q = _fails[k]
        while q and now - q[0] > WINDOW:
            q.popleft()
        if len(q) >= MAX_FAILS:
            return True
    return False


def failed(*keys: str) -> None:
    for k in keys:
        _fails[k].append(time.monotonic())


def cleared(*keys: str) -> None:
    for k in keys:
        _fails.pop(k, None)


# ---- current user ----------------------------------------------------------------
async def current_user(request: Request, session: Annotated[AsyncSession, Depends(get_session)],
                       alpha_session: Annotated[str | None, Cookie()] = None) -> User:
    if not get_settings().users_enabled:
        raise HTTPException(404, "Accounts are not open")
    if not alpha_session:
        raise HTTPException(401, "Not signed in")
    row = await session.get(UserSession, _token_hash(alpha_session))
    if row is None or _as_utc(row.expires_at) < _now():
        raise HTTPException(401, "Session expired, sign in again")
    user = await session.get(User, row.user_id)
    if user is None or user.status == "blocked":
        raise HTTPException(403, "This account is blocked")
    return user


def _as_utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


CurrentUser = Annotated[User, Depends(current_user)]


async def audit(session: AsyncSession, actor: str, action: str, target: str = "",
                details: dict[str, Any] | None = None, ip: str = "") -> None:
    session.add(AuditLog(actor=actor[:80], action=action[:40], target=target[:80], details=details, ip=ip[:64]))


async def user_by_email(session: AsyncSession, email: str) -> User | None:
    return (await session.execute(select(User).where(User.email == email))).scalars().first()
