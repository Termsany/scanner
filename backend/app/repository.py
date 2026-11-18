"""Read-side queries + ORM -> schema conversion shared by the API, AI analyst and Telegram bot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Chain, ScannerRun, ScoreRecord, SecurityAudit, Signal, Token
from app.schemas import (
    MarketMetrics,
    PaginatedTokens,
    ScannerStatus,
    ScoreBreakdown,
    ScorePoint,
    SecurityBadges,
    SecurityReport,
    SignalOut,
    SortField,
    SortOrder,
    TokenDetail,
    TokenListParams,
    TokenSummary,
    TopGainer,
)


def utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything we store is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_address(chain: Chain, address: str) -> str:
    return address if chain == Chain.solana else address.lower()


def _gain(current: float | None, entry: float | None) -> float | None:
    if not current or not entry or entry <= 0:
        return None
    return round((current / entry - 1.0) * 100.0, 2)


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def to_summary(t: Token) -> TokenSummary:
    return TokenSummary(
        id=t.id, chain=t.chain, address=t.address, symbol=t.symbol, name=t.name, image_url=t.image_url,
        pair_url=t.pair_url, dex_id=t.dex_id, pair_created_at=utc(t.pair_created_at), price_usd=t.price_usd,
        price_change_m5=t.price_change_m5, price_change_h1=t.price_change_h1, price_change_h24=t.price_change_h24,
        liquidity_usd=t.liquidity_usd, volume_h1=t.volume_h1, volume_h24=t.volume_h24, market_cap=t.market_cap,
        fdv=t.fdv, alpha_score=t.alpha_score, badge=t.badge, smart_money_hits=t.smart_money_hits,
        badges=SecurityBadges(
            lp_locked=None if t.lp_locked_pct is None else t.lp_locked_pct > 80.0,
            no_mint=None if t.is_mintable is None else not t.is_mintable,
            no_freeze=None if t.is_freezable is None else not t.is_freezable,
            honeypot=t.is_honeypot,
            buy_tax=t.buy_tax,
            sell_tax=t.sell_tax,
        ),
        last_scanned_at=utc(t.last_scanned_at),
    )


def to_metrics(t: Token) -> MarketMetrics:
    return MarketMetrics(
        chain=t.chain, address=t.address, symbol=t.symbol, name=t.name, image_url=t.image_url,
        pair_address=t.pair_address, dex_id=t.dex_id, pair_url=t.pair_url, quote_symbol=t.quote_symbol,
        pair_created_at=utc(t.pair_created_at), price_usd=t.price_usd, price_change_m5=t.price_change_m5,
        price_change_h1=t.price_change_h1, price_change_h6=t.price_change_h6, price_change_h24=t.price_change_h24,
        liquidity_usd=t.liquidity_usd, volume_m5=t.volume_m5, volume_h1=t.volume_h1, volume_h6=t.volume_h6,
        volume_h24=t.volume_h24, buys_m5=t.buys_m5, sells_m5=t.sells_m5, buys_h1=t.buys_h1, sells_h1=t.sells_h1,
        buys_h24=t.buys_h24, sells_h24=t.sells_h24, fdv=t.fdv, market_cap=t.market_cap,
        boosts_active=t.boosts_active or 0, discovery_sources=list(t.discovery_sources or []),
    )


def to_security(t: Token, a: SecurityAudit) -> SecurityReport:
    return SecurityReport(
        chain=t.chain, address=t.address, providers=list(a.providers or []), is_honeypot=a.is_honeypot,
        is_mintable=a.is_mintable, is_freezable=a.is_freezable, is_open_source=a.is_open_source,
        is_proxy=a.is_proxy, owner_can_change_balance=a.owner_can_change_balance, hidden_owner=a.hidden_owner,
        cannot_sell_all=a.cannot_sell_all, transfer_pausable=a.transfer_pausable, buy_tax=a.buy_tax,
        sell_tax=a.sell_tax, lp_locked_pct=a.lp_locked_pct, top10_holders_pct=a.top10_holders_pct,
        holder_count=a.holder_count, creator_pct=a.creator_pct, rugcheck_score=a.rugcheck_score,
        risks=a.risks or [], top_holders=a.top_holders or [], fetched_at=utc(a.fetched_at),
    )


def to_signal_out(sig: Signal, t: Token) -> SignalOut:
    return SignalOut(
        id=sig.id, chain=t.chain, address=t.address, symbol=t.symbol, score=sig.score, badge=sig.badge,
        price_usd=sig.price_usd, current_price_usd=t.price_usd, gain_pct=_gain(t.price_usd, sig.price_usd),
        created_at=utc(sig.created_at), payload=sig.payload or {},
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_SORT_COLUMNS = {
    SortField.score: Token.alpha_score,
    SortField.volume: Token.volume_h24,
    SortField.liquidity: Token.liquidity_usd,
    SortField.age: Token.pair_created_at,
    SortField.change_1h: Token.price_change_h1,
    SortField.volume_1h: Token.volume_h1,
}


def _active_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=get_settings().active_window_minutes)


def _list_filters(p: TokenListParams) -> list:
    conds = [Token.last_scanned_at >= _active_cutoff()]
    if p.chain is not None:
        conds.append(Token.chain == p.chain)
    if p.min_score:
        conds.append(Token.alpha_score >= p.min_score)
    if p.min_liquidity:
        conds.append(Token.liquidity_usd >= p.min_liquidity)
    if p.badge is not None:
        conds.append(Token.badge == p.badge)
    if p.q:
        q = p.q.strip()
        like = f"%{q.lower()}%"
        conds.append(or_(func.lower(Token.symbol).like(like), func.lower(Token.name).like(like),
                         Token.address == q, Token.address == q.lower()))
    return conds


async def list_tokens(session: AsyncSession, p: TokenListParams) -> PaginatedTokens:
    conds = _list_filters(p)
    total = (await session.execute(select(func.count(Token.id)).where(and_(*conds)))).scalar_one()

    col = _SORT_COLUMNS[p.sort]
    # "age desc" = youngest first: newest pair_created_at first, which is the same as column desc.
    ordered = col.desc().nulls_last() if p.order == SortOrder.desc else col.asc().nulls_last()
    stmt: Select = (
        select(Token).where(and_(*conds)).order_by(ordered, Token.id.desc())
        .offset((p.page - 1) * p.page_size).limit(p.page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return PaginatedTokens(items=[to_summary(t) for t in rows], page=p.page, page_size=p.page_size, total=total)


async def get_token(session: AsyncSession, chain: Chain, address: str) -> Token | None:
    return (await session.execute(
        select(Token).where(Token.chain == chain, Token.address == normalize_address(chain, address))
    )).scalar_one_or_none()


async def token_detail(session: AsyncSession, token: Token, history_points: int = 120) -> TokenDetail:
    audit = (await session.execute(
        select(SecurityAudit).where(SecurityAudit.token_id == token.id)
    )).scalar_one_or_none()
    records = (await session.execute(
        select(ScoreRecord).where(ScoreRecord.token_id == token.id)
        .order_by(ScoreRecord.scored_at.desc()).limit(history_points)
    )).scalars().all()
    latest = ScoreBreakdown.model_validate(records[0].breakdown) if records else None
    return TokenDetail(
        token=to_summary(token),
        metrics=to_metrics(token),
        security=to_security(token, audit) if audit else None,
        score=latest,
        score_history=[ScorePoint(total=r.total, scored_at=utc(r.scored_at)) for r in reversed(records)],
    )


async def recent_signals(
    session: AsyncSession, *, limit: int = 50, chain: Chain | None = None, min_score: int = 0,
    since_id: int | None = None,
) -> list[SignalOut]:
    stmt = select(Signal, Token).join(Token, Token.id == Signal.token_id)
    if chain is not None:
        stmt = stmt.where(Token.chain == chain)
    if min_score:
        stmt = stmt.where(Signal.score >= min_score)
    if since_id is not None:
        stmt = stmt.where(Signal.id > since_id).order_by(Signal.id.asc())
    else:
        stmt = stmt.order_by(Signal.id.desc())
    rows = (await session.execute(stmt.limit(limit))).all()
    return [to_signal_out(sig, tok) for sig, tok in rows]


async def scanner_status(session: AsyncSession) -> ScannerStatus:
    s = get_settings()
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    last = (await session.execute(select(ScannerRun).order_by(ScannerRun.id.desc()).limit(1))).scalar_one_or_none()
    last_at = utc(last.finished_at or last.started_at) if last else None
    live = bool(last_at and (now - last_at).total_seconds() <= s.scan_interval_seconds * 3)

    hour_ago = now - timedelta(hours=1)
    active = (await session.execute(
        select(func.count(Token.id)).where(Token.last_scanned_at >= hour_ago)
    )).scalar_one()
    audited = (await session.execute(
        select(func.count(Token.id)).join(SecurityAudit, SecurityAudit.token_id == Token.id)
        .where(Token.last_scanned_at >= hour_ago)
    )).scalar_one()
    cycle_s = None
    if last and last.finished_at and last.started_at:
        cycle_s = round((utc(last.finished_at) - utc(last.started_at)).total_seconds(), 1)
    signals_24h = (await session.execute(
        select(func.count(Signal.id)).where(Signal.created_at >= day_ago)
    )).scalar_one()
    high_alpha = (await session.execute(
        select(func.count(Signal.id)).where(Signal.created_at >= day_ago, Signal.score >= 80)
    )).scalar_one()

    top: TopGainer | None = None
    rows = (await session.execute(
        select(Signal, Token).join(Token, Token.id == Signal.token_id).where(Signal.created_at >= day_ago)
    )).all()
    for sig, tok in rows:
        g = _gain(tok.price_usd, sig.price_usd)
        if g is not None and (top is None or g > top.gain_pct):
            top = TopGainer(chain=tok.chain, address=tok.address, symbol=tok.symbol,
                            signal_price_usd=sig.price_usd, current_price_usd=tok.price_usd, gain_pct=g,
                            signalled_at=utc(sig.created_at))

    return ScannerStatus(
        live=live, last_run_at=last_at, last_run_ok=last.ok if last else None,
        last_run_scored=last.scored if last else None,
        last_run_candidates=last.candidates if last else None,
        last_run_errors=len(last.errors or []) if last else None,
        last_cycle_seconds=cycle_s,
        security_coverage_pct=round(audited / active * 100, 1) if active else None,
        scan_interval_seconds=s.scan_interval_seconds,
        active_tokens=active, high_alpha_24h=high_alpha, signals_24h=signals_24h, top_gainer=top,
    )
