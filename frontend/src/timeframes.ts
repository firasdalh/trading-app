// Bar length of every timeframe the chart can show, in seconds.
//
// 5s, 10s and 30s are chart-only. MT5 has no bar under a minute (the backend builds these from the
// tick history), and everything that ANALYSES or TRADES - Run analysis, the price map, AI scenarios,
// order blocks, chart tickets, armed setups, auto-trade, watchlist adds - works on the bars of a
// timeframe the engine knows. From a seconds chart all of those run on 1m; analysisTimeframe() is
// the one place that says so. 10m is MT5's own M10 bar, but the engine was never built or tested on
// it, so from a 10m chart they run on 5m. 3m (MT5's M3, added 2026-11-05) is the same: from a 3m chart
// they run on 1m.
export const TF_SECONDS: Record<string, number> = {
  "5s": 5, "10s": 10, "30s": 30,
  "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14_400, "1d": 86_400,
};

export const isSecondsTf = (tf: string): boolean => (TF_SECONDS[tf] ?? 60) < 60;

export const analysisTimeframe = (tf: string): string =>
  isSecondsTf(tf) || tf === "3m" ? "1m" : tf === "10m" ? "5m" : tf;

// The chart's timeframe switch (desk and chart windows). It drives BOTH the chart and the
// analysis/trade the engine produces. 1m, 5m and 15m are included so you can drop down to time an
// entry, but they are marked: measured over 959 signals, 15m lost -0.159R per trade in BOTH halves
// of the data (the clearest negative result on this book) because the win rate falls AND the fixed
// spread eats roughly twice as much of a smaller stop. 5m and 1m are faster still. 1h is the tested
// baseline. 5s/10s/30s, 3m and 10m are chart-only (above).
export const TIMEFRAMES = ["5s", "10s", "30s", "1m", "3m", "5m", "10m", "15m", "1h", "4h", "1d"];
export const FAST_TF = new Set(["1m", "5m", "15m"]);

// Scalp mode's switch: the fast charts a scalp is timed on, then the slower ones for the bigger
// picture around it. Entering scalp mode always lands on a fast one.
export const SCALP_FAST = ["5s", "10s", "30s", "1m", "3m", "5m", "10m"];
export const SCALP_TFS = [...SCALP_FAST, "15m", "1h", "4h", "1d"];
