// Bar length of every timeframe the chart can show, in seconds.
//
// 5s, 10s and 30s are chart-only. MT5 has no bar under a minute (the backend builds these from the
// tick history), and everything that ANALYSES or TRADES - Run analysis, the price map, AI scenarios,
// order blocks, chart tickets, armed setups, auto-trade, watchlist adds - works on the bars of a
// timeframe the engine knows. From a seconds chart all of those run on 1m; analysisTimeframe() is
// the one place that says so.
export const TF_SECONDS: Record<string, number> = {
  "5s": 5, "10s": 10, "30s": 30,
  "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14_400, "1d": 86_400,
};

export const isSecondsTf = (tf: string): boolean => (TF_SECONDS[tf] ?? 60) < 60;

export const analysisTimeframe = (tf: string): string => (isSecondsTf(tf) ? "1m" : tf);
