"""SQLAlchemy 2.0 ORM models — the persistent record of everything the system does.

Design notes:
- Every order is represented by an ``Order`` row that captures the agent reasoning that
  produced it AND the broker's response (before/after submission). Nothing executes
  silently (safety requirement #5).
- ``AppSettings`` and ``RiskConfig`` are effectively singletons (id=1), seeded on first
  boot from RISK.md defaults.
- ``DailyRiskState`` tracks per-day P&L and the auto-pause flag (max-daily-loss rule).
- Agent reasoning and broker payloads are stored as JSON for full auditability.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.models.enums import (
    AssetClass,
    Direction,
    ExecutionMode,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    ProposalStatus,
    RiskDecisionType,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AppSettings(Base):
    """Singleton (id=1) holding user-selected app-level settings."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    execution_mode: Mapped[str] = mapped_column(
        String(32), default=ExecutionMode.A_PROPOSE_APPROVE.value
    )
    # Mirrors env BROKER_ENV but persisted so the app remembers the user's choice.
    broker_env: Mapped[str] = mapped_column(String(8), default="paper")

    # Asset class -> broker adapter name (e.g. {"stock": "alpaca", "crypto": "ccxt"}).
    broker_map: Mapped[dict] = mapped_column(JSON, default=dict)

    # Live-mode (Mode C) confirmation tracking. Re-confirmation is required each restart,
    # so this is compared against the current process start time at request handling.
    live_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # UI kill-switch state (the env KILL_SWITCH is a separate, additional backstop).
    kill_switch_engaged: Mapped[bool] = mapped_column(Boolean, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RiskConfig(Base):
    """Singleton (id=1) holding the deterministic risk limits (seeded from RISK.md).

    These are enforced by ``app.risk`` (Milestone 3). The ceiling on risk_per_trade is in
    config, not here — this table holds the *active* values, never above the ceiling.
    """

    __tablename__ = "risk_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    risk_per_trade: Mapped[float] = mapped_column(Float, default=0.01)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=3)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=0.03)
    max_total_exposure: Mapped[float] = mapped_column(Float, default=0.06)
    per_pair_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=30)
    # Master switch for the max-daily-loss circuit breaker. ON by default (RISK.md). May be
    # turned OFF for testing on a demo account; when off, the daily-loss auto-pause and veto
    # are skipped. Disabling it removes a real-money protection, so the UI warns loudly and
    # every toggle is logged at WARNING.
    daily_loss_breaker_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TradeProposalRecord(Base):
    """A proposal produced by the orchestrator, plus the full agent reasoning bundle."""

    __tablename__ = "trade_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(16), default="1h")
    direction: Mapped[str] = mapped_column(String(16))  # Direction enum value
    entry: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")

    # Full structured reasoning from each agent (fundamental/technical/orchestrator).
    reasoning: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(
        String(24), default=ProposalStatus.PENDING_RISK.value, index=True
    )

    # Risk Manager outcome (populated after the deterministic pass).
    risk_decision: Mapped[str | None] = mapped_column(String(16))  # RiskDecisionType
    risk_reason: Mapped[str | None] = mapped_column(Text)
    approved_qty: Mapped[float | None] = mapped_column(Float)
    risk_amount: Mapped[float | None] = mapped_column(Float)
    review_decision: Mapped[str | None] = mapped_column(String(16))  # LLM reviewer: confirm/veto
    watch: Mapped[bool] = mapped_column(Boolean, default=False)       # setup forming (wait for trigger)

    orders: Mapped[list["Order"]] = relationship(back_populates="proposal")


class Order(Base):
    """An order through its full lifecycle. Logged before AND after submission."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("trade_proposals.id"))
    proposal: Mapped["TradeProposalRecord | None"] = relationship(back_populates="orders")

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))        # OrderSide
    order_type: Mapped[str] = mapped_column(String(8))  # OrderType
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)

    broker: Mapped[str] = mapped_column(String(24), default="paper")
    broker_env: Mapped[str] = mapped_column(String(8), default="paper")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(
        String(24), default=OrderStatus.PENDING.value, index=True
    )
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)

    # Audit trail: the request we sent and the response we got back.
    submit_payload: Mapped[dict | None] = mapped_column(JSON)
    broker_response: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class Position(Base):
    """An open or closed position, with realized/unrealized P&L tracking."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(16))  # Direction (long/short)
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(
        String(8), default=PositionStatus.OPEN.value, index=True
    )
    last_price: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float)

    # Risk at entry (used for exposure accounting).
    risk_amount: Mapped[float | None] = mapped_column(Float)

    broker: Mapped[str] = mapped_column(String(24), default="paper")
    broker_env: Mapped[str] = mapped_column(String(8), default="paper")


class DailyRiskState(Base):
    """Per-day risk accounting. One row per UTC date; drives the daily-loss auto-pause."""

    __tablename__ = "daily_risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # YYYY-MM-DD
    starting_equity: Mapped[float | None] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    trading_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Mt5Credentials(Base):
    """Singleton (id=1) MT5/Exness connection details entered from the UI.

    Stored locally in the gitignored SQLite DB. The password is write-only over the API
    (never returned). Leaving login/password blank attaches to whatever account the running
    MT5 terminal is already logged into — the recommended, no-secret-stored path.
    """

    __tablename__ = "mt5_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    login: Mapped[int | None] = mapped_column(Integer)
    password: Mapped[str | None] = mapped_column(String(128))
    server: Mapped[str | None] = mapped_column(String(64))
    path: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LlmConfig(Base):
    """Singleton (id=1) AI-provider selection set from the UI.

    Stored locally in the gitignored DB. The API key is write-only over the API (never
    returned). Falls back to .env when unset.
    """

    __tablename__ = "llm_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str | None] = mapped_column(String(16))   # "anthropic" | "gemini"
    model: Mapped[str | None] = mapped_column(String(64))
    api_key: Mapped[str | None] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WatchItem(Base):
    """A pair the autonomous scanner watches (and may auto-trade, per execution mode)."""

    __tablename__ = "watch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32))
    asset_class: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(8), default="1h")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScanConfig(Base):
    """Singleton (id=1) controlling the autonomous watchlist scanner."""

    __tablename__ = "scan_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # off by default (safety)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=120)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AdvisorConfig(Base):
    """Singleton (id=1) controlling the open-position advisor's auto-watch."""

    __tablename__ = "advisor_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # off by default
    # When True the advisor may autonomously act on its highest-confidence, risk-reducing
    # decisions (close an invalidated trade, lock a winner's stop to breakeven). Off by default.
    auto_execute: Mapped[bool] = mapped_column(Boolean, default=False)
    # When True, after auto-CLOSING an invalidated trade the advisor re-runs the analysis and,
    # if the engine + risk manager approve a fresh setup, opens it properly (sized, with SL/TP).
    auto_reenter: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentRun(Base):
    """A record of one agent-cycle / event for transparency and the reflection agent."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    agent: Mapped[str] = mapped_column(String(32), index=True)  # fundamental/technical/...
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)
