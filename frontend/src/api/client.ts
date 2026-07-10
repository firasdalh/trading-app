// Tiny typed fetch wrapper. Uses relative paths (Vite proxies to the backend in dev).
import type {
  AccountState,
  AdvisorState,
  AnalyzeResponse,
  AssetClass,
  BacktestResult,
  ExecutionMode,
  OHLCVSeries,
  PositionAdvice,
  PositionView,
  LlmStatus,
  Mt5Status,
  ProposalView,
  WatchlistResponse,
  ReflectionReport,
  CalibrationBucket,
  ExplainedReview,
  RiskState,
  SettingsResponse,
} from "../types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  settings: () => request<SettingsResponse>("/api/settings"),
  riskState: () => request<RiskState>("/api/risk/state"),
  resumeTrading: () => request<RiskState>("/api/risk/resume", { method: "POST" }),

  ohlcv: (symbol: string, assetClass: AssetClass, timeframe: string, limit = 200) =>
    request<OHLCVSeries>(
      `/api/market/ohlcv?symbol=${encodeURIComponent(symbol)}&asset_class=${assetClass}&timeframe=${timeframe}&limit=${limit}`,
    ),

  // Plain-language "where is price + do RSI/volume/ATR confirm?" read for the current pair.
  context: (symbol: string, assetClass: AssetClass) =>
    request<import("../types").MarketContext>(
      `/api/market/context?symbol=${encodeURIComponent(symbol)}&asset_class=${assetClass}`,
    ),

  // AI two-scenario read (ranked + scored + why-primary). INFO only. Falls back to deterministic.
  scenarios: (symbol: string, assetClass: AssetClass) =>
    request<import("../types").AiScenarioRead>(
      `/api/market/scenarios?symbol=${encodeURIComponent(symbol)}&asset_class=${assetClass}`,
    ),

  // Shadow scorecard — AI vs deterministic head-to-head (auto-grades pending on GET).
  shadowScorecard: () => request<import("../types").ShadowScorecard>("/api/shadow/scorecard"),

  // Multi-timeframe support/resistance levels (1h/4h/1d) for the chart overlay.
  levels: (symbol: string, assetClass: AssetClass) =>
    request<{ symbol: string; price: number | null; levels: Record<string, { price: number; kind: string }[]> }>(
      `/api/market/levels?symbol=${encodeURIComponent(symbol)}&asset_class=${assetClass}`,
    ),

  account: (assetClass: AssetClass) =>
    request<AccountState>(`/api/broker/account?asset_class=${assetClass}`),
  // App-opened positions (used internally for exposure).
  positions: () => request<PositionView[]>("/api/positions"),
  // The broker's real open positions (includes trades opened directly in MT5/Exness).
  livePositions: () => request<PositionView[]>("/api/positions/live"),
  // Management guidance for open positions (protect winners / cut losers around news).
  positionAdvice: () => request<PositionAdvice[]>("/api/positions/advice"),
  // Advisor auto-watch config + current advisories (one read for the panel).
  advisorState: () => request<AdvisorState>("/api/positions/advisor"),
  advisorRun: () => request<AdvisorState>("/api/positions/advisor/run", { method: "POST" }),
  advisorActivity: () =>
    request<import("../types").AdvisorActivityItem[]>("/api/positions/advisor/activity"),
  advisorConfig: (cfg: {
    enabled?: boolean;
    auto_execute?: boolean;
    auto_reenter?: boolean;
    interval_seconds?: number;
  }) =>
    request<AdvisorState>("/api/positions/advisor/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    }),
  closePosition: (id: number) =>
    request<{ closed: boolean; symbol: string; realized_pnl: number }>(
      `/api/positions/${id}/close`,
      { method: "POST" },
    ),
  liveClose: (symbol: string, assetClass: AssetClass) =>
    request<{ status: string; symbol: string }>("/api/positions/live/close", {
      method: "POST",
      body: JSON.stringify({ symbol, asset_class: assetClass }),
    }),
  setSlTp: (symbol: string, assetClass: AssetClass, stopLoss: number | null, takeProfit: number | null) =>
    request<{ status: string }>("/api/positions/sl-tp", {
      method: "POST",
      body: JSON.stringify({ symbol, asset_class: assetClass, stop_loss: stopLoss, take_profit: takeProfit }),
    }),
  brokerInfo: (assetClass: AssetClass) =>
    request<{ name: string; is_paper: boolean }>(`/api/broker/info?asset_class=${assetClass}`),
  symbols: (assetClass: AssetClass) =>
    request<{ broker: string; asset_class: string; symbols: string[]; descriptions: Record<string, string> }>(
      `/api/market/symbols?asset_class=${assetClass}`,
    ),

  analyze: (symbol: string, assetClass: AssetClass, timeframe: string) =>
    request<AnalyzeResponse>("/api/proposals/analyze", {
      method: "POST",
      body: JSON.stringify({ symbol, asset_class: assetClass, timeframe }),
    }),
  // Manual quick trade — always sized + gated by the deterministic Risk Manager.
  manualTrade: (body: {
    symbol: string;
    asset_class: AssetClass;
    direction: "long" | "short";
    stop_loss: number;
    take_profit?: number | null;
    entry?: number | null;
    lots?: number | null;
    execute?: boolean;
  }) => request<AnalyzeResponse>("/api/proposals/manual", { method: "POST", body: JSON.stringify(body) }),
  // Risk-size a manual ticket WITHOUT placing it — returns the max lots at the 3% cap + $ risk.
  manualPreview: (body: {
    symbol: string;
    asset_class: AssetClass;
    direction: "long" | "short";
    stop_loss: number;
  }) =>
    request<{ entry: number; approved: boolean; max_lots: number; risk_amount: number; reason: string }>(
      "/api/proposals/manual/preview",
      { method: "POST", body: JSON.stringify(body) },
    ),
  explainReview: (text: string, lang: "en" | "ar") =>
    request<ExplainedReview>("/api/proposals/explain", {
      method: "POST",
      body: JSON.stringify({ text, lang }),
    }),
  proposals: (opts: { symbol?: string; timeframe?: string; status?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.symbol) q.set("symbol", opts.symbol);
    if (opts.timeframe) q.set("timeframe", opts.timeframe);
    if (opts.status) q.set("status", opts.status);
    q.set("limit", String(opts.limit ?? 50));
    return request<ProposalView[]>(`/api/proposals?${q.toString()}`);
  },
  sizePreview: (id: number, lots: number | null) =>
    request<import("../types").SizePreviewResponse>(`/api/proposals/${id}/size-preview`, {
      method: "POST",
      body: JSON.stringify({ lots }),
    }),
  approve: (id: number, lots?: number | null) =>
    request<ProposalView>(`/api/proposals/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ lots: lots ?? null }),
    }),
  reject: (id: number) =>
    request<ProposalView>(`/api/proposals/${id}/reject`, { method: "POST" }),

  setKillSwitch: (engage: boolean) =>
    request<{ effective: boolean; kill_switch_engaged: boolean; env_kill_switch: boolean }>(
      `/api/kill-switch/${engage}`,
      { method: "POST" },
    ),

  setMode: (mode: ExecutionMode, confirmPhrase?: string) =>
    request<SettingsResponse>("/api/settings/mode", {
      method: "POST",
      body: JSON.stringify({ mode, confirm_phrase: confirmPhrase }),
    }),
  setBrokerEnv: (env: "paper" | "live", confirmPhrase?: string) =>
    request<SettingsResponse>("/api/settings/broker-env", {
      method: "POST",
      body: JSON.stringify({ env, confirm_phrase: confirmPhrase }),
    }),
  liveConfirm: (confirmPhrase: string) =>
    request<SettingsResponse>("/api/settings/live-confirm", {
      method: "POST",
      body: JSON.stringify({ confirm_phrase: confirmPhrase }),
    }),
  updateRisk: (patch: Record<string, number | boolean>) =>
    request<SettingsResponse>("/api/settings/risk", {
      method: "POST",
      body: JSON.stringify(patch),
    }),
  setTrendOnly: (enabled: boolean) =>
    request<SettingsResponse>("/api/settings/trend-only", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  setStBandMode: (enabled: boolean) =>
    request<SettingsResponse>("/api/settings/st-band-mode", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  setAiReview: (enabled: boolean) =>
    request<SettingsResponse>("/api/settings/ai-review", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  setBrokerMap: (brokerMap: Record<string, string>) =>
    request<SettingsResponse>("/api/settings/broker-map", {
      method: "POST",
      body: JSON.stringify({ broker_map: brokerMap }),
    }),
  watchlist: () => request<WatchlistResponse>("/api/watchlist"),
  addWatch: (symbol: string, assetClass: AssetClass, timeframe: string) =>
    request<WatchlistResponse>("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol, asset_class: assetClass, timeframe }),
    }),
  removeWatch: (id: number) =>
    request<WatchlistResponse>(`/api/watchlist/${id}`, { method: "DELETE" }),
  setScanConfig: (cfg: { enabled?: boolean; interval_seconds?: number }) =>
    request<WatchlistResponse>("/api/watchlist/scan-config", {
      method: "POST",
      body: JSON.stringify(cfg),
    }),
  scanNow: () => request<{ scanned: number }>("/api/watchlist/scan-now", { method: "POST" }),
  opportunities: (timeframe?: string) =>
    request<import("../types").OpportunityView[]>(
      `/api/watchlist/opportunities${timeframe ? `?timeframe=${encodeURIComponent(timeframe)}` : ""}`,
    ),

  hybridState: () => request<import("../types").HybridState>("/api/hybrid"),
  setHybridConfig: (cfg: {
    enabled?: boolean; interval_seconds?: number; min_confidence?: number;
    conditional_enabled?: boolean; max_armed?: number;
  }) =>
    request<import("../types").HybridState>("/api/hybrid/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    }),
  hybridRun: (timeframe?: string) =>
    request<import("../types").HybridState>(
      `/api/hybrid/run${timeframe ? `?timeframe=${encodeURIComponent(timeframe)}` : ""}`,
      { method: "POST" },
    ),
  hybridStats: () => request<import("../types").HybridStats>("/api/hybrid/stats"),

  // Conditional ('armed' / pending) setups.
  conditionals: () =>
    request<import("../types").ConditionalSetupView[]>("/api/conditionals"),
  armConditional: (body: {
    symbol: string; asset_class: string; timeframe: string; direction: string;
    order_type: string; trigger_price: number; stop_loss?: number | null;
    take_profit?: number | null; confidence?: number; rr?: number | null; reason?: string;
  }) =>
    request<import("../types").ConditionalSetupView>("/api/conditionals", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  cancelConditional: (id: number) =>
    request<{ cancelled: boolean }>(`/api/conditionals/${id}`, { method: "DELETE" }),
  clearFinishedConditionals: () =>
    request<{ cleared: number }>("/api/conditionals/finished", { method: "DELETE" }),
  conditionalSizePreview: (id: number, lots: number | null) =>
    request<import("../types").SizePreviewResponse>(`/api/conditionals/${id}/size-preview`, {
      method: "POST",
      body: JSON.stringify({ lots }),
    }),
  setConditionalLots: (id: number, lots: number | null) =>
    request<import("../types").ConditionalSetupView>(`/api/conditionals/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ lots }),
    }),
  setConditionalLevels: (
    id: number,
    levels: { trigger_price?: number; stop_loss?: number; take_profit?: number },
  ) =>
    request<import("../types").ConditionalSetupView>(`/api/conditionals/${id}/levels`, {
      method: "PATCH",
      body: JSON.stringify(levels),
    }),

  llmStatus: () => request<LlmStatus>("/api/settings/llm"),
  setLlm: (body: { provider: string; model?: string; api_key?: string }) =>
    request<LlmStatus>("/api/settings/llm", { method: "POST", body: JSON.stringify(body) }),

  mt5Status: () => request<Mt5Status>("/api/settings/mt5/status"),
  connectMt5: (body: { login?: number; password?: string; server?: string; path?: string }) =>
    request<Mt5Status>("/api/settings/mt5", { method: "POST", body: JSON.stringify(body) }),
  flatten: () => request<{ closed: number }>("/api/execution/flatten", { method: "POST" }),

  backtest: (symbol: string, assetClass: AssetClass, timeframe: string, bars: number, startingEquity: number) =>
    request<BacktestResult>("/api/backtest", {
      method: "POST",
      body: JSON.stringify({
        symbol,
        asset_class: assetClass,
        timeframe,
        bars,
        starting_equity: startingEquity,
      }),
    }),

  journalTrades: (limit = 100) => request<PositionView[]>(`/api/journal/trades?limit=${limit}`),
  resetJournal: () =>
    request<{ journal_reset_at: string | null }>("/api/journal/reset", { method: "POST" }),
  restoreJournal: () =>
    request<{ journal_reset_at: string | null }>("/api/journal/reset", { method: "DELETE" }),
  reflect: () => request<ReflectionReport>("/api/journal/reflect", { method: "POST" }),
  reflectionLatest: () => request<ReflectionReport | null>("/api/journal/reflection/latest"),
  journalCalibration: () => request<CalibrationBucket[]>("/api/journal/calibration"),
  journalStats: () => request<import("../types").JournalStats>("/api/journal/stats"),
  journalBreakdown: () => request<import("../types").JournalBreakdown>("/api/journal/breakdown"),
  detFilters: () => request<import("../types").DetFiltersView>("/api/settings/det-filters"),
  setDetFilters: (disabled: string[]) =>
    request<import("../types").DetFiltersView>("/api/settings/det-filters", {
      method: "POST",
      body: JSON.stringify({ disabled }),
    }),

  // RSI-Over strategy: sweep all available pairs on `timeframe` for the first RSI-extreme reversal
  // (EMA10-confirmed when `confirm`) and stage it (Mode A queues it). "" timeframe -> default 1h.
  rsiOverScan: (
    timeframe?: string,
    opts: { confirm?: boolean; macd?: boolean; rsiDiv?: boolean; trendFilter?: boolean; autoApprove?: boolean } = {},
  ) =>
    request<import("../types").RsiOverScanResult>("/api/rsi-over/scan", {
      method: "POST",
      body: JSON.stringify({
        timeframe: timeframe || null,
        confirm: opts.confirm ?? true,
        macd: opts.macd ?? false,
        rsi_div: opts.rsiDiv ?? false,
        trend_filter: opts.trendFilter ?? true,
        auto_approve: opts.autoApprove ?? false,
      }),
    }),
  rsiOverConfig: () => request<import("../types").RsiOverConfig>("/api/rsi-over/config"),
  setRsiOverConfig: (cfg: Partial<import("../types").RsiOverConfig>) =>
    request<import("../types").RsiOverConfig>("/api/rsi-over/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    }),
};
