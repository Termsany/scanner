"""Pydantic v2 schemas.

Three layers:
1. Domain inputs produced by the scanner (MarketMetrics, SecurityReport, SmartMoneyInfo).
2. Scoring output (ScoreComponent, CategoryScore, ScoreBreakdown).
3. API / transport contracts used by FastAPI, WebSockets and the frontend (Phase 2/3).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models import Badge, Chain

Pct = Annotated[float, Field(ge=0.0, le=100.0)]


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, use_enum_values=False)


# ---------------------------------------------------------------------------
# 1. Domain inputs
# ---------------------------------------------------------------------------


class MarketMetrics(_Model):
    """Normalised view of a token's primary pair (from DexScreener)."""

    chain: Chain
    address: str
    symbol: str = ""
    name: str = ""
    image_url: str | None = None
    pair_address: str | None = None
    dex_id: str | None = None
    pair_url: str | None = None
    quote_symbol: str | None = None
    pair_created_at: datetime | None = None

    price_usd: float | None = None
    price_change_m5: float | None = None
    price_change_h1: float | None = None
    price_change_h6: float | None = None
    price_change_h24: float | None = None

    liquidity_usd: float | None = None
    volume_m5: float | None = None
    volume_h1: float | None = None
    volume_h6: float | None = None
    volume_h24: float | None = None

    buys_m5: int | None = None
    sells_m5: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    buys_h24: int | None = None
    sells_h24: int | None = None

    fdv: float | None = None
    market_cap: float | None = None
    boosts_active: int = 0
    discovery_sources: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def age_minutes(self) -> float | None:
        if self.pair_created_at is None:
            return None
        delta = datetime.now(timezone.utc) - self.pair_created_at
        return max(delta.total_seconds() / 60.0, 0.0)

    @property
    def effective_mcap(self) -> float | None:
        """Market cap, falling back to FDV (DexScreener omits mcap for many new tokens)."""
        return self.market_cap if self.market_cap and self.market_cap > 0 else self.fdv


class HolderEntry(_Model):
    address: str
    pct: Pct
    tag: str | None = None
    is_contract: bool | None = None
    is_locked: bool | None = None


class RiskItem(_Model):
    name: str
    level: str = "info"  # info | warn | danger
    description: str = ""
    source: str = ""


class SecurityReport(_Model):
    """Normalised, provider-agnostic security report. `None` means *unknown*."""

    chain: Chain
    address: str
    providers: list[str] = Field(default_factory=list)

    is_honeypot: bool | None = None
    is_mintable: bool | None = None
    is_freezable: bool | None = None
    is_open_source: bool | None = None
    is_proxy: bool | None = None
    owner_can_change_balance: bool | None = None
    hidden_owner: bool | None = None
    cannot_sell_all: bool | None = None
    transfer_pausable: bool | None = None

    buy_tax: Pct | None = None
    sell_tax: Pct | None = None
    lp_locked_pct: Pct | None = None
    top10_holders_pct: Pct | None = None
    holder_count: int | None = None
    creator_pct: Pct | None = None
    rugcheck_score: float | None = None

    risks: list[RiskItem] = Field(default_factory=list)
    top_holders: list[HolderEntry] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def has_data(self) -> bool:
        return bool(self.providers)


class SmartMoneyInfo(_Model):
    tracked_wallet_hits: list[str] = Field(default_factory=list)  # labels of tracked wallets holding the token
    inferred: bool = False            # True when points come from the heuristic, not a wallet match
    avg_trade_usd_h1: float | None = None
    note: str = ""


# ---------------------------------------------------------------------------
# 2. Scoring output
# ---------------------------------------------------------------------------


class ScoreComponent(_Model):
    key: str
    label: str
    points: float
    max_points: float
    passed: bool
    value: float | str | bool | None = None
    detail: str = ""


class CategoryScore(_Model):
    name: str
    points: float
    max_points: float
    components: list[ScoreComponent]


class ScoreBreakdown(_Model):
    version: str
    total: int = Field(ge=0, le=100)
    badge: Badge
    security: CategoryScore
    momentum: CategoryScore
    smart_money: CategoryScore
    hard_fail: str | None = None          # e.g. "HONEYPOT" -> total forced to 0
    data_gaps: list[str] = Field(default_factory=list)
    scored_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def badge_color(self) -> str:
        return {Badge.strong_alpha: "green", Badge.moderate: "amber", Badge.high_risk: "red"}[self.badge]


