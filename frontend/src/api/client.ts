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

  ohlcv: (symbol: string, assetClass: AssetClass, timeframe: string, limit = 200) =>
    request<OHLCVSeries>(
      `/api/market/ohlcv?symbol=${encodeURIComponent(symbol)}&asset_class=${assetClass}&timeframe=${timeframe}&limit=${limit}`,
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
  setScalpMode: (enabled: boolean) =>
    request<SettingsResponse>("/api/settings/scalp-mode", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  setAiLedMode: (enabled: boolean) =>
    request<SettingsResponse>("/api/settings/ai-led-mode", {
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
  opportunities: () =>
    request<import("../types").OpportunityView[]>("/api/watchlist/opportunities"),

  hybridState: () => request<import("../types").HybridState>("/api/hybrid"),
  setHybridConfig: (cfg: {
    enabled?: boolean; interval_seconds?: number; min_confidence?: number;
    conditional_enabled?: boolean; max_armed?: number;
  }) =>
    request<import("../types").HybridState>("/api/hybrid/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    }),
  hybridRun: () => request<import("../types").HybridState>("/api/hybrid/run", { method: "POST" }),

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
  reflect: () => request<ReflectionReport>("/api/journal/reflect", { method: "POST" }),
  reflectionLatest: () => request<ReflectionReport | null>("/api/journal/reflection/latest"),
  journalCalibration: () => request<CalibrationBucket[]>("/api/journal/calibration"),
  journalStats: () => request<import("../types").JournalStats>("/api/journal/stats"),
};
