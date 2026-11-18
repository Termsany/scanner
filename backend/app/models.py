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
    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    token: Mapped[Token] = relationship(back_populates="signals", lazy="raise")


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
