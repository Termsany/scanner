"""Async scanner & data-ingestion engine.

Cycle (every SCAN_INTERVAL_SECONDS, default 60s):
  1. Discover   DexScreener boosts/profiles + GeckoTerminal trending/new pools (4 chains)
  2. Enrich     DexScreener /tokens/v1 batch lookups -> primary pair metrics
  3. Filter     liquidity >= $10k AND 1h volume >= $5k (configurable)
  4. Audit      GoPlus (EVM + Solana) and RugCheck (Solana), cached 30 min
  5. Score      scoring_engine.compute_alpha_score
  6. Persist    tokens / security_audits / score_records / market_snapshots
  7. Signal     score >= 75 -> signals table + Redis pub/sub (WebSocket + Telegram in Phase 2)

Run:
  python -m app.scanner_worker            # loop forever
  python -m app.scanner_worker --once     # single cycle (cron / debugging)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import Cache, RateLimiter
from app.config import Settings, get_settings
from app.db import SessionLocal, engine, init_db
from app.models import Chain, MarketSnapshot, ScannerRun, ScoreRecord, SecurityAudit, Signal, Token
from app.schemas import (
    HolderEntry,
    LiveSignal,
    MarketMetrics,
    RiskItem,
    ScoreBreakdown,
    SecurityReport,
    SmartMoneyInfo,
)
from app.scoring_engine import compute_alpha_score, top_reasons
from app.holdings import HoldingsChecker
from app.smart_money import SmartWalletRegistry

log = logging.getLogger("scanner")

# --- Chain identifiers per provider ----------------------------------------
DEXSCREENER_CHAIN = {Chain.solana: "solana", Chain.ethereum: "ethereum", Chain.base: "base", Chain.bsc: "bsc"}
GECKO_NETWORK = {Chain.solana: "solana", Chain.ethereum: "eth", Chain.base: "base", Chain.bsc: "bsc"}
GOPLUS_CHAIN_ID = {Chain.ethereum: "1", Chain.bsc: "56", Chain.base: "8453"}
FROM_DEXSCREENER = {v: k for k, v in DEXSCREENER_CHAIN.items()}

EVM_BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}
# Native / wrapped / stable assets that show up as the base token of some pools.
SKIP_SYMBOLS = {"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "USDC", "USDT", "DAI", "USDE", "FDUSD", "CBBTC", "WBTC",
                "PYUSD", "USD1", "USDS", "USDB", "BUSD", "TUSD", "EURC"}
# Solana program-owned accounts that hold pooled supply, not investor supply.
SOLANA_POOL_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",   # Meteora / DAMM authority
}

DEXSCREENER_BATCH = 30


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


def _flag(value: Any) -> bool | None:
    """GoPlus encodes booleans as "0"/"1" strings; missing -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):  # Solana GoPlus: {"status": "1", ...}
        return _flag(value.get("status"))
    return str(value).strip() == "1"


