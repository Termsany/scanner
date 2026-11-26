"""Token table, token inspector, chart data, signals and scanner status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import Cache
from app.db import get_session
from app.gecko_budget import GeckoBudget
from app.models import Chain, MarketSnapshot, SmartWallet, WalletSource
from app.repository import get_token, list_tokens, recent_signals, scanner_status, token_detail, utc
from app.scanner_worker import GECKO_NETWORK
from app.schemas import (
    Candle,
    ChartData,
    PaginatedTokens,
    ScannerStatus,
    SignalOut,
    SmartWalletList,
    SmartWalletOut,
    TokenDetail,
    TokenListParams,
)

router = APIRouter(prefix="/api", tags=["tokens"])
Session = Annotated[AsyncSession, Depends(get_session)]

# interval -> (GeckoTerminal timeframe, aggregate, seconds per candle)
CHART_INTERVALS: dict[str, tuple[str, int, int]] = {
    "1m": ("minute", 1, 60),
    "5m": ("minute", 5, 300),
    "15m": ("minute", 15, 900),
    "1h": ("hour", 1, 3_600),
    "4h": ("hour", 4, 14_400),
    "1d": ("day", 1, 86_400),
}


@router.get("/tokens", response_model=PaginatedTokens)
async def get_tokens(session: Session, params: Annotated[TokenListParams, Query()]) -> PaginatedTokens:
    """Paginated scanner table. `sort=age&order=desc` returns the youngest pairs first."""
    return await list_tokens(session, params)


@router.get("/tokens/{chain}/{address}", response_model=TokenDetail)
async def get_token_detail(chain: Chain, address: str, session: Session) -> TokenDetail:
    token = await get_token(session, chain, address)
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found or not yet scanned")
    return await token_detail(session, token)


@router.get("/tokens/{chain}/{address}/chart", response_model=ChartData)
async def get_token_chart(
    chain: Chain,
    address: str,
    request: Request,
    session: Session,
    interval: Annotated[str, Query(pattern="^(1m|5m|15m|1h|4h|1d)$")] = "5m",
    limit: Annotated[int, Query(ge=10, le=1000)] = 300,
) -> ChartData:
    """OHLCV for TradingView Lightweight Charts. GeckoTerminal first, scanner snapshots as fallback."""
    token = await get_token(session, chain, address)
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found or not yet scanned")

    cache: Cache = request.app.state.cache
    key = f"chart:v1:{chain.value}:{token.pair_address}:{interval}:{limit}"
    cached = await cache.get_json(key)
    if cached:
        return ChartData.model_validate(cached)

    timeframe, aggregate, step = CHART_INTERVALS[interval]
    candles: list[Candle] = []
    # GeckoTerminal's keyless quota (~30/min) is shared with the scanner's discovery calls: one budget for both
    # (app.gecko_budget); with no slot left this minute, or after a 429, serve snapshots instead.
    budget = GeckoBudget(cache)
    if token.pair_address and await budget.try_take():
        http: httpx.AsyncClient = request.app.state.http
        url = (f"https://api.geckoterminal.com/api/v2/networks/{GECKO_NETWORK[chain]}/pools/"
               f"{token.pair_address}/ohlcv/{timeframe}")
        try:
            resp = await http.get(url, params={"aggregate": aggregate, "limit": limit, "currency": "usd"},
                                  headers={"Accept": "application/json;version=20230302"})
            if resp.status_code == 200:
                rows = ((resp.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                candles = [Candle(time=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                                  close=float(r[4]), volume=float(r[5] or 0)) for r in rows if len(r) >= 6]
                candles.sort(key=lambda c: c.time)
            elif resp.status_code == 429:
                await budget.block()
        except (httpx.HTTPError, ValueError, TypeError):
            candles = []

    if candles:
        data = ChartData(source="geckoterminal", timeframe=interval, candles=candles)
        await cache.set_json(key, data.model_dump(mode="json"), ttl_seconds=min(step, 60))
        return data

    # Fallback: bucket our own per-cycle price snapshots into candles.
    since = datetime.now(timezone.utc) - timedelta(seconds=step * limit)
    snaps = (await session.execute(
        select(MarketSnapshot.captured_at, MarketSnapshot.price_usd, MarketSnapshot.volume_h1)
        .where(MarketSnapshot.token_id == token.id, MarketSnapshot.captured_at >= since,
               MarketSnapshot.price_usd.is_not(None))
        .order_by(MarketSnapshot.captured_at.asc())
    )).all()
    buckets: dict[int, Candle] = {}
    for ts, price, _vol in snaps:
        t = int(utc(ts).timestamp()) // step * step
        c = buckets.get(t)
        if c is None:
            buckets[t] = Candle(time=t, open=price, high=price, low=price, close=price)
        else:
            c.high, c.low, c.close = max(c.high, price), min(c.low, price), price
    return ChartData(source="snapshots", timeframe=interval, candles=list(buckets.values()))


@router.get("/signals", response_model=list[SignalOut])
async def get_signals(
    session: Session,
    chain: Chain | None = None,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SignalOut]:
    """Most recent signals with performance since the signal fired."""
    return await recent_signals(session, limit=limit, chain=chain, min_score=min_score)


@router.get("/smart-wallets", response_model=SmartWalletList)
async def get_smart_wallets(
    request: Request,
    session: Session,
    include_disabled: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> SmartWalletList:
    """Tracked wallets (read-only; manage them with `python -m app.smart_money` on the server)."""
    grouped = (await session.execute(
        select(SmartWallet.source, SmartWallet.enabled, func.count()).group_by(SmartWallet.source, SmartWallet.enabled)
    )).all()
    counts = {(src, en): n for src, en, n in grouped}
    q = select(SmartWallet).order_by(SmartWallet.enabled.desc(), SmartWallet.wins.desc(), SmartWallet.id.asc())
    if not include_disabled:  # tracked (buy-alert) wallets are listed even when they are watch-only
        q = q.where(or_(SmartWallet.enabled.is_(True), SmartWallet.track_buys.is_(True)))
    rows = (await session.execute(q.limit(limit))).scalars().all()
    return SmartWalletList(
        items=[SmartWalletOut(chain=w.chain, address=w.address, label=w.label, source=w.source.value,
                              enabled=w.enabled, wins=w.wins, appearances=w.appearances,
                              win_tokens=w.win_tokens or [], stats=w.stats, track_buys=bool(w.track_buys), updated_at=utc(w.updated_at)) for w in rows],
        enabled_manual=counts.get((WalletSource.manual, True), 0),
        enabled_auto=counts.get((WalletSource.auto, True), 0),
        enabled_birdeye=counts.get((WalletSource.birdeye, True), 0),
        disabled=sum(n for (src, en), n in counts.items() if not en),
        telegram_bot=getattr(getattr(request.app.state, "telegram", None), "username", None),
    )


@router.get("/performance")
async def get_performance(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24 * 7,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
) -> dict:
    """How signals performed 1h / 6h / 24h after firing (GeckoTerminal candles; includes tokens that crashed)."""
    from app.performance import performance_summary

    return await performance_summary(session, hours=hours, min_score=min_score)


_PAPER_CACHE: dict[int, tuple[float, dict]] = {}
_PAPER_LOCKS: dict[int, "asyncio.Lock"] = {}
PAPER_CACHE_SECONDS = 2.0     # /paper refreshes every 3 s: one build serves every viewer within this window


@router.get("/paper")
async def get_paper(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24 * 7,
) -> dict:
    """Paper-trading results per strategy (simulated $ size per trade, slippage on both fills)."""
    import asyncio
    import time

    from app.paper import paper_summary

    hit = _PAPER_CACHE.get(hours)
    if hit and time.monotonic() - hit[0] < PAPER_CACHE_SECONDS:
        return hit[1]
    lock = _PAPER_LOCKS.setdefault(hours, asyncio.Lock())
    async with lock:                       # concurrent requests wait for one build instead of each running it
        hit = _PAPER_CACHE.get(hours)
        if hit and time.monotonic() - hit[0] < PAPER_CACHE_SECONDS:
            return hit[1]
        data = await paper_summary(session, hours=hours)
        _PAPER_CACHE[hours] = (time.monotonic(), data)
        return data


_SCORECARD_CACHE: dict[int, tuple[float, dict]] = {}
SCORECARD_CACHE_SECONDS = 30.0   # closed trades only: no need for the 3 s refresh of /paper


@router.get("/paper/scorecard")
async def get_paper_scorecard(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24 * 30,
) -> dict:
    """Per paper strategy: expectancy, median, profit factor, 95% range, drawdown, verdict (app/scorecard.py)."""
    import time

    from app.scorecard import paper_scorecard

    hit = _SCORECARD_CACHE.get(hours)
    if hit and time.monotonic() - hit[0] < SCORECARD_CACHE_SECONDS:
        return hit[1]
    data = await paper_scorecard(session, hours=hours)
    _SCORECARD_CACHE[hours] = (time.monotonic(), data)
    return data


@router.get("/status", response_model=ScannerStatus)
async def get_status(session: Session) -> ScannerStatus:
    """KPI bar: active tokens, 24h high-alpha signals, top gainer caught, live pulse."""
    return await scanner_status(session)
