// Types mirroring the backend Pydantic schemas (kept in sync by hand for now).

export type AssetClass = "stock" | "crypto" | "forex" | "metal" | "energy" | "index";
export type Direction = "long" | "short" | "no_trade";
export type ExecutionMode = "A_PROPOSE_APPROVE" | "B_AUTO_PAPER" | "C_AUTO_LIVE";

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OHLCVSeries {
  symbol: string;
  timeframe: string;
  candles: Candle[];
}

export interface RiskDecision {
  decision: "approved" | "resized" | "vetoed";
  approved: boolean;
  reason: string;
  symbol: string;
  side: "buy" | "sell" | null;
  approved_qty: number;
  risk_amount: number;
  risk_pct_of_equity: number;
  min_lot_floored?: boolean;   // sized up to the broker minimum; may exceed the per-trade cap
  checks: Record<string, boolean>;
}

export interface TimeframeRead {
  timeframe: string;
  trend: string;
  support_levels: number[];
  resistance_levels: number[];
  indicators: Record<string, number>;
  patterns: string[];
  comment: string;
}

export interface TechnicalRead {
  symbol: string;
  timeframes: TimeframeRead[];
  overall_trend: string;
  confidence: number;
  notes: string;
}

export interface FundamentalRead {
  symbol: string;
  bias: "bullish" | "bearish" | "neutral";
  key_drivers: string[];
  surprise_assessment: string;
  stand_aside_windows: { label: string; start: string; end: string; importance: string }[];
  confidence: number;
  notes: string;
}

// A 'wait for the break' entry the engine suggests when a setup is valid but blocked by structure.
export interface ConditionalSuggestion {
  order_type: string;        // "sell_stop" | "buy_stop" | "sell_limit" | "buy_limit"
  trigger_price: number;
  stop_loss: number;
  take_profit: number;
  confidence: number;
  rr: number;
  reason: string;
}

export interface TradeProposal {
  symbol: string;
  asset_class: AssetClass;
  timeframe: string;
  direction: Direction;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  confidence: number;
  rationale: string;
  watch?: boolean;
  review_decision: string | null;
  regime?: string | null;       // trending | moderate | ranging | volatile
  strategy?: string | null;     // trend | mean_reversion | stand_aside
  conditional?: ConditionalSuggestion | null;
  technical: TechnicalRead | null;
  fundamental: FundamentalRead | null;
}

// An armed conditional setup (the Armed/Pending panel).
export interface ConditionalSetupView {
  id: number;
  created_at: string;
  symbol: string;
  asset_class: string;
  timeframe: string;
  direction: string;
  order_type: string;
  trigger_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  confidence: number;
  rr: number | null;
  rationale: string;
  status: string;            // armed | triggered | rejected | expired | cancelled
  source: string;            // manual | hybrid | scanner
  auto_execute: boolean;
  require_close_confirm: boolean;
  valid_until: string | null;
  triggered_at: string | null;
  result_proposal_id: number | null;
  last_note: string | null;
  desired_lots: number | null;
  current_price: number | null;     // armed only: live distance to the trigger
  pips_to_trigger: number | null;   // FX only
  pct_to_trigger: number | null;
}

export interface AnalyzeResponse {
  proposal_id: number;
  status: string;
  proposal: TradeProposal;
  risk: RiskDecision;
}

export interface TradeEconomics {
  lots: number | null;
  qty_units: number | null;
  margin_usd: number | null;
  notional_usd: number | null;
  leverage: number | null;
  pct_of_equity: number | null;
}

export interface SizePreviewResponse {
  risk: RiskDecision;
  economics: TradeEconomics | null;
  capped: boolean;
  max_lots: number | null;
}

export interface ProposalView {
  id: number;
  created_at: string;
  symbol: string;
  asset_class: string;
  timeframe: string;
  direction: Direction;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  confidence: number;
  rationale: string;
  status: string;
  risk_decision: string | null;
  risk_reason: string | null;
  approved_qty: number | null;
  risk_amount: number | null;
  review_decision?: string | null;
  watch?: boolean;
  reasoning: { technical?: TechnicalRead; fundamental?: FundamentalRead };
}

export interface PositionView {
  id: number;
  symbol: string;
  asset_class: string;
  direction: string;
  qty: number;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  status: string;
  last_price: number | null;
  unrealized_pnl: number;
  realized_pnl: number | null;
  cost_usd: number | null;
  closed_at: string | null;
}

export interface PositionAdvice {
  symbol: string;
  direction: string;
  unrealized_pnl: number;
  has_stop: boolean;
  severity: "info" | "warn" | "danger";
  headline: string;
  detail: string;
  thesis: "intact" | "weakening" | "invalidated" | "unknown";
  r_multiple: number | null;
  event_label: string | null;
  minutes_to_event: number | null;
}

export interface AdvisorAction {
  symbol: string;
  action: string;
  kind?: string | null;
  stop?: number | null;
  ok: boolean;
  reason: string;
  intended?: string | null;
  error?: string | null;
}

