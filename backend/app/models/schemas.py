"""Pydantic schemas — the typed contracts between agents, risk, brokers, and the API.

Agents MUST return JSON matching the relevant schema here. The agent layer (M4/M6) parses
LLM output into these defensively; malformed output is rejected, never executed.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
    ReviewDecision,
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


# --- Gemini-safe LLM output schemas (no free-form dict maps; Gemini structured output
#     only supports a strict OpenAPI subset). We enrich these with computed indicators after. ---


class TimeframeReadLLM(BaseModel):
    timeframe: str
    trend: str
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    comment: str = ""


class TechnicalReadLLM(BaseModel):
    symbol: str
    timeframes: list[TimeframeReadLLM] = Field(default_factory=list)
    overall_trend: str = "sideways"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    notes: str = ""


class TradeProposalLLM(BaseModel):
    """Flat decision the orchestrator LLM returns; we attach the reasoning bundle after."""

    symbol: str
    direction: Direction
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = ""


class TradeReviewLLM(BaseModel):
    """The LLM's review of a deterministic setup. It can only confirm or veto (never create,
    flip, or widen a trade) and may only lower confidence."""

    decision: ReviewDecision
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = ""
    concerns: list[str] = Field(default_factory=list)


class ConditionalSuggestion(BaseModel):
    """A 'wait for the trigger' entry the engine proposes when a setup is valid in direction but
    the immediate entry is blocked by structure (e.g. a support cluster between entry and target).

    Instead of discarding the idea, enter only once price clears the level — a break-stop order.
    Levels/RR are computed FROM the trigger so the R:R is honest; the trade is re-checked at the
    trigger before it ever opens."""

    order_type: str            # ConditionalOrderType value (e.g. "sell_stop", "buy_stop")
    trigger_price: float       # price at which the setup arms into a trade
    stop_loss: float
    take_profit: float
    confidence: float = Field(0.0, ge=0.0, le=1.0)   # projected (re-scored at the trigger)
    rr: float                  # reward:risk measured from the trigger entry
    reason: str = ""


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

    # True when the setup is forming (e.g. trend aligned but momentum not yet) — "watching",
    # waiting for a trigger, rather than a flat reject.
    watch: bool = False

    # A conditional break-entry the engine suggests when the immediate entry is blocked by
    # structure but the idea is valid once price clears the level (see ConditionalSuggestion).
    conditional: ConditionalSuggestion | None = None

    # LLM reviewer outcome on the deterministic setup: "confirm" | "veto" | None (rules only).
    review_decision: str | None = None

    # Market regime read + the strategy it permits (trend / mean_reversion / stand_aside), so the
    # "what are we doing and why" is explicit and visible rather than buried in the rationale.
    regime: str | None = None
    strategy: str | None = None
    # Trend-alignment score 0..1 (how clearly the direction stacks up across TFs + strength +
    # momentum). ~0.85+ = an "A+" fully-aligned trend (the clearest up/down). None = not a trend trade.
    alignment: float | None = None

    # Structured AI decision (when the AI is the decider) — the created scenarios, the chosen one, why,
    # risks, and the action/levels — so the UI can render it cleanly instead of parsing the rationale.
    ai_decision: dict | None = None

    # AI momentum CLASSIFICATION at an ambiguous-momentum fork: {category, evidence, confidence}. Set
    # ONLY when the AI momentum-read ran (MACD rolling over / RSI stretched); None otherwise. The engine
    # decides the action from it — this field is for the UI to show WHY momentum was read the way it was.
    momentum_read: dict | None = None

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

    Mirrors the RiskConfig DB row. ``risk_per_trade_ceiling`` is a hard cap (RISK.md: 3%)
    that the manager re-clamps against defensively, regardless of the stored value.
    """

    model_config = ConfigDict(from_attributes=True)

    risk_per_trade: float = 0.03
    max_open_positions: int = 3
    max_daily_loss: float = 0.03
    max_total_exposure: float = 0.06
    per_pair_cooldown_minutes: int = 30
    # After a STOP-OUT, stand down on the SAME symbol+direction for this much longer than the normal
    # per-pair cooldown — re-entering a just-failed setup is how one stop becomes three (XAGGBP case).
    loss_cooldown_minutes: int = 180
    risk_per_trade_ceiling: float = 0.03
    # Effective state of the daily-loss circuit breaker handed to the manager (already
    # resolved by the service layer). When False, the daily-loss gate is skipped.
    daily_loss_breaker_enabled: bool = True


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
    # True when the size was floored UP to the broker's minimum lot because the risk-budget size was
    # smaller — so this trade may risk MORE than the per-trade cap (you can't trade smaller). The UI
    # flags it; it's an explicit, accepted exception (small money), not a cap breach we chose.
    min_lot_floored: bool = False
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
    source: str | None = None   # who opened it (ai / rsi_over / armed / hybrid / manual / …)
    opened_at: datetime | None = None   # when the position was opened (app-tracked rows)
    # Margin required to hold the position, in the account currency (USD) — the "cost to open".
    # Broker-computed (MT5) with full currency conversion; None when the broker can't supply it.
    cost_usd: float | None = None
    # When the trade closed (closed trades only). For the broker-truth journal this is the
    # exit deal's time, so it matches the Exness journal.
    closed_at: datetime | None = None