def _norm_addr(chain: Chain, address: str) -> str:
    """EVM addresses are case-insensitive -> lowercase. Solana base58 is case-sensitive."""
    return address.lower() if chain != Chain.solana else address


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Http:
    """Shared httpx client with per-provider rate limits and retry/backoff on 429/5xx."""

    def __init__(self, settings: Settings) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": "alpha-scanner/0.1", "Accept": "application/json"},
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
            follow_redirects=True,
        )

    async def get_json(
        self,
        url: str,
        *,
        limiter: RateLimiter,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> Any | None:
        for attempt in range(retries + 1):
            await limiter.acquire()
            try:
                resp = await self.client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == retries:
                    log.warning("GET %s failed: %s", url, exc)
                    return None
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("GET %s returned non-JSON body", url)
                    return None
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    log.warning("GET %s gave up after HTTP %s", url, resp.status_code)
                    return None
                retry_after = _f(resp.headers.get("Retry-After"))
                await asyncio.sleep(min(retry_after, 30.0) if retry_after else self._backoff(attempt))
                continue
            log.warning("GET %s -> HTTP %s: %s", url, resp.status_code, resp.text[:200])
            return None
        return None

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2 ** attempt, 16) + random.uniform(0, 0.5)

    async def close(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Provider clients
# ---------------------------------------------------------------------------


class DexScreenerClient:
    """https://docs.dexscreener.com/api/reference"""

    BASE = "https://api.dexscreener.com"

    def __init__(self, http: Http) -> None:
        self.http = http
        self.slow = RateLimiter(55, 60.0)    # profiles / boosts: 60 req/min
        self.fast = RateLimiter(280, 60.0)   # pairs / tokens: 300 req/min

    async def _token_list(self, path: str) -> list[tuple[str, str]]:
        data = await self.http.get_json(f"{self.BASE}{path}", limiter=self.slow)
        if isinstance(data, dict):  # some endpoints wrap a single object
            data = [data]
        out: list[tuple[str, str]] = []
        for item in data or []:
            chain_id, addr = item.get("chainId"), item.get("tokenAddress")
            if chain_id and addr:
                out.append((chain_id, addr))
        return out

    async def latest_boosts(self) -> list[tuple[str, str]]:
        return await self._token_list("/token-boosts/latest/v1")

    async def top_boosts(self) -> list[tuple[str, str]]:
        return await self._token_list("/token-boosts/top/v1")

    async def latest_profiles(self) -> list[tuple[str, str]]:
        return await self._token_list("/token-profiles/latest/v1")

    async def pairs_for_tokens(self, chain: Chain, addresses: list[str]) -> list[dict[str, Any]]:
        """Up to 30 token addresses per call; returns every pair that involves them."""
        if not addresses:
            return []
        url = f"{self.BASE}/tokens/v1/{DEXSCREENER_CHAIN[chain]}/{','.join(addresses[:DEXSCREENER_BATCH])}"
        data = await self.http.get_json(url, limiter=self.fast)
        return data if isinstance(data, list) else []


class GeckoUnavailable(Exception):
    """GeckoTerminal did not answer (rate limit, network, unknown pool) after retries."""


class GeckoTerminalClient:
    """https://www.geckoterminal.com/dex-api  (public: 30 calls/min)"""

    BASE = "https://api.geckoterminal.com/api/v2"
    HEADERS = {"Accept": "application/json;version=20230302"}

    def __init__(self, http: Http) -> None:
        self.http = http
        self.limiter = RateLimiter(25, 60.0)

    async def ohlcv(self, chain: Chain, pool: str, *, aggregate_minutes: int = 15, limit: int = 120,
                    before_ts: int | None = None) -> list[list[float]]:
        """Minute candles [ts, open, high, low, close, volume], oldest first (one call, <= 1000 candles)."""
        params: dict[str, Any] = {"aggregate": aggregate_minutes, "limit": limit, "currency": "usd"}
        if before_ts:
            params["before_timestamp"] = before_ts
        data = await self.http.get_json(
            f"{self.BASE}/networks/{GECKO_NETWORK[chain]}/pools/{pool}/ohlcv/minute",
            limiter=self.limiter, headers=self.HEADERS, params=params,
        )
        if data is None:  # rate limited / network / 404 after retries: not the same as "no candles"
            raise GeckoUnavailable(f"ohlcv unavailable for {pool[:10]}")
        rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted((r for r in rows if isinstance(r, list) and len(r) >= 5), key=lambda r: r[0])

    async def _pools(self, chain: Chain, kind: str) -> list[str]:
        network = GECKO_NETWORK[chain]
        data = await self.http.get_json(
            f"{self.BASE}/networks/{network}/{kind}",
            limiter=self.limiter,
            headers=self.HEADERS,
            params={"page": 1},
        )
        out: list[str] = []
        for pool in (data or {}).get("data", []) or []:
            token_id = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            prefix = f"{network}_"
            if token_id.startswith(prefix):
                out.append(token_id[len(prefix):])
        return out

    async def trending_pools(self, chain: Chain) -> list[str]:
        return await self._pools(chain, "trending_pools")

    async def new_pools(self, chain: Chain) -> list[str]:
        return await self._pools(chain, "new_pools")


class GoPlusClient:
    """https://docs.gopluslabs.io/reference/api-overview"""

    BASE = "https://api.gopluslabs.io/api/v1"
    RATE_LIMITED = 4029
    COOLDOWN_SECONDS = 20.0

    def __init__(self, http: Http, access_token: str | None) -> None:
        self.http = http
        self.blocked_until = 0.0
        self.limiter = RateLimiter(28, 60.0, burst=4)  # free tier: ~30/min, punishes bursts
        self.headers = {"Authorization": access_token} if access_token else None

    async def token_security(self, chain: Chain, address: str) -> dict[str, Any] | None:
        if chain == Chain.solana:
            url = f"{self.BASE}/solana/token_security"
        else:
            url = f"{self.BASE}/token_security/{GOPLUS_CHAIN_ID[chain]}"
        data: Any = None
        for attempt in range(2):
            # GoPlus reports rate limits as HTTP 200 + code 4029, so the generic 429 retry never fires.
            wait = self.blocked_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            data = await self.http.get_json(url, limiter=self.limiter, headers=self.headers,
                                            params={"contract_addresses": address})
            if isinstance(data, dict) and data.get("code") == self.RATE_LIMITED and attempt == 0:
                self.blocked_until = max(self.blocked_until, time.monotonic() + self.COOLDOWN_SECONDS)
                continue
            break
        if not isinstance(data, dict):
            return None
        # code 1 = complete, 2/3 = partial data (still usable); anything else is an error / rate limit.
        result = data.get("result") or {}
        if data.get("code") not in (1, 2, 3) or not isinstance(result, dict) or not result:
            log.info("GoPlus %s %s -> code=%s msg=%s", chain.value, address, data.get("code"), data.get("message"))
            return None
        return result.get(address) or result.get(address.lower()) or next(iter(result.values()), None)


class RugCheckClient:
    """https://api.rugcheck.xyz/swagger/index.html (Solana only)"""

    BASE = "https://api.rugcheck.xyz/v1"

    def __init__(self, http: Http, api_key: str | None) -> None:
        self.http = http
        self.limiter = RateLimiter(20, 60.0)
        self.headers = {"X-API-KEY": api_key} if api_key else None

    async def report(self, mint: str) -> dict[str, Any] | None:
        data = await self.http.get_json(f"{self.BASE}/tokens/{mint}/report", limiter=self.limiter,
                                        headers=self.headers)
        return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def pair_to_metrics(chain: Chain, token_address: str, pair: dict[str, Any], sources: list[str]) -> MarketMetrics:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    txns = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    liq = pair.get("liquidity") or {}
    info = pair.get("info") or {}
    created_ms = _f(pair.get("pairCreatedAt"))

    def tx(window: str, side: str) -> int | None:
        return _i((txns.get(window) or {}).get(side))

    return MarketMetrics(
        chain=chain,
        address=token_address,
        symbol=str(base.get("symbol") or "")[:64],
        name=str(base.get("name") or "")[:128],
        image_url=info.get("imageUrl"),
        pair_address=pair.get("pairAddress"),
        dex_id=pair.get("dexId"),
        pair_url=pair.get("url"),
        quote_symbol=quote.get("symbol"),
        pair_created_at=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if created_ms else None,
        price_usd=_f(pair.get("priceUsd")),
        price_change_m5=_f(chg.get("m5")),
        price_change_h1=_f(chg.get("h1")),
        price_change_h6=_f(chg.get("h6")),
        price_change_h24=_f(chg.get("h24")),
        liquidity_usd=_f(liq.get("usd")),
        volume_m5=_f(vol.get("m5")),
        volume_h1=_f(vol.get("h1")),
        volume_h6=_f(vol.get("h6")),
        volume_h24=_f(vol.get("h24")),
        buys_m5=tx("m5", "buys"),
        sells_m5=tx("m5", "sells"),
        buys_h1=tx("h1", "buys"),
        sells_h1=tx("h1", "sells"),
        buys_h24=tx("h24", "buys"),
        sells_h24=tx("h24", "sells"),
        fdv=_f(pair.get("fdv")),
        market_cap=_f(pair.get("marketCap")),
        boosts_active=_i((pair.get("boosts") or {}).get("active")) or 0,
        discovery_sources=sorted(set(sources)),
    )


def select_primary_pair(chain: Chain, token_address: str, pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Most liquid pair where the token is the *base* asset."""
    target = _norm_addr(chain, token_address)
    candidates = [
        p for p in pairs
        if _norm_addr(chain, str((p.get("baseToken") or {}).get("address", ""))) == target
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0.0)


def _pct_from_fraction(value: Any) -> float | None:
    """GoPlus returns ratios as strings in [0, 1]; convert to percent, clamped."""
    f = _f(value)
    if f is None:
        return None
    return min(max(f * 100.0, 0.0), 100.0)


def normalize_goplus_evm(chain: Chain, address: str, pair_address: str | None, d: dict[str, Any]) -> SecurityReport:
    pair_lc = (pair_address or "").lower()

    # LP lock: share of LP tokens that are locked or sitting in burn addresses.
    lp_locked: float | None = None
    lp_holders = d.get("lp_holders")
    if isinstance(lp_holders, list) and lp_holders:
        total = 0.0
        for h in lp_holders:
            addr = str(h.get("address", "")).lower()
            if _flag(h.get("is_locked")) or addr in EVM_BURN_ADDRESSES:
                total += _pct_from_fraction(h.get("percent")) or 0.0
        lp_locked = min(total, 100.0)

    holders: list[HolderEntry] = []
    for h in d.get("holders") or []:
        addr = str(h.get("address", "")).lower()
        pct = _pct_from_fraction(h.get("percent"))
        if pct is None:
            continue
        holders.append(HolderEntry(address=addr, pct=pct, tag=h.get("tag") or None,
                                   is_contract=_flag(h.get("is_contract")), is_locked=_flag(h.get("is_locked"))))
    investor_holders = [
        h for h in holders
        if not h.is_locked and h.address not in EVM_BURN_ADDRESSES and h.address != pair_lc
    ]
    top10 = sum(h.pct for h in sorted(investor_holders, key=lambda h: h.pct, reverse=True)[:10]) if holders else None

    risks: list[RiskItem] = []
    checks = [
        ("is_honeypot", "Honeypot", "danger", "Token cannot be sold"),
        ("cannot_sell_all", "Cannot sell all", "danger", "Holders cannot sell their full balance"),
        ("is_mintable", "Mintable", "warn", "Owner can mint new supply"),
        ("owner_change_balance", "Owner can change balance", "danger", "Owner can modify holder balances"),
        ("hidden_owner", "Hidden owner", "danger", "Contract has a hidden owner"),
        ("is_blacklisted", "Blacklist", "warn", "Owner can blacklist addresses"),
        ("transfer_pausable", "Pausable", "warn", "Transfers can be paused"),
        ("slippage_modifiable", "Modifiable tax", "warn", "Owner can change buy/sell tax"),
        ("is_proxy", "Proxy contract", "warn", "Logic can be upgraded"),
        ("trading_cooldown", "Trading cooldown", "info", "Trades limited by cooldown"),
    ]
    for key, name, level, desc in checks:
        if _flag(d.get(key)):
            risks.append(RiskItem(name=name, level=level, description=desc, source="goplus"))
    if _flag(d.get("is_open_source")) is False:
        risks.append(RiskItem(name="Closed source", level="warn", description="Contract not verified", source="goplus"))

    return SecurityReport(
        chain=chain,
        address=address,
        providers=["goplus"],
        is_honeypot=_flag(d.get("is_honeypot")),
        is_mintable=_flag(d.get("is_mintable")),
        # EVM has no SPL-style freeze authority; blacklist / pause are the functional equivalent.
        is_freezable=(_flag(d.get("is_blacklisted")) or _flag(d.get("transfer_pausable")))
        if (d.get("is_blacklisted") is not None or d.get("transfer_pausable") is not None) else None,
        is_open_source=_flag(d.get("is_open_source")),
        is_proxy=_flag(d.get("is_proxy")),
        owner_can_change_balance=_flag(d.get("owner_change_balance")),
        hidden_owner=_flag(d.get("hidden_owner")),
        cannot_sell_all=_flag(d.get("cannot_sell_all")),
        transfer_pausable=_flag(d.get("transfer_pausable")),
        buy_tax=_pct_from_fraction(d.get("buy_tax")),
        sell_tax=_pct_from_fraction(d.get("sell_tax")),
        lp_locked_pct=lp_locked,
        top10_holders_pct=min(top10, 100.0) if top10 is not None else None,
        holder_count=_i(d.get("holder_count")),
        creator_pct=_pct_from_fraction(d.get("creator_percent")),
        risks=risks,
        top_holders=sorted(holders, key=lambda h: h.pct, reverse=True)[:20],
        raw={"goplus": d},
    )


def normalize_solana(
    address: str,
    pair_address: str | None,
    goplus: dict[str, Any] | None,
    rug: dict[str, Any] | None,
) -> SecurityReport:
    providers: list[str] = []
    risks: list[RiskItem] = []
    raw: dict[str, Any] = {}

    is_mintable: bool | None = None
    is_freezable: bool | None = None
    non_transferable: bool | None = None
    lp_locked: float | None = None
    top10: float | None = None
    holders: list[HolderEntry] = []
    holder_count: int | None = None
    rug_score: float | None = None
    owner_can_change_balance: bool | None = None

    if goplus:
        providers.append("goplus")
        raw["goplus"] = goplus
        is_mintable = _flag(goplus.get("mintable"))
        is_freezable = _flag(goplus.get("freezable"))
        non_transferable = _flag(goplus.get("non_transferable"))
        owner_can_change_balance = _flag(goplus.get("balance_mutable_authority"))
        holder_count = _i(goplus.get("holder_count"))
        if _flag(goplus.get("closable")):
            risks.append(RiskItem(name="Closable", level="danger", description="Mint account can be closed",
                                  source="goplus"))
        if goplus.get("transfer_hook"):
            risks.append(RiskItem(name="Transfer hook", level="warn",
                                  description="Token-2022 transfer hook can block or tax transfers", source="goplus"))

    if rug:
        providers.append("rugcheck")
        raw["holders_are_owners"] = True  # marker: holder addresses are wallets, not token accounts
        raw["rugcheck"] = {k: rug.get(k) for k in
                           ("mintAuthority", "freezeAuthority", "score", "score_normalised", "rugged", "risks",
                            "totalHolders", "totalLPProviders", "totalMarketLiquidity")}
        # RugCheck reads authorities directly from the mint account: prefer it over GoPlus.
        if "mintAuthority" in rug:
            is_mintable = rug.get("mintAuthority") is not None
        if "freezeAuthority" in rug:
            is_freezable = rug.get("freezeAuthority") is not None
        rug_score = _f(rug.get("score_normalised") if rug.get("score_normalised") is not None else rug.get("score"))
        holder_count = _i(rug.get("totalHolders")) or holder_count

        markets = rug.get("markets") or []
        pool_accounts: set[str] = set(SOLANA_POOL_AUTHORITIES)
        for mk in markets:
            for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
                if mk.get(k):
                    pool_accounts.add(str(mk[k]))
        for acct, meta in (rug.get("knownAccounts") or {}).items():
            if isinstance(meta, dict) and str(meta.get("type", "")).upper() in {"AMM", "LOCKER", "BURN"}:
                pool_accounts.add(acct)

        primary = next((mk for mk in markets if pair_address and mk.get("pubkey") == pair_address), None)
        lp_values = [
            _f((mk.get("lp") or {}).get("lpLockedPct"))
            for mk in ([primary] if primary else markets)
        ]
        lp_values = [v for v in lp_values if v is not None]
        if lp_values:
            lp_locked = min(max(max(lp_values), 0.0), 100.0)

        for h in rug.get("topHolders") or []:
            pct = _f(h.get("pct"))
            account = str(h.get("address", ""))
            owner = str(h.get("owner") or "")
            if pct is None:
                continue
            is_pool = account in pool_accounts or owner in pool_accounts
            # `address` is the SPL token account; the wallet that controls it is `owner`.
            # Store the owner so holder lists can be matched against wallet addresses (smart money).
            holders.append(HolderEntry(address=owner or account, pct=min(max(pct, 0.0), 100.0),
                                       tag="pool" if is_pool else ("insider" if h.get("insider") else None),
                                       is_locked=is_pool))
        investor = [h for h in holders if not h.is_locked]
        if holders:
            top10 = min(sum(h.pct for h in sorted(investor, key=lambda h: h.pct, reverse=True)[:10]), 100.0)

        for r in rug.get("risks") or []:
            lvl = str(r.get("level", "info")).lower()
            risks.append(RiskItem(name=str(r.get("name", "")), level="danger" if lvl == "danger" else
                                  ("warn" if lvl in {"warn", "warning"} else "info"),
                                  description=str(r.get("description", "")), source="rugcheck"))
        if rug.get("rugged") is True:
            risks.append(RiskItem(name="Rugged", level="danger", description="RugCheck marks this token as rugged",
                                  source="rugcheck"))

    if not holders and goplus:
        for h in goplus.get("holders") or []:
            pct = _pct_from_fraction(h.get("percent"))
            addr = str(h.get("account") or h.get("address") or "")
            if pct is None or not addr:
                continue
            locked = _flag(h.get("is_locked")) or addr in SOLANA_POOL_AUTHORITIES
            holders.append(HolderEntry(address=addr, pct=pct, tag=h.get("tag") or None, is_locked=locked))
        if holders:
            investor = [h for h in holders if not h.is_locked]
            top10 = min(sum(h.pct for h in sorted(investor, key=lambda h: h.pct, reverse=True)[:10]), 100.0)

    # SPL honeypot vectors: non-transferable mint, or freeze authority (can freeze buyers' accounts).
    # Freeze is scored separately; here we only assert "not a honeypot" when transfers are provably open.
    if non_transferable is True:
        is_honeypot: bool | None = True
    elif is_freezable is False and non_transferable is not True and providers:
        is_honeypot = False
    else:
        is_honeypot = None

    return SecurityReport(
        chain=Chain.solana,
        address=address,
        providers=providers,
        is_honeypot=is_honeypot,
        is_mintable=is_mintable,
        is_freezable=is_freezable,
        owner_can_change_balance=owner_can_change_balance,
        lp_locked_pct=lp_locked,
        top10_holders_pct=top10,
        holder_count=holder_count,
        rugcheck_score=rug_score,
        risks=risks,
        top_holders=sorted(holders, key=lambda h: h.pct, reverse=True)[:20],
        raw=raw,
        # SPL tokens have no buy/sell tax unless Token-2022 transfer fee is set
        buy_tax=_pct_from_fraction(((goplus or {}).get("transfer_fee") or {}).get("fee_rate"))
        if isinstance((goplus or {}).get("transfer_fee"), dict) else None,
        sell_tax=_pct_from_fraction(((goplus or {}).get("transfer_fee") or {}).get("fee_rate"))
        if isinstance((goplus or {}).get("transfer_fee"), dict) else None,
    )


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


@dataclass
class CycleStats:
    candidates: int = 0
    passed_filters: int = 0
    scored: int = 0
    signals: int = 0
    errors: list[str] = field(default_factory=list)


class Scanner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.chains = [Chain(c) for c in self.settings.chains]
        self.http = Http(self.settings)
        self.cache = Cache(self.settings.redis_url)
        self.dex = DexScreenerClient(self.http)
        self.gecko = GeckoTerminalClient(self.http)
        self.goplus = GoPlusClient(self.http, self.settings.goplus_access_token)
        self.rugcheck = RugCheckClient(self.http, self.settings.rugcheck_api_key)
        self.smart_money = SmartWalletRegistry(self.settings)
        self.holdings = HoldingsChecker(self.settings)
        self._security_sem = asyncio.Semaphore(6)
        self._cycle_no = 0

    # ---- 1. discovery ---------------------------------------------------
    async def discover(self, stats: CycleStats) -> dict[tuple[Chain, str], set[str]]:
        found: dict[tuple[Chain, str], set[str]] = defaultdict(set)

        dex_jobs = {
            "dex_boost_latest": self.dex.latest_boosts(),
            "dex_boost_top": self.dex.top_boosts(),
            "dex_profile_latest": self.dex.latest_profiles(),
        }
        # GeckoTerminal's free API (~30 calls/min per IP) is shared with performance measurement and the
        # inspector chart; its trending/new lists change slowly, so poll them every Nth cycle only.
        self._cycle_no += 1
        gecko_jobs = {}
        if (self._cycle_no - 1) % max(1, self.settings.gecko_discovery_every_cycles) == 0:
            for chain in self.chains:
                gecko_jobs[(chain, "gecko_trending")] = self.gecko.trending_pools(chain)
                gecko_jobs[(chain, "gecko_new")] = self.gecko.new_pools(chain)

        dex_results = await asyncio.gather(*dex_jobs.values(), return_exceptions=True)
        for source, res in zip(dex_jobs.keys(), dex_results):
            if isinstance(res, BaseException):
                stats.errors.append(f"{source}: {res!r}")
                continue
            for chain_id, addr in res:
                chain = FROM_DEXSCREENER.get(chain_id)
                if chain in self.chains:
                    found[(chain, _norm_addr(chain, addr))].add(source)

        gecko_results = await asyncio.gather(*gecko_jobs.values(), return_exceptions=True)
        for (chain, source), res in zip(gecko_jobs.keys(), gecko_results):
            if isinstance(res, BaseException):
                stats.errors.append(f"{source}/{chain.value}: {res!r}")
                continue
            for addr in res:
                found[(chain, _norm_addr(chain, addr))].add(source)

        # Tokens tracked wallets bought recently get scored too, even if no DEX feed lists them.
        try:
            from app.models import WalletTrade

            async with SessionLocal() as session:
                since = datetime.now(timezone.utc) - timedelta(hours=2)
                for chain, addr in (await session.execute(
                    select(WalletTrade.chain, WalletTrade.token_address).where(
                        WalletTrade.detected_at >= since,
                        (WalletTrade.side == "buy") | WalletTrade.side.is_(None))  # buys only
                )).all():
                    if chain in self.chains:
                        found[(chain, _norm_addr(chain, addr))].add("tracked_wallet_buy")
        except Exception:
            log.warning("could not load tracked-wallet buys", exc_info=True)

        stats.candidates = len(found)
        return found

    # ---- 2. market enrichment -------------------------------------------
    async def enrich(self, candidates: dict[tuple[Chain, str], set[str]]) -> list[MarketMetrics]:
        by_chain: dict[Chain, list[str]] = defaultdict(list)
        for chain, addr in candidates:
            by_chain[chain].append(addr)

        async def batch(chain: Chain, addrs: list[str]) -> list[MarketMetrics]:
            pairs = await self.dex.pairs_for_tokens(chain, addrs)
            out: list[MarketMetrics] = []
            for addr in addrs:
                pair = select_primary_pair(chain, addr, pairs)
                if pair is None:
                    continue
                # Preserve the canonical address casing returned by DexScreener.
                canonical = str((pair.get("baseToken") or {}).get("address") or addr)
                out.append(pair_to_metrics(chain, _norm_addr(chain, canonical), pair,
                                           sorted(candidates[(chain, addr)])))
            return out

        jobs = [
            batch(chain, addrs[i:i + DEXSCREENER_BATCH])
            for chain, addrs in by_chain.items()
            for i in range(0, len(addrs), DEXSCREENER_BATCH)
        ]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        metrics: list[MarketMetrics] = []
        for res in results:
            if isinstance(res, BaseException):
                log.warning("DexScreener batch failed: %r", res)
                continue
            metrics.extend(res)
        return metrics

    # ---- 3. filter ------------------------------------------------------
    def passes_filters(self, m: MarketMetrics) -> bool:
        if m.symbol.upper() in SKIP_SYMBOLS:
            return False
        if (m.liquidity_usd or 0.0) < self.settings.min_liquidity_usd:
            return False
        if (m.volume_h1 or 0.0) < self.settings.min_volume_1h_usd:
            return False
        return m.price_usd is not None and m.price_usd > 0

    # ---- 4. security ----------------------------------------------------
    async def security_for(self, m: MarketMetrics) -> SecurityReport | None:
        key = f"sec:v2:{m.chain.value}:{m.address}"  # v2: Solana holders are owner wallets
        cached = await self.cache.get_json(key)
        if cached:
            try:
                return SecurityReport.model_validate(cached)
            except ValueError:
                pass

        async with self._security_sem:
            if m.chain == Chain.solana:
                # RugCheck reads mint/freeze/LP/holders on-chain; GoPlus only as fallback to save its quota for EVM.
                rug = await self.rugcheck.report(m.address)
                gp = None if rug else await self.goplus.token_security(m.chain, m.address)
                report = normalize_solana(m.address, m.pair_address, gp, rug)
            else:
                gp = await self.goplus.token_security(m.chain, m.address)
                report = normalize_goplus_evm(m.chain, m.address, m.pair_address, gp) if gp else \
                    SecurityReport(chain=m.chain, address=m.address)

        if report.has_data:
            payload = report.model_dump(mode="json")
            await self.cache.set_json(key, payload, self.settings.security_cache_ttl_seconds)
        return report

    async def _audit_with_deadline(
        self, survivors: list[MarketMetrics], stats: CycleStats
    ) -> list[SecurityReport | BaseException | None]:
        """Run security lookups but keep the cycle inside its interval. Tokens whose audit misses
        the deadline are scored as unaudited (0 security pts) now and picked up next cycle."""
        tasks = [asyncio.create_task(self.security_for(m)) for m in survivors]
        if not tasks:
            return []
        deadline = self.settings.scan_interval_seconds * 0.7
        _, pending = await asyncio.wait(tasks, timeout=deadline)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            log.info("%d security audits deferred to next cycle (deadline %.0fs)", len(pending), deadline)
            stats.errors.append(f"{len(pending)} audits deferred")
        out: list[SecurityReport | BaseException | None] = []
        for t in tasks:
            if t.cancelled():
                out.append(None)
            elif t.exception() is not None:
                out.append(t.exception())
            else:
                out.append(t.result())
        return out

    # ---- 6. persistence -------------------------------------------------
    async def persist(
        self,
        session: AsyncSession,
        m: MarketMetrics,
        sec: SecurityReport | None,
        sm: SmartMoneyInfo,
        score: ScoreBreakdown,
    ) -> Token:
        token = (await session.execute(
            select(Token).where(Token.chain == m.chain, Token.address == m.address)
        )).scalar_one_or_none()
        if token is None:
            token = Token(chain=m.chain, address=m.address, discovery_sources=[])
            session.add(token)

        for f in ("symbol", "name", "image_url", "pair_address", "dex_id", "pair_url", "quote_symbol",
                  "pair_created_at", "price_usd", "price_change_m5", "price_change_h1", "price_change_h6",
                  "price_change_h24", "liquidity_usd", "volume_m5", "volume_h1", "volume_h6", "volume_h24",
                  "buys_m5", "sells_m5", "buys_h1", "sells_h1", "buys_h24", "sells_h24", "fdv", "market_cap",
                  "boosts_active"):
            setattr(token, f, getattr(m, f))
        token.discovery_sources = sorted(set(token.discovery_sources or []) | set(m.discovery_sources))
        token.alpha_score = score.total
        token.badge = score.badge
        token.smart_money_hits = len(sm.tracked_wallet_hits)
        token.last_scanned_at = datetime.now(timezone.utc)
        if sec is not None:
            for f in ("is_honeypot", "is_mintable", "is_freezable", "lp_locked_pct", "top10_holders_pct",
                      "buy_tax", "sell_tax"):
                setattr(token, f, getattr(sec, f))
        await session.flush()  # assign token.id

        if sec is not None and sec.has_data:
            audit = (await session.execute(
                select(SecurityAudit).where(SecurityAudit.token_id == token.id)
            )).scalar_one_or_none()
            if audit is None:
                audit = SecurityAudit(token_id=token.id)
                session.add(audit)
            dump = sec.model_dump(mode="json")
            for f in ("providers", "is_honeypot", "is_mintable", "is_freezable", "is_open_source", "is_proxy",
                      "owner_can_change_balance", "hidden_owner", "cannot_sell_all", "transfer_pausable",
                      "buy_tax", "sell_tax", "lp_locked_pct", "top10_holders_pct", "holder_count", "creator_pct",
                      "rugcheck_score", "risks", "top_holders"):
                setattr(audit, f, dump[f])
            if sec.raw:  # cached reports drop `raw` (excluded from dumps); keep the last provider payload
                audit.raw = json.loads(json.dumps(sec.raw, default=str))
            audit.fetched_at = sec.fetched_at

        session.add(ScoreRecord(
            token_id=token.id, total=score.total, security=score.security.points,
            momentum=score.momentum.points, smart_money=score.smart_money.points, badge=score.badge,
            breakdown=score.model_dump(mode="json"),
        ))
        session.add(MarketSnapshot(
            token_id=token.id, price_usd=m.price_usd, liquidity_usd=m.liquidity_usd, volume_h1=m.volume_h1,
            market_cap=m.effective_mcap, buys_h1=m.buys_h1, sells_h1=m.sells_h1,
        ))
        return token

    # ---- 7. signals -----------------------------------------------------
    async def maybe_signal(
        self, session: AsyncSession, token: Token, m: MarketMetrics, score: ScoreBreakdown,
        sec: SecurityReport | None = None,
    ) -> dict[str, Any] | None:
        """Create a Signal row (flushed, not committed). Returns the payload to publish after commit."""
        if score.total < self.settings.live_signal_min_score or score.hard_fail:
            return None
        cooldown = self.settings.signal_cooldown_seconds
        last = (await session.execute(
            select(Signal.created_at).where(Signal.token_id == token.id)
            .order_by(Signal.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if last and _utc(last) > datetime.now(timezone.utc) - timedelta(seconds=cooldown):
            return None
        if not await self.cache.set_if_absent(f"sig:{m.chain.value}:{m.address}", cooldown):
            return None

        live = LiveSignal(
            chain=m.chain, address=m.address, symbol=m.symbol, name=m.name, score=score.total, badge=score.badge,
            price_usd=m.price_usd, liquidity_usd=m.liquidity_usd, volume_h1=m.volume_h1,
            price_change_h1=m.price_change_h1, market_cap=m.effective_mcap, pair_url=m.pair_url,
            top_reasons=top_reasons(score),
        )
        holders = None
        # sec:v2 reports carry wallet (owner) addresses; on Solana only RugCheck exposes the owner.
        if sec is not None and sec.top_holders and (sec.chain != Chain.solana or "rugcheck" in sec.providers):
            holders = [h.model_dump(mode="json") for h in sec.top_holders]
        sig = Signal(token_id=token.id, score=score.total, badge=score.badge, price_usd=m.price_usd,
                     payload=live.model_dump(mode="json"), holders=holders)
        session.add(sig)
        await session.flush()
        live.signal_id = sig.id
        sig.payload = live.model_dump(mode="json")
        log.info("SIGNAL %s %s (%s) score=%d", m.chain.value, m.symbol, m.address, score.total)
        return sig.payload

    # ---- cycle ----------------------------------------------------------
    async def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()

        candidates = await self.discover(stats)
        metrics = await self.enrich(candidates)
        survivors = [m for m in metrics if self.passes_filters(m)]
        survivors.sort(key=lambda m: m.volume_h1 or 0.0, reverse=True)
        survivors = survivors[: self.settings.max_tokens_per_cycle]
        stats.passed_filters = len(survivors)

        sec_results = await self._audit_with_deadline(survivors, stats)

        try:
            async with SessionLocal() as session:
                await self.smart_money.prepare_cycle(session)
        except Exception as exc:  # stale wallet list is better than a failed cycle
            log.exception("Smart wallet refresh failed")
            stats.errors.append(f"smart_wallets: {exc!r}")

        held: dict = {}
        if self.smart_money.size:
            try:
                held = await self.holdings.check(survivors, self.smart_money.wallets)
            except Exception as exc:  # never let RPC trouble break scoring
                log.warning("holdings check failed: %r", exc)

        async with SessionLocal() as session:
            for m, sec in zip(survivors, sec_results):
                if isinstance(sec, BaseException):
                    stats.errors.append(f"security {m.chain.value}:{m.address}: {sec!r}")
                    sec = None
                try:
                    sm = self.smart_money.evaluate(m.chain, sec, held.get((m.chain, m.address)))
                    score = compute_alpha_score(m, sec, sm)
                    token = await self.persist(session, m, sec, sm, score)
                    payload = await self.maybe_signal(session, token, m, score, sec)
                    if payload is not None:
                        # Commit first so the token detail exists when clients react, then publish.
                        await session.commit()
                        await self.cache.publish(self.settings.redis_signal_channel, payload)
                        stats.signals += 1
                    stats.scored += 1
                except Exception as exc:  # one bad token must not kill the cycle
                    log.exception("Failed to process %s:%s", m.chain.value, m.address)
                    stats.errors.append(f"process {m.chain.value}:{m.address}: {exc!r}")
            session.add(ScannerRun(
                started_at=started, finished_at=datetime.now(timezone.utc), candidates=stats.candidates,
                passed_filters=stats.passed_filters, scored=stats.scored, signals_emitted=stats.signals,
                errors=stats.errors[:50], ok=stats.scored > 0 or stats.candidates == 0,
            ))
            await session.commit()

        log.info("cycle done in %.1fs: candidates=%d passed=%d scored=%d signals=%d errors=%d",
                 time.perf_counter() - t0, stats.candidates, stats.passed_filters, stats.scored,
                 stats.signals, len(stats.errors))
        return stats

    async def run_forever(self) -> None:
        interval = self.settings.scan_interval_seconds
        log.info("Scanner started: chains=%s interval=%ss cache=%s",
                 [c.value for c in self.chains], interval, self.cache.backend)
        while True:
            t0 = time.monotonic()
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scan cycle crashed; retrying next interval")
            await asyncio.sleep(max(interval - (time.monotonic() - t0), 1.0))

    async def close(self) -> None:
        await self.holdings.close()
        await self.http.close()
        await self.cache.close()


async def _main(once: bool) -> None:
    await init_db()
    scanner = Scanner()
    from app.wallet_tracker import WalletTracker

    tracker = WalletTracker(scanner.settings)
    try:
        if once:
            await scanner.run_cycle()
        else:
            from app.performance import PerformanceTracker

            await asyncio.gather(scanner.run_forever(), tracker.run_forever(),
                                 PerformanceTracker(scanner.gecko, scanner.settings).run_forever())
    finally:
        await tracker.close()
        await scanner.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Alpha scanner worker")
    parser.add_argument("--once", action="store_true", help="run a single scan cycle and exit")
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_main(args.once))
    except KeyboardInterrupt:
        log.info("Scanner stopped")


if __name__ == "__main__":
    main()