export interface AdvisorState {
  enabled: boolean;
  auto_execute: boolean;
  auto_reenter: boolean;
  interval_seconds: number;
  last_run_at: string | null;
  advice: PositionAdvice[];
  actions: AdvisorAction[];
}

export interface AdvisorActivityItem {
  run_id: number;
  seq: number;
  at: string | null;
  symbol: string;
  action: string;
  kind?: string | null;
  stop?: number | null;
  ok: boolean;
  reason: string;
  error?: string | null;
}

export interface SettingsResponse {
  app: {
    execution_mode: ExecutionMode;
    broker_env: string;
    broker_map: Record<string, string>;
    kill_switch_engaged: boolean;
    trend_only_mode: boolean;
    scalp_mode: boolean;
    ai_led_mode: boolean;
    live_confirmed_at: string | null;
  };
  risk: {
    risk_per_trade: number;
    max_open_positions: number;
    max_daily_loss: number;
    max_total_exposure: number;
    per_pair_cooldown_minutes: number;
    daily_loss_breaker_enabled: boolean;
  };
  env_kill_switch: boolean;
  env_broker_env: string;
  live_re_confirm_required: boolean;
}

export interface RiskState {
  trade_date: string;
  starting_equity: number | null;
  realized_pnl: number;
  unrealized_pnl: number;
  total_risk_amount: number;
  trades_count: number;
  trading_paused: boolean;
  pause_reason: string | null;
  max_daily_loss: number;
  daily_loss_limit_amount: number | null;
  daily_loss_breaker_enabled: boolean;
}

export interface BacktestMetrics {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_r_multiple: number;
  avg_win_r: number;
  avg_loss_r: number;
  profit_factor: number | null;
  net_pnl: number;
  return_pct: number;
  max_drawdown_pct: number;
  starting_equity: number;
  ending_equity: number;
}

export interface BacktestTrade {
  entry_time: string;
  exit_time: string;
  direction: Direction;
  entry: number;
  exit: number;
  qty: number;
  pnl: number;
  r_multiple: number;
  bars_held: number;
  exit_reason: string;
}

export interface BacktestResult {
  symbol: string;
  asset_class: AssetClass;
  timeframe: string;
  bars: number;
  metrics: BacktestMetrics;
  equity_curve: { ts: string; equity: number }[];
  trades: BacktestTrade[];
  disclaimer: string;
}

export interface HybridState {
  enabled: boolean;
  interval_seconds: number;
  min_confidence: number;
  conditional_enabled: boolean;
  max_armed: number;
  last_run_at: string | null;
  last_result: string | null;
}

export interface OpportunityView {
  symbol: string;
  asset_class: AssetClass;
  timeframe: string;
  direction: Direction;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  rr: number | null;
  confidence: number;
  watch: boolean;
  rationale: string;
  risk_approved: boolean;
  risk_decision: string | null;
  risk_reason: string | null;
  already_open: boolean;
  conditional?: ConditionalSuggestion | null;
  lots?: number | null;        // size that would be opened
  risk_usd?: number | null;
  reward_usd?: number | null;
  regime?: string | null;
  strategy?: string | null;
}

export interface WatchItem {
  id: number;
  symbol: string;
  asset_class: string;
  timeframe: string;
  enabled: boolean;
  recommended?: boolean;
}

export interface WatchlistResponse {
  items: WatchItem[];
  scan_enabled: boolean;
  interval_seconds: number;
  last_scan_at: string | null;
}

export interface LlmStatus {
  provider: string;
  model: string;
  available: boolean;
  tested_ok?: boolean;
  error?: string | null;
}

export interface Mt5Status {
  configured: boolean;
  connected: boolean;
  is_paper?: boolean;
  equity?: number;
  cash?: number;
  open_positions?: number;
  login?: number | null;
  server?: string | null;
  error?: string;
}

export interface ReflectionReport {
  generated_at: string;
  trades_reviewed: number;
  win_rate: number;
  net_pnl: number;
  profit_factor: number | null;
  summary: string;
  patterns: string[];
  lessons: string[];
}

export interface CalibrationBucket {
  bucket: string;             // confidence range, e.g. "70-80%"
  trades: number;
  wins: number;
  win_rate: number | null;    // null when the bucket has no trades yet
  avg_r: number | null;       // mean realized R
}

export interface JournalStats {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  expectancy_r: number | null;   // mean realized R per trade
  avg_win_r: number | null;
  avg_loss_r: number | null;
  profit_factor: number | null;
  total_r: number | null;
  max_drawdown_r: number | null;
}

// Structured (and optionally Arabic) reformatting of a raw AI-review rationale.
export interface ExplainedReview {
  decision: string;        // headline, e.g. "NO TRADE" / "ENTER SHORT"
  is_trade: boolean;       // true = take the trade, false = stand aside
  grade: string | null;    // A/B/C if present
  main_reason: string;
  pros: string[];
  cons: string[];
  conclusion: string;
  lang: "en" | "ar";
}

export interface AccountState {
  equity: number;
  cash: number;
  open_positions: number;
  total_risk_amount: number;
  daily_realized_pnl: number;
  trading_paused: boolean;
}
