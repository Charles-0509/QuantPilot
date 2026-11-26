from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid4str() -> str:
    return str(uuid.uuid4())


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    template_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    versions: Mapped[list["StrategyVersion"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan"
    )


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    strategy: Mapped[Strategy] = relationship(back_populates="versions")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), default=1, index=True
    )
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    equity_curve: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    benchmark_curve: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    trades: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestArtifact(Base):
    __tablename__ = 'backtest_artifacts'

    run_id: Mapped[str] = mapped_column(ForeignKey('backtest_runs.id', ondelete='CASCADE'), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), default=1, index=True
    )
    unique_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    bar_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="created")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), default=1, index=True
    )
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(24))
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    notional: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StrategyPosition(Base):
    """Quantity opened by one strategy; manual and other-strategy holdings are excluded."""

    __tablename__ = "strategy_positions"
    __table_args__ = (
        UniqueConstraint("user_id", "strategy_id", "symbol", name="uq_strategy_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    strategy_id: Mapped[str] = mapped_column(
        ForeignKey("strategies.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MarketBar(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_market_bar"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(12), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    feed: Mapped[str] = mapped_column(String(12), default="iex")


class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    category: Mapped[str] = mapped_column(String(32), default="system", index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ExecutionIncident(Base):
    """Persistent execution quarantine (long-only or owned-short invariant) for one user and symbol."""

    __tablename__ = "execution_incidents"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_execution_incident_user_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    reason: Mapped[str] = mapped_column(Text)
    trigger_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RiskSettings(Base):
    __tablename__ = "risk_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True, default=1
    )
    max_symbol_pct: Mapped[float] = mapped_column(Float, default=10.0)
    max_total_exposure_pct: Mapped[float] = mapped_column(Float, default=80.0)
    max_positions: Mapped[int] = mapped_column(Integer, default=8)
    max_daily_loss_pct: Mapped[float] = mapped_column(Float, default=3.0)
    max_intraday_drawdown_pct: Mapped[float] = mapped_column(Float, default=5.0)
    stale_data_seconds: Mapped[int] = mapped_column(Integer, default=900)
    cash_sweep_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cash_sweep_symbol: Mapped[str] = mapped_column(String(16), default="SGOV")
    cash_sweep_buffer_pct: Mapped[float] = mapped_column(Float, default=2.0)
    cash_sweep_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cash_sweep_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    cash_sweep_min_trade_usd: Mapped[float] = mapped_column(Float, default=1000.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EngineState(Base):
    __tablename__ = "engine_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True, default=1
    )
    status: Mapped[str] = mapped_column(String(24), default="paused")
    reason: Mapped[str] = mapped_column(Text, default="首次启动，等待用户开启")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    day_start_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_high_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    session_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WatchlistGroup(Base):
    """User-named watchlist section; every symbol belongs to exactly one."""

    __tablename__ = "watchlist_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchlistItem(Base):
    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), primary_key=True, default=1
    )
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("watchlist_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectionConfig(Base):
    """Encrypted, local-only Alpaca Paper credentials entered from Settings."""

    __tablename__ = "connection_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), unique=True, index=True, default=1
    )
    api_key_cipher: Mapped[str] = mapped_column(Text)
    api_secret_cipher: Mapped[str] = mapped_column(Text)
    data_feed: Mapped[str] = mapped_column(String(12), default="iex")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LlmSettings(Base):
    """Per-user OpenAI-compatible endpoint used only to draft strategies."""

    __tablename__ = "llm_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), default="openai")
    base_url: Mapped[str] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(120))
    api_key_cipher: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BrokerSettings(Base):
    __tablename__ = 'broker_settings'
    __table_args__ = (
        Index('uq_active_ibkr_gateway', 'ibkr_gateway_url', unique=True,
              sqlite_where=text("broker = 'ibkr' AND ibkr_account_id != ''")),
        Index('uq_active_ibkr_account', 'ibkr_account_id', unique=True,
              sqlite_where=text("broker = 'ibkr' AND ibkr_account_id != ''")),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey('auth_users.id', ondelete='CASCADE'), primary_key=True)
    broker: Mapped[str] = mapped_column(String(16), default='alpaca')
    trading_mode: Mapped[str] = mapped_column(String(8), default='paper')
    ibkr_gateway_url: Mapped[str] = mapped_column(String(500), default='https://127.0.0.1:5000/v1/api')
    ibkr_account_id: Mapped[str] = mapped_column(String(32), default='')
    ibkr_verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    market_data_source: Mapped[str] = mapped_column(String(16), default='alpaca')
    revision: Mapped[int] = mapped_column(Integer, default=0)
    live_consent_context: Mapped[str | None] = mapped_column(String(64), nullable=True)
    live_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BrokerLiveChallenge(Base):
    __tablename__ = 'broker_live_challenges'

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('auth_users.id', ondelete='CASCADE'), index=True)
    context_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthUser(Base):
    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64))
    username_normalized: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OAuthAccessToken(Base):
    __tablename__ = "oauth_access_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