# ---------------------------------------------------------------------------
# 3. API / transport contracts
# ---------------------------------------------------------------------------


class SortField(str, enum.Enum):
    score = "score"
    volume = "volume"
    liquidity = "liquidity"
    age = "age"
    change_1h = "change_1h"
    volume_1h = "volume_1h"


class SortOrder(str, enum.Enum):
    asc = "asc"
    desc = "desc"


class TokenListParams(_Model):
    chain: Chain | None = None
    min_score: int = Field(0, ge=0, le=100)
    min_liquidity: float = Field(0.0, ge=0.0)
    badge: Badge | None = None
    q: str | None = Field(None, max_length=64, description="symbol / name / address search")
    sort: SortField = SortField.score
    order: SortOrder = SortOrder.desc
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)


class SecurityBadges(_Model):
    lp_locked: bool | None
    no_mint: bool | None
    no_freeze: bool | None
    honeypot: bool | None
    buy_tax: float | None
    sell_tax: float | None


class TokenSummary(_Model):
    """One row of the main scanner table."""

    id: int
    chain: Chain
    address: str
    symbol: str
    name: str
    image_url: str | None
    pair_url: str | None
    dex_id: str | None
    pair_created_at: datetime | None
    price_usd: float | None
    price_change_m5: float | None
    price_change_h1: float | None
    price_change_h24: float | None
    liquidity_usd: float | None
    volume_h1: float | None
    volume_h24: float | None
    market_cap: float | None
    fdv: float | None
    alpha_score: int
    badge: Badge
    smart_money_hits: int
    badges: SecurityBadges
    last_scanned_at: datetime


class PaginatedTokens(_Model):
    items: list[TokenSummary]
    page: int
    page_size: int
    total: int


class ScorePoint(_Model):
    total: int
    scored_at: datetime


class TokenDetail(_Model):
    """Payload for the slide-over inspector."""

    token: TokenSummary
    metrics: MarketMetrics
    security: SecurityReport | None
    score: ScoreBreakdown | None
    score_history: list[ScorePoint] = Field(default_factory=list)


class AIAnalyzeRequest(_Model):
    chain: Chain
    address: str
    metrics: MarketMetrics | None = None     # if omitted, server loads latest from DB
    security: SecurityReport | None = None
    score: ScoreBreakdown | None = None


class AIAnalysis(_Model):
    verdict: str = Field(description="BULLISH | NEUTRAL | AVOID")
    bullets: list[str] = Field(min_length=1, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)
    model: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    disclaimer: str = "Automated analysis, not financial advice."


class LiveSignal(_Model):
    """Message pushed on /ws/live-signals and to Telegram."""

    type: str = "signal"
    signal_id: int | None = None
    chain: Chain
    address: str
    symbol: str
    name: str
    score: int
    badge: Badge
    price_usd: float | None
    liquidity_usd: float | None
    volume_h1: float | None
    price_change_h1: float | None
    market_cap: float | None
    pair_url: str | None
    top_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TopGainer(_Model):
    chain: Chain
    address: str
    symbol: str
    signal_price_usd: float
    current_price_usd: float
    gain_pct: float
    signalled_at: datetime


class ScannerStatus(_Model):
    live: bool
    last_run_at: datetime | None
    last_run_ok: bool | None
    last_run_scored: int | None
    last_run_candidates: int | None = None
    last_run_errors: int | None = None
    last_cycle_seconds: float | None = None
    security_coverage_pct: float | None = None   # active tokens (1h) that have a security audit
    scan_interval_seconds: int
    active_tokens: int
    high_alpha_24h: int
    signals_24h: int
    top_gainer: TopGainer | None


class SignalOut(_Model):
    id: int
    chain: Chain
    address: str
    symbol: str
    score: int
    badge: Badge
    price_usd: float | None
    current_price_usd: float | None
    gain_pct: float | None
    created_at: datetime
    payload: dict[str, Any]


class SmartWalletOut(_Model):
    chain: Chain
    address: str
    label: str
    source: str            # manual | auto | birdeye
    enabled: bool
    wins: int
    appearances: int
    win_tokens: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] | None = None
    updated_at: datetime


class SmartWalletList(_Model):
    items: list[SmartWalletOut]
    enabled_manual: int
    enabled_auto: int
    enabled_birdeye: int = 0
    disabled: int


class Candle(_Model):
    time: int          # unix seconds (TradingView Lightweight Charts format)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class ChartData(_Model):
    source: str        # "geckoterminal" | "snapshots"
    timeframe: str
    candles: list[Candle]
