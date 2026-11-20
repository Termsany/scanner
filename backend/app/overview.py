"""Admin overview (/admin page): every account, user and trade at a glance, read-only.

Everything here comes from the database; the API route adds live prices and wallet balances on top.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import LivePosition, Signal, TradingAccount, User

DAYS = 30


def _utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime | None) -> str | None:
    d = _utc(d)
    return d.isoformat() if d else None


def _name(p: LivePosition) -> str:
    return p.account or "main"     # rows from before accounts existed belong to main


async def build_overview(session: AsyncSession, s: Settings, now: datetime | None = None) -> dict[str, Any]:
    from app.accounts import rows as account_rows
    from app.live import HEARTBEAT, PUBKEY, _get, _key, is_paused

    now = now or datetime.now(timezone.utc)
    mode = s.live_mode if s.live_mode in ("dry", "live") else "off"
    d1, d7, d30 = now - timedelta(days=1), now - timedelta(days=7), now - timedelta(days=DAYS)

    users = (await session.execute(select(User))).scalars().all()
    by_id = {u.id: u for u in users}
    accts = await account_rows(session, s)
    positions = (await session.execute(select(LivePosition).where(LivePosition.mode == mode))).scalars().all()

    per: dict[str, list[LivePosition]] = {}
    for p in positions:
        per.setdefault(_name(p), []).append(p)

    start = (now - timedelta(days=DAYS - 1)).date()
    daily = {(start + timedelta(days=i)).isoformat(): {"pnl": 0.0, "trades": 0} for i in range(DAYS)}

    def realized(ps: list[LivePosition], since: datetime | None) -> float:
        return sum(p.pnl_usd or 0 for p in ps if p.status == "closed" and (
            since is None or (p.closed_at and _utc(p.closed_at) >= since)))

    accounts = []
    for r in accts:
        ps = per.get(r.name, [])
        opened = [p for p in ps if p.status == "open"]
        closed = [p for p in ps if p.status == "closed"]
        wins = [p for p in closed if (p.pnl_usd or 0) > 0]
        owner = by_id.get(r.user_id) if r.user_id is not None else None
        last = max((_utc(p.closed_at or p.opened_at) for p in ps if p.closed_at or p.opened_at), default=None)
        beat = await _get(session, _key(HEARTBEAT, r.name))
        accounts.append({
            "name": r.name, "enabled": r.enabled, "paused": bool(r.paused),
            "pubkey": await _get(session, _key(PUBKEY, r.name)) or r.pubkey,
            "owner": ({"id": owner.id, "email": owner.email, "status": owner.status} if owner else None),
            "size_usd": r.size_usd, "max_open_usd": r.max_open_usd,
            "open_count": len(opened), "open_cost_usd": sum(p.cost_usd or 0 for p in opened),
            "open_tokens": [{"token_address": p.token_address, "tokens_left": p.tokens_raw,
                             "proceeds_usd": p.proceeds_usd or 0, "cost_usd": p.cost_usd or 0} for p in opened],
            "realized_24h": realized(ps, d1), "realized_7d": realized(ps, d7), "realized_all": realized(ps, None),
            "trades": len(closed), "wins": len(wins),
            "win_rate": len(wins) / len(closed) * 100 if closed else None,
            "failed_24h": sum(1 for p in ps if p.status == "failed" and _utc(p.opened_at) and _utc(p.opened_at) >= d1),
            "last_trade_at": _iso(last), "heartbeat_at": beat,
        })

    for p in positions:
        if p.status == "closed" and p.closed_at:
            k = _utc(p.closed_at).date().isoformat()
            if k in daily:
                daily[k]["pnl"] += p.pnl_usd or 0
                daily[k]["trades"] += 1

    closed_all = [p for p in positions if p.status == "closed"]
    closed_30 = [p for p in closed_all if p.closed_at and _utc(p.closed_at) >= d30]
    wins_30 = [p for p in closed_30 if (p.pnl_usd or 0) > 0]
    recent = sorted((p for p in positions if p.status in ("closed", "failed")),
                    key=lambda p: _utc(p.closed_at or p.opened_at) or now, reverse=True)[:20]

    sig_24h = (await session.execute(select(func.count(Signal.id)).where(Signal.created_at >= d1))).scalar() or 0
    last_sig = (await session.execute(select(func.max(Signal.created_at)))).scalar()

    count = lambda st: sum(1 for u in users if u.status == st)  # noqa: E731
    return {
        "mode": mode, "paused": await is_paused(session), "updated_at": now.isoformat(),
        "users": {"total": len(users), "pending": count("pending"), "active": count("active"),
                  "blocked": count("blocked"),
                  "new_7d": sum(1 for u in users if u.created_at and _utc(u.created_at) >= d7),
                  "pending_list": [{"id": u.id, "email": u.email, "name": u.name, "created_at": _iso(u.created_at)}
                                   for u in sorted(users, key=lambda u: u.id, reverse=True)
                                   if u.status == "pending"][:10]},
        "totals": {
            "accounts": len(accts), "enabled": sum(1 for r in accts if r.enabled),
            "paused": sum(1 for r in accts if r.paused),
            "open_count": sum(a["open_count"] for a in accounts),
            "open_cost_usd": sum(a["open_cost_usd"] for a in accounts),
            "realized_24h": sum(a["realized_24h"] for a in accounts),
            "realized_7d": sum(a["realized_7d"] for a in accounts),
            "realized_30d": sum(p.pnl_usd or 0 for p in closed_30),
            "realized_all": sum(a["realized_all"] for a in accounts),
            "trades_30d": len(closed_30), "wins_30d": len(wins_30),
            "win_rate_30d": len(wins_30) / len(closed_30) * 100 if closed_30 else None,
            "failed_24h": sum(a["failed_24h"] for a in accounts),
        },
        "accounts": accounts,
        "daily": [{"day": k, "pnl": round(v["pnl"], 2), "trades": int(v["trades"])} for k, v in daily.items()],
        "recent": [{
            "id": p.id, "account": _name(p), "symbol": p.symbol, "token_address": p.token_address,
            "status": p.status, "cost_usd": p.cost_usd, "pnl_usd": p.pnl_usd,
            "pnl_pct": (p.pnl_usd or 0) / p.cost_usd * 100 if p.cost_usd and p.pnl_usd is not None else None,
            "exit_reason": p.exit_reason, "error": p.error,
            "at": _iso(p.closed_at or p.opened_at),
        } for p in recent],
        "signals": {"last_24h": int(sig_24h), "last_at": _iso(last_sig)},
    }
