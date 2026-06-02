"""Pydantic schemas — the typed contracts between agents, risk, brokers, and the API.

Agents MUST return JSON matching the relevant schema here. The agent layer (M4/M6) parses
LLM output into these defensively; malformed output is rejected, never executed.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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
    TradingBias,
)

# --------------------------------------------------------------------------- #
#  Market data
# --------------------------------------------------------------------------- #


class Candle(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class OHLCVSeries(BaseModel):
    symbol: str
    timeframe: str
    candles: list[Candle] = Field(default_factory=list)


class Quote(BaseModel):
    symbol: str
    price: float
    ts: datetime


# --------------------------------------------------------------------------- #
#  Agent outputs (strict JSON contracts)
# --------------------------------------------------------------------------- #


class EventWindow(BaseModel):
    """A high-impact window during which the system should STAND ASIDE."""

    label: str
    start: datetime
    end: datetime
    importance: str = "high"  # low | medium | high


class FundamentalRead(BaseModel):
    """Output of the Fundamental Analyst agent."""

    symbol: str
    bias: TradingBias
    key_drivers: list[str] = Field(default_factory=list)
    # Surprise-vs-expectation framing, not raw headlines.
    surprise_assessment: str = ""
    stand_aside_windows: list[EventWindow] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    notes: str = ""


class TimeframeRead(BaseModel):
    timeframe: str
    trend: str                       # up | down | sideways
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    indicators: dict[str, float] = Field(default_factory=dict)
    patterns: list[str] = Field(default_factory=list)
    comment: str = ""


class TechnicalRead(BaseModel):
    """Output of the Technical Analyst agent (analyzes numbers, not images)."""

    symbol: str
    timeframes: list[TimeframeRead] = Field(default_factory=list)
    overall_trend: str = "sideways"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    notes: str = ""


class TradeProposal(BaseModel):
    """Output of the Orchestrator. ``direction == NO_TRADE`` is a valid, encouraged result."""

    symbol: str
    asset_class: AssetClass
    timeframe: str = "1h"
    direction: Direction
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = ""

    # The full reasoning bundle that produced this proposal (for audit + UI).
    fundamental: FundamentalRead | None = None
    technical: TechnicalRead | None = None

    @property
    def is_actionable(self) -> bool:
        return (
            self.direction in (Direction.LONG, Direction.SHORT)
            and self.entry is not None
            and self.stop_loss is not None
        )


# --------------------------------------------------------------------------- #
#  Risk Manager (deterministic) I/O
# --------------------------------------------------------------------------- #


class AccountState(BaseModel):
    """Snapshot of the account used by the deterministic Risk Manager."""

    equity: float
    cash: float
    open_positions: int = 0
    total_risk_amount: float = 0.0       # sum of risk across open positions
    daily_realized_pnl: float = 0.0
    trading_paused: bool = False


class RiskLimits(BaseModel):
    """The active risk limits handed to the deterministic Risk Manager.

    Mirrors the RiskConfig DB row. ``risk_per_trade_ceiling`` is a hard cap (RISK.md: 2%)
    that the manager re-clamps against defensively, regardless of the stored value.
    """

    model_config = ConfigDict(from_attributes=True)

    risk_per_trade: float = 0.01
    max_open_positions: int = 3
    max_daily_loss: float = 0.03
    max_total_exposure: float = 0.06
    per_pair_cooldown_minutes: int = 30
    risk_per_trade_ceiling: float = 0.02


class RiskDecision(BaseModel):
    """Final, deterministic verdict on a proposal. The Risk Manager's word is final."""

    decision: RiskDecisionType
    approved: bool
    reason: str
    symbol: str
    side: OrderSide | None = None
    approved_qty: float = 0.0
    risk_amount: float = 0.0
    risk_pct_of_equity: float = 0.0
    # Echo of the limits that were checked, for transparency in the UI/log.
    checks: dict[str, bool] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Orders / positions (API-facing views)
# --------------------------------------------------------------------------- #


class OrderRequest(BaseModel):
    symbol: str
    asset_class: AssetClass
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    qty: float
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


class OrderResult(BaseModel):
    broker_order_id: str | None = None
    status: OrderStatus
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    raw: dict = Field(default_factory=dict)
    error: str | None = None


class PositionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    asset_class: str
    direction: str
    qty: float
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    status: str
    last_price: float | None = None
    unrealized_pnl: float = 0.0
    realized_pnl: float | None = None


# --------------------------------------------------------------------------- #
#  Settings / risk (API-facing)
# --------------------------------------------------------------------------- #


class RiskConfigView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    risk_per_trade: float
    max_open_positions: int
    max_daily_loss: float
    max_total_exposure: float
    per_pair_cooldown_minutes: int


class AppSettingsView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    execution_mode: ExecutionMode
    broker_env: str
    broker_map: dict
    kill_switch_engaged: bool
    live_confirmed_at: datetime | None = None


class SettingsResponse(BaseModel):
    app: AppSettingsView
    risk: RiskConfigView
    # Env-level safety state (read-only, surfaced so the UI can warn).
    env_kill_switch: bool
    env_broker_env: str
    live_re_confirm_required: bool


class RiskStateView(BaseModel):
    trade_date: str
    starting_equity: float | None = None
    realized_pnl: float = 0.0
    trades_count: int = 0
    trading_paused: bool = False
    pause_reason: str | None = None
    max_daily_loss: float
    daily_loss_limit_amount: float | None = None


class AnalyzeRequest(BaseModel):
    symbol: str
    asset_class: AssetClass = AssetClass.STOCK
    timeframe: str = "1h"


class ProposalView(BaseModel):
    """API view of a stored proposal + its risk outcome."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    symbol: str
    asset_class: str
    timeframe: str
    direction: str
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float
    rationale: str
    status: str
    risk_decision: str | None = None
    risk_reason: str | None = None
    approved_qty: float | None = None
    risk_amount: float | None = None
    reasoning: dict = Field(default_factory=dict)


class AnalyzeResponse(BaseModel):
    proposal_id: int
    status: str
    proposal: TradeProposal
    risk: RiskDecision


class HealthResponse(BaseModel):
    status: str
    app_env: str
    broker_env: str
    kill_switch: bool
    time: datetime