# --------------------------------------------------------------------------- #
#  Settings / risk (API-facing)
# --------------------------------------------------------------------------- #


class PositionAdvice(BaseModel):
    """Management guidance for an OPEN position (distinct from new-entry analysis) — e.g.
    protect a winner / cut a loser ahead of a high-impact event."""

    symbol: str
    direction: str
    unrealized_pnl: float
    has_stop: bool
    severity: str            # info | warn | danger
    headline: str
    detail: str
    # When the position was opened + who opened it (manual / rsi_over / armed / hybrid / auto_trade /
    # ai / …), so the advisor card shows the trade's age and origin at a glance.
    opened_at: datetime | None = None
    source: str | None = None
    # Re-check of the original plan against the current read: is the trade still on track?
    thesis: str = "unknown"  # intact | weakening | invalidated | unknown
    r_multiple: float | None = None   # progress in R (profit / planned risk)
    event_label: str | None = None
    minutes_to_event: int | None = None
    events_soon: str | None = None    # SOFT heads-up: upcoming medium-impact events (display only)


class RiskConfigView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    risk_per_trade: float
    max_open_positions: int
    max_daily_loss: float
    max_total_exposure: float
    per_pair_cooldown_minutes: int
    loss_cooldown_minutes: int = 180
    daily_loss_breaker_enabled: bool = True


class AppSettingsView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    execution_mode: ExecutionMode
    broker_env: str
    broker_map: dict
    kill_switch_engaged: bool
    trend_only_mode: bool = True
    st_band_mode: bool = False
    ai_momentum_read: bool = True
    ai_review_enabled: bool = False
    disabled_filters: list[str] = []
    journal_reset_at: datetime | None = None
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
    unrealized_pnl: float = 0.0          # floating P&L of open broker positions
    total_risk_amount: float = 0.0       # exposure: risk-at-entry across open positions
    trades_count: int = 0
    trading_paused: bool = False
    pause_reason: str | None = None
    max_daily_loss: float
    daily_loss_limit_amount: float | None = None
    # Whether the daily-loss circuit breaker is currently armed. When False the breaker is
    # OFF (e.g. demo-account testing): no auto-pause and no daily-loss veto.
    daily_loss_breaker_enabled: bool = True


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
    review_decision: str | None = None
    watch: bool = False
    reasoning: dict = Field(default_factory=dict)


class TradeEconomics(BaseModel):
    """What a trade costs and how leveraged it is, BEFORE you approve it. Broker-computed (MT5)
    so margin/leverage include full currency conversion. Fields are None when unavailable."""

    lots: float | None = None
    qty_units: float | None = None
    margin_usd: float | None = None       # the "spend" — cash the broker holds to open it
    notional_usd: float | None = None     # market exposure (account currency)
    leverage: float | None = None         # notional / margin
    pct_of_equity: float | None = None    # notional / equity, as a fraction


class SizePreviewResponse(BaseModel):
    """Risk verdict + economics for a proposal at a chosen lot size (Mode A pre-approval)."""

    risk: RiskDecision
    economics: TradeEconomics | None = None
    # True when the requested size was reduced — by the 3% per-trade ceiling or the exposure budget.
    capped: bool = False
    # Largest lot size allowed by the 3% per-trade ceiling (for the UI to show the cap). None if
    # it can't be computed (no equity / stop).
    max_lots: float | None = None


class AnalyzeResponse(BaseModel):
    proposal_id: int
    status: str
    proposal: TradeProposal
    risk: RiskDecision
    analyzed_at: datetime | None = None   # when this analysis ran (shown as "analysed X ago")


class BacktestRequest(BaseModel):
    symbol: str
    asset_class: AssetClass = AssetClass.STOCK
    timeframe: str = "1h"
    bars: int = Field(400, ge=50, le=2000)
    starting_equity: float = Field(100_000.0, gt=0)


class BacktestTrade(BaseModel):
    entry_time: datetime
    exit_time: datetime
    direction: Direction
    entry: float
    exit: float
    qty: float
    pnl: float
    r_multiple: float          # pnl / risk_at_entry
    bars_held: int
    exit_reason: str           # stop | target | end_of_data


class EquityPoint(BaseModel):
    ts: datetime
    equity: float


class BacktestMetrics(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_r_multiple: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float | None   # None when there are no losses (undefined)
    net_pnl: float
    return_pct: float
    max_drawdown_pct: float
    starting_equity: float
    ending_equity: float


class BacktestResult(BaseModel):
    symbol: str
    asset_class: AssetClass
    timeframe: str
    bars: int
    metrics: BacktestMetrics
    equity_curve: list[EquityPoint]
    trades: list[BacktestTrade]
    disclaimer: str = (
        "Backtest results are hypothetical and do not guarantee live (or even paper) results."
    )


class ReflectionReport(BaseModel):
    """Output of the read-only Reflection/Journal agent. It NEVER places trades."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trades_reviewed: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float | None = None
    summary: str = ""
    patterns: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)


class ReflectionInsights(BaseModel):
    """The free-form part the LLM fills in (stats are computed deterministically)."""

    summary: str = ""
    patterns: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    app_env: str
    broker_env: str
    kill_switch: bool
    time: datetime
