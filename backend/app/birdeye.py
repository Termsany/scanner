"""Profitable-trader discovery with the Birdeye Data API (https://data.birdeye.so/docs).

Free "Standard" plan: 30,000 compute units / month, 30 CU per call (~1,000 calls), 1 request / second.
Every call is recorded in `birdeye_calls`, and a rolling 24h budget (BIRDEYE_DAILY_CALL_BUDGET, default
30) is enforced before each request, so the free allowance is never exceeded.

Pipeline (runs every BIRDEYE_INTERVAL_MINUTES inside the scanner worker):
  1. Seed       GET /trader/gainers-losers (type=1W, sort_by=PnL) once a day per chain.
                Wallets with more than BIRDEYE_SEED_MAX_TRADES_WEEK trades are bots / market makers.
  2. Winners    GET /defi/v2/tokens/top_traders (sort_by=total_pnl) once per token that rallied
                >= SMART_AUTO_WIN_GAIN_PCT after one of our signals. Wallets tagged dev / bundler /
                sniper / insider are dropped.
  3. Verify     GET /wallet/v2/pnl/summary (duration=30d) for the best unverified candidates.
                A wallet qualifies with win rate >= BIRDEYE_MIN_WIN_RATE, realized profit >=
                BIRDEYE_MIN_REALIZED_USD_30D and a human-plausible trade count. Qualified wallets
                become SmartWallet rows with source="birdeye"; re-verified every BIRDEYE_REVERIFY_DAYS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import (
    BirdeyeCall,
    Chain,
    SmartWallet,
    TraderCandidate,
    TraderTokenFetch,
    WalletSource,
)
from app.repository import normalize_address, utc

log = logging.getLogger("birdeye")

BASE_URL = "https://public-api.birdeye.so"
X_CHAIN = {Chain.solana: "solana", Chain.ethereum: "ethereum", Chain.base: "base", Chain.bsc: "bsc"}
EXCLUDED_TAGS = {"dev", "bundler", "sniper", "insider"}


class BudgetExhausted(Exception):
    pass


class BirdeyeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Birdeye HTTP {status}: {message}")
        self.status = status


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class BirdeyeClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        if not settings.birdeye_api_key:
            raise ValueError("BIRDEYE_API_KEY is not set")
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._own_http = http is None
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._own_http:
            await self.http.aclose()

    async def calls_last_24h(self, session: AsyncSession) -> int:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        return (await session.execute(
            select(func.count(BirdeyeCall.id)).where(BirdeyeCall.created_at >= since)
        )).scalar_one()

    async def remaining(self, session: AsyncSession) -> int:
        return max(0, self.settings.birdeye_daily_call_budget - await self.calls_last_24h(session))

    async def get(self, session: AsyncSession, path: str, chain: Chain, params: dict[str, Any]) -> dict[str, Any]:
        if await self.remaining(session) <= 0:
            raise BudgetExhausted()
        async with self._lock:  # free plan: 1 request / second per account
            wait = 1.1 - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            status = 0
            try:
                resp = await self.http.get(
                    BASE_URL + path, params=params,
                    headers={"X-API-KEY": self.settings.birdeye_api_key, "x-chain": X_CHAIN[chain],
                             "accept": "application/json"},
                )
                status = resp.status_code
            finally:
                self._last_call = time.monotonic()
                session.add(BirdeyeCall(endpoint=path, chain=chain.value, status=status))
                await session.flush()
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if status != 200 or not body.get("success", False):
            raise BirdeyeError(status, str(body.get("message") or resp.text[:120]))
        return body.get("data") or {}

    async def gainers(self, session: AsyncSession, chain: Chain, limit: int) -> list[dict[str, Any]]:
        data = await self.get(session, "/trader/gainers-losers", chain,
                              {"type": "1W", "sort_by": "PnL", "sort_type": "desc", "offset": 0,
                               "limit": min(limit, 100)})
        return data.get("items") or []

    async def top_traders(self, session: AsyncSession, chain: Chain, token: str) -> list[dict[str, Any]]:
        data = await self.get(session, "/defi/v2/tokens/top_traders", chain,
                              {"address": token, "time_frame": "7d", "sort_by": "total_pnl",
                               "sort_type": "desc", "offset": 0, "limit": 10})
        return data.get("items") or []

    async def pnl_summary(self, session: AsyncSession, chain: Chain, wallet: str) -> dict[str, Any]:
        data = await self.get(session, "/wallet/v2/pnl/summary", chain, {"wallet": wallet, "duration": "30d"})
        return data.get("summary") or {}


# ---------------------------------------------------------------------------
# Verification rules (pure, unit-tested)
# ---------------------------------------------------------------------------


def normalize_win_rate(v: Any) -> float:
    """Birdeye documents win_rate without a unit; accept 0-1 or 0-100."""
    r = _f(v)
    return r / 100.0 if r > 1.0 else r


def evaluate_summary(summary: dict[str, Any], s: Settings) -> tuple[bool, str, dict[str, Any]]:
    counts = summary.get("counts") or {}
    pnl = summary.get("pnl") or {}
    win_rate = normalize_win_rate(counts.get("win_rate"))
    trades = int(_f(counts.get("total_trade")))
    realized = _f(pnl.get("realized_profit_usd"))
    stats = {
        "win_rate": round(win_rate, 3),
        "realized_usd_30d": round(realized, 2),
        "total_usd_30d": round(_f(pnl.get("total_usd")), 2),
        "trades_30d": trades,
        "unique_tokens_30d": int(_f(summary.get("unique_tokens"))),
        "wins_30d": int(_f(counts.get("total_win"))),
        "losses_30d": int(_f(counts.get("total_loss"))),
    }
    if trades < s.birdeye_min_trades_30d:
        return False, f"too few trades ({trades})", stats
    if trades > s.birdeye_max_trades_30d:
        return False, f"bot-like trade count ({trades})", stats
    if realized < s.birdeye_min_realized_usd_30d:
        return False, f"realized ${realized:,.0f} < ${s.birdeye_min_realized_usd_30d:,.0f}", stats
    if win_rate < s.birdeye_min_win_rate:
        return False, f"win rate {win_rate:.0%} < {s.birdeye_min_win_rate:.0%}", stats
    return True, "qualified", stats


def wallet_label(stats: dict[str, Any]) -> str:
    usd = stats.get("realized_usd_30d", 0.0)
    amount = f"${usd / 1e6:.1f}M" if usd >= 1e6 else f"${usd / 1e3:.0f}K"
    return f"BE {stats.get('win_rate', 0):.0%} · {amount} 30d"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def _candidate(session: AsyncSession, chain: Chain, address: str) -> TraderCandidate:
    addr = normalize_address(chain, address)
    c = (await session.execute(
        select(TraderCandidate).where(TraderCandidate.chain == chain, TraderCandidate.address == addr)
    )).scalar_one_or_none()
    if c is None:
        c = TraderCandidate(chain=chain, address=addr, origins=[], winner_hits=[], best_pnl_usd=0.0)
        session.add(c)
        await session.flush()
    return c


async def _seed(session: AsyncSession, client: BirdeyeClient, chains: list[Chain], stats: dict[str, int]) -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    for chain in chains:
        done = (await session.execute(
            select(func.count(BirdeyeCall.id)).where(
                BirdeyeCall.endpoint == "/trader/gainers-losers", BirdeyeCall.chain == chain.value,
                BirdeyeCall.created_at >= since, BirdeyeCall.status == 200)
        )).scalar_one()
        if done:
            continue
        items = await client.gainers(session, chain, client.settings.birdeye_seed_limit)
        for it in items:
            addr = str(it.get("address") or "")
            trades = int(_f(it.get("trade_count")))
            pnl = _f(it.get("pnl"))
            if not addr or trades > client.settings.birdeye_seed_max_trades_week or pnl <= 0:
                continue
            c = await _candidate(session, chain, addr)
            if "gainers_1W" not in c.origins:
                c.origins = [*c.origins, "gainers_1W"]
            c.best_pnl_usd = max(c.best_pnl_usd, pnl)
            stats["seeded"] += 1


async def _winners(session: AsyncSession, client: BirdeyeClient, settings: Settings, stats: dict[str, int]) -> None:
    from app.smart_money import SOLANA_NON_TRADERS, winner_tokens

    fetched = {(f.chain, f.token_address) for f in (await session.execute(select(TraderTokenFetch))).scalars()}
    for tok, gain in await winner_tokens(session, settings):
        if (tok.chain, tok.address) in fetched or tok.chain not in X_CHAIN:
            continue
        items = await client.top_traders(session, tok.chain, tok.address)
        kept = 0
        for it in items:
            addr = str(it.get("owner") or "")
            tags = {str(t).lower() for t in (it.get("tags") or [])}
            pnl = _f(it.get("totalPnl"))
            if not addr or tags & EXCLUDED_TAGS or addr in SOLANA_NON_TRADERS:
                continue
            if pnl < settings.birdeye_min_token_pnl_usd:
                continue
            c = await _candidate(session, tok.chain, addr)
            if not any(h.get("address") == tok.address for h in c.winner_hits):
                c.winner_hits = [*c.winner_hits, {"symbol": tok.symbol, "address": tok.address,
                                                  "pnl_usd": round(pnl, 2), "gain_pct": round(gain, 1)}]
            origin = f"winner:{tok.symbol}"
            if origin not in c.origins:
                c.origins = [*c.origins, origin]
            c.best_pnl_usd = max(c.best_pnl_usd, pnl)
            kept += 1
        session.add(TraderTokenFetch(chain=tok.chain, token_address=tok.address, traders_found=kept))
        stats["winner_tokens"] += 1
        stats["winner_traders"] += kept


async def _verify(session: AsyncSession, client: BirdeyeClient, settings: Settings, stats: dict[str, int]) -> None:
    stale = datetime.now(timezone.utc) - timedelta(days=settings.birdeye_reverify_days)
    rows = (await session.execute(select(TraderCandidate))).scalars().all()
    due = [c for c in rows if c.verified_at is None or utc(c.verified_at) < stale]
    # Repeat winners first, then single-token winners, then chain-wide seeds; bigger PnL first.
    due.sort(key=lambda c: (-len(c.winner_hits), -c.best_pnl_usd))
    existing = {(w.chain, w.address): w for w in (await session.execute(select(SmartWallet))).scalars()}
    for c in due:
        summary = await client.pnl_summary(session, c.chain, c.address)
        ok, reason, wstats = evaluate_summary(summary, settings)
        c.summary, c.verified_at, c.qualified, c.reject_reason = wstats, datetime.now(timezone.utc), ok, (
            None if ok else reason)
        stats["verified"] += 1
        w = existing.get((c.chain, c.address))
        if ok:
            stats["qualified"] += 1
            if w is None:
                w = SmartWallet(chain=c.chain, address=c.address, source=WalletSource.birdeye, enabled=True)
                session.add(w)
                existing[(c.chain, c.address)] = w
                stats["added"] += 1
            if w.source == WalletSource.birdeye:
                w.label = wallet_label(wstats)
                w.enabled = not w.disabled_by_user
            w.stats = wstats
            w.win_tokens = [{"symbol": h["symbol"], "address": h["address"], "gain_pct": h.get("gain_pct", 0),
                             "pnl_usd": h["pnl_usd"]} for h in c.winner_hits][:10]
        elif w is not None and w.source == WalletSource.birdeye and w.enabled:
            w.enabled = False
            w.stats = wstats
            stats["disabled"] += 1
        await session.flush()


async def discover_traders(session: AsyncSession, settings: Settings | None = None,
                           client: BirdeyeClient | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    own = client is None
    client = client or BirdeyeClient(s)
    stats: dict[str, Any] = {"seeded": 0, "winner_tokens": 0, "winner_traders": 0, "verified": 0,
                             "qualified": 0, "added": 0, "disabled": 0}
    try:
        chains = [Chain(c) for c in s.chains if Chain(c) in X_CHAIN]
        # Winners and verification are worth more than seeds; seed only with budget to spare.
        for step in (_winners, _verify):
            try:
                await step(session, client, s, stats)
            except BudgetExhausted:
                stats["stopped"] = "daily budget reached"
                break
        if "stopped" not in stats:
            if await client.remaining(session) > len(chains) + 4:
                try:
                    await _seed(session, client, chains, stats)
                    await _verify(session, client, s, stats)  # verify fresh seeds with what is left
                except BudgetExhausted:
                    stats["stopped"] = "daily budget reached"
            else:
                stats["stopped"] = "daily budget reached (seeding skipped)"
    except BirdeyeError as e:
        stats["error"] = str(e)
        if e.status in (401, 403):
            log.error("Birdeye rejected the API key or plan (%s); check BIRDEYE_API_KEY", e)
        else:
            log.warning("Birdeye discovery stopped: %s", e)
    finally:
        stats["calls_24h"] = await client.calls_last_24h(session)
        if own:
            await client.close()
    return stats
