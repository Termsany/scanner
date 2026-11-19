"""SQLAlchemy 2.0 ORM models (async-compatible, works on SQLite and PostgreSQL)."""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Store enum *values* (e.g. 'STRONG_ALPHA') rather than member names."""
    return [m.value for m in enum_cls]


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Chain(str, enum.Enum):
    solana = "solana"
    ethereum = "ethereum"
    base = "base"
    bsc = "bsc"


class Badge(str, enum.Enum):
    strong_alpha = "STRONG_ALPHA"   # 80-100
    moderate = "MODERATE"           # 60-79
    high_risk = "HIGH_RISK"         # < 60


class Token(Base):
    """One row per (chain, token address). Holds the *latest* market snapshot
    and denormalised score/security columns so list queries need no joins."""

    __tablename__ = "tokens"
    __table_args__ = (
        UniqueConstraint("chain", "address", name="uq_token_chain_address"),
        Index("ix_tokens_score", "alpha_score"),
        Index("ix_tokens_liquidity", "liquidity_usd"),
        Index("ix_tokens_volume_h24", "volume_h24"),
        Index("ix_tokens_pair_created", "pair_created_at"),
        Index("ix_tokens_chain_score", "chain", "alpha_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    address: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    image_url: Mapped[str | None] = mapped_column(String(512))

    # Primary (most liquid) pair
    pair_address: Mapped[str | None] = mapped_column(String(128))
    dex_id: Mapped[str | None] = mapped_column(String(64))
    pair_url: Mapped[str | None] = mapped_column(String(512))
    quote_symbol: Mapped[str | None] = mapped_column(String(32))
    pair_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Market metrics (latest)
    price_usd: Mapped[float | None] = mapped_column(Float)
    price_change_m5: Mapped[float | None] = mapped_column(Float)
    price_change_h1: Mapped[float | None] = mapped_column(Float)
    price_change_h6: Mapped[float | None] = mapped_column(Float)
    price_change_h24: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_m5: Mapped[float | None] = mapped_column(Float)
    volume_h1: Mapped[float | None] = mapped_column(Float)
    volume_h6: Mapped[float | None] = mapped_column(Float)
    volume_h24: Mapped[float | None] = mapped_column(Float)
    buys_m5: Mapped[int | None] = mapped_column(Integer)
    sells_m5: Mapped[int | None] = mapped_column(Integer)
    buys_h1: Mapped[int | None] = mapped_column(Integer)
    sells_h1: Mapped[int | None] = mapped_column(Integer)
    buys_h24: Mapped[int | None] = mapped_column(Integer)
    sells_h24: Mapped[int | None] = mapped_column(Integer)
    fdv: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    boosts_active: Mapped[int] = mapped_column(Integer, default=0)
    discovery_sources: Mapped[list[Any]] = mapped_column(JSON, default=list)

    # Denormalised security flags for table badges / filtering
    is_honeypot: Mapped[bool | None] = mapped_column(Boolean)
    is_mintable: Mapped[bool | None] = mapped_column(Boolean)
    is_freezable: Mapped[bool | None] = mapped_column(Boolean)
    lp_locked_pct: Mapped[float | None] = mapped_column(Float)
    top10_holders_pct: Mapped[float | None] = mapped_column(Float)
    buy_tax: Mapped[float | None] = mapped_column(Float)
    sell_tax: Mapped[float | None] = mapped_column(Float)

    # Latest score
    alpha_score: Mapped[int] = mapped_column(Integer, default=0)
    badge: Mapped[Badge] = mapped_column(Enum(Badge, native_enum=False, length=16, values_callable=_enum_values), default=Badge.high_risk)
    smart_money_hits: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    security: Mapped[SecurityAudit | None] = relationship(
        back_populates="token", uselist=False, cascade="all, delete-orphan", lazy="raise"
    )
    scores: Mapped[list[ScoreRecord]] = relationship(
        back_populates="token", cascade="all, delete-orphan", lazy="raise", order_by="ScoreRecord.scored_at.desc()"
    )
    snapshots: Mapped[list[MarketSnapshot]] = relationship(
        back_populates="token", cascade="all, delete-orphan", lazy="raise"
    )
    signals: Mapped[list[Signal]] = relationship(back_populates="token", cascade="all, delete-orphan", lazy="raise")


class SecurityAudit(Base):
    """Latest normalised security report for a token (1:1 with Token)."""

    __tablename__ = "security_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"), unique=True)
    providers: Mapped[list[Any]] = mapped_column(JSON, default=list)  # ["goplus", "rugcheck"]

    is_honeypot: Mapped[bool | None] = mapped_column(Boolean)
    is_mintable: Mapped[bool | None] = mapped_column(Boolean)
    is_freezable: Mapped[bool | None] = mapped_column(Boolean)
    is_open_source: Mapped[bool | None] = mapped_column(Boolean)
    is_proxy: Mapped[bool | None] = mapped_column(Boolean)
    owner_can_change_balance: Mapped[bool | None] = mapped_column(Boolean)
    hidden_owner: Mapped[bool | None] = mapped_column(Boolean)
    cannot_sell_all: Mapped[bool | None] = mapped_column(Boolean)
    transfer_pausable: Mapped[bool | None] = mapped_column(Boolean)
    buy_tax: Mapped[float | None] = mapped_column(Float)    # percent 0-100
    sell_tax: Mapped[float | None] = mapped_column(Float)   # percent 0-100
    lp_locked_pct: Mapped[float | None] = mapped_column(Float)      # percent 0-100 (locked + burned)
    top10_holders_pct: Mapped[float | None] = mapped_column(Float)  # percent 0-100, LP/burn excluded
    holder_count: Mapped[int | None] = mapped_column(Integer)
    creator_pct: Mapped[float | None] = mapped_column(Float)
    rugcheck_score: Mapped[float | None] = mapped_column(Float)
    risks: Mapped[list[Any]] = mapped_column(JSON, default=list)       # [{"name","level","description"}]
    top_holders: Mapped[list[Any]] = mapped_column(JSON, default=list) # [{"address","pct","tag"}]
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)    # provider payloads for audit trail

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    token: Mapped[Token] = relationship(back_populates="security", lazy="raise")


class ScoreRecord(Base):
    """Append-only score history (drives score sparkline + backtesting)."""

    __tablename__ = "score_records"
    __table_args__ = (Index("ix_score_records_token_time", "token_id", "scored_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    total: Mapped[int] = mapped_column(Integer)
    security: Mapped[float] = mapped_column(Float)
    momentum: Mapped[float] = mapped_column(Float)
    smart_money: Mapped[float] = mapped_column(Float)
    badge: Mapped[Badge] = mapped_column(Enum(Badge, native_enum=False, length=16, values_callable=_enum_values))
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON)   # full ScoreBreakdown JSON
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    token: Mapped[Token] = relationship(back_populates="scores", lazy="raise")


class MarketSnapshot(Base):
    """Per-cycle market snapshot (fallback chart series + historical analytics)."""

    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_token_time", "token_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    volume_h1: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    buys_h1: Mapped[int | None] = mapped_column(Integer)
    sells_h1: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    token: Mapped[Token] = relationship(back_populates="snapshots", lazy="raise")


class Signal(Base):
    """A high-score detection pushed to WebSocket / Telegram."""

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    score: Mapped[int] = mapped_column(Integer)
    badge: Mapped[Badge] = mapped_column(Enum(Badge, native_enum=False, length=16, values_callable=_enum_values))
    price_usd: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)    # LiveSignal JSON as broadcast
    # Top holders at signal time (wallet addresses), used by smart-wallet discovery.
    holders: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    token: Mapped[Token] = relationship(back_populates="signals", lazy="raise")


class WalletSource(str, enum.Enum):
    manual = "manual"   # added by the operator (CLI or data/smart_wallets.json)
    auto = "auto"       # discovered from holders of tokens that rallied after a signal
    birdeye = "birdeye" # profitable traders found and verified with the Birdeye API


class SmartWallet(Base):
    """Tracked wallets. A token held by an enabled wallet earns the 15-point smart-wallet component."""

    __tablename__ = "smart_wallets"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_smart_wallet_chain_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    address: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(128), default="")
    source: Mapped[WalletSource] = mapped_column(
        Enum(WalletSource, native_enum=False, length=16, values_callable=_enum_values)
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when the operator disables a wallet: auto-discovery never re-enables it.
    disabled_by_user: Mapped[bool] = mapped_column(Boolean, default=False)

    # Auto-discovery stats (also maintained for manual wallets, for information)
    wins: Mapped[int] = mapped_column(Integer, default=0)          # winner tokens it held at signal time
    appearances: Mapped[int] = mapped_column(Integer, default=0)   # signalled tokens it held
    win_tokens: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{"symbol","address","gain_pct"}]
    # Verified performance (Birdeye wallet PnL summary): {"win_rate","realized_usd_30d","trades_30d",...}
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Operator's buy-alert watchlist (Telegram). Independent of `enabled`, which controls scoring:
    # a tracked wallet can be any address, smart or not. Nullable so it can be added to live tables.
    track_buys: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PaperPosition(Base):
    """A simulated trade (no real money). One open position per strategy and token at a time."""

    __tablename__ = "paper_positions"
    __table_args__ = (
        Index("ix_paper_strategy_status", "strategy", "status"),
        Index("ix_paper_entry_time", "entry_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(32))
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    token_address: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64), default="")
    trigger: Mapped[str] = mapped_column(String(160), default="")      # e.g. "auto 2/3 bought $380", "score 86"
    trigger_wallet: Mapped[str | None] = mapped_column(String(64))     # wallet_follow: exit when it sells
    status: Mapped[str] = mapped_column(String(8), default="open")    # open | closed
    size_usd: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    entry_price: Mapped[float] = mapped_column(Float)                 # market price at the trigger
    fill_price: Mapped[float] = mapped_column(Float)                  # after simulated slippage + fees
    tp_pct: Mapped[float] = mapped_column(Float)
    sl_pct: Mapped[float] = mapped_column(Float)
    max_hold_minutes: Mapped[int] = mapped_column(Integer)
    last_price: Mapped[float | None] = mapped_column(Float)
    last_priced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    peak_pct: Mapped[float | None] = mapped_column(Float)             # vs market entry price
    trough_pct: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float | None] = mapped_column(Float)           # market price at exit
    exit_reason: Mapped[str | None] = mapped_column(String(24))       # take_profit | stop_loss | time | leader_sold | no_price
    pnl_usd: Mapped[float | None] = mapped_column(Float)              # net of simulated costs
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    # wallet_mirror: partial exits that copy the leader's sells
    remaining_frac: Mapped[float | None] = mapped_column(Float, nullable=True, default=1.0)
    proceeds_usd: Mapped[float | None] = mapped_column(Float, nullable=True, default=0.0)
    leader_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # last leader sell copied


class SignalOutcome(Base):
    """What happened after a signal, from GeckoTerminal 15-minute candles of its pool (so tokens that crashed
    out of the scanner's filters are still measured: no survivorship bias)."""

    __tablename__ = "signal_outcomes"
    __table_args__ = (Index("ix_signal_outcomes_signal_time", "signal_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), unique=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    symbol: Mapped[str] = mapped_column(String(64), default="")
    token_address: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column(Integer)
    signal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float | None] = mapped_column(Float)
    r_1h: Mapped[float | None] = mapped_column(Float)        # % change vs entry
    r_6h: Mapped[float | None] = mapped_column(Float)
    r_24h: Mapped[float | None] = mapped_column(Float)
    peak_pct: Mapped[float | None] = mapped_column(Float)    # best high within 24h (or so far)
    trough_pct: Mapped[float | None] = mapped_column(Float)  # worst low within 24h (or so far)
    peak_minutes: Mapped[int | None] = mapped_column(Integer)
    horizon_hours: Mapped[int] = mapped_column(Integer, default=0)  # 0 / 1 / 6 / 24: horizons measured so far
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # snapshots | candles
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WalletTrade(Base):
    """A buy by a tracked wallet, detected on-chain. Delivered to Telegram once (telegram_sent claim)."""

    __tablename__ = "wallet_trades"
    __table_args__ = (
        UniqueConstraint("chain", "tx_hash", "token_address", name="uq_wallet_trade"),
        Index("ix_wallet_trades_detected", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    wallet: Mapped[str] = mapped_column(String(64))
    wallet_label: Mapped[str] = mapped_column(String(128), default="")
    token_address: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    tx_hash: Mapped[str] = mapped_column(String(128))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    usd_value: Mapped[float | None] = mapped_column(Float)
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    pair_url: Mapped[str | None] = mapped_column(String(512))
    alpha_score: Mapped[int | None] = mapped_column(Integer)
    first_buy: Mapped[bool] = mapped_column(Boolean, default=True)   # first time this wallet bought it
    # Sells (nullable so they can be added to the live table; NULL side = buy, rows predating sells)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True, default="buy")        # buy | sell
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True, default="buy")       # buy | sell | transfer
    remaining_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_sold: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_basis_usd: Mapped[float | None] = mapped_column(Float, nullable=True)   # avg buy price per token
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    block_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class TrackerCursor(Base):
    """Where each tracker stream stopped (Solana: last signature per wallet; EVM: last block per chain)."""

    __tablename__ = "tracker_cursors"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[str] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BirdeyeCall(Base):
    """One row per Birdeye request: enforces the daily budget (free plan: ~1,000 calls / month)."""

    __tablename__ = "birdeye_calls"
    __table_args__ = (Index("ix_birdeye_calls_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(String(64))
    chain: Mapped[str] = mapped_column(String(16))
    status: Mapped[int] = mapped_column(Integer, default=0)   # HTTP status, 0 = network error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraderCandidate(Base):
    """A wallet seen as a profitable trader, waiting for (or holding the result of) verification."""

    __tablename__ = "trader_candidates"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_trader_candidate"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    address: Mapped[str] = mapped_column(String(64))
    origins: Mapped[list[Any]] = mapped_column(JSON, default=list)     # ["gainers_1W", "winner:<symbol>"]
    winner_hits: Mapped[list[Any]] = mapped_column(JSON, default=list) # [{"symbol","address","pnl_usd"}]
    best_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraderTokenFetch(Base):
    """Winner tokens whose top traders were already pulled (each costs one Birdeye call)."""

    __tablename__ = "trader_token_fetches"
    __table_args__ = (UniqueConstraint("chain", "token_address", name="uq_trader_token_fetch"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[Chain] = mapped_column(Enum(Chain, native_enum=False, length=16, values_callable=_enum_values))
    token_address: Mapped[str] = mapped_column(String(64))
    traders_found: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScannerRun(Base):
    """One row per scan cycle — powers the 'Scanner Status' KPI and ops monitoring."""

    __tablename__ = "scanner_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    passed_filters: Mapped[int] = mapped_column(Integer, default=0)
    scored: Mapped[int] = mapped_column(Integer, default=0)
    signals_emitted: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
