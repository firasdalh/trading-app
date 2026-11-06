import { useEffect, useState } from "react";

// The RSI period the scalp chart shows (picked in its Indicators menu), shared with the scalp ticket so
// its RSI gauges read the same RSI. The chart stores it and announces each change; the ticket listens.
export const RSI_PERIOD_EVENT = "scalp-rsi-period";
const KEY = "scalp.chart.rsiPeriod";
const DEFAULT = 7;

const valid = (v: number) => (Number.isFinite(v) && v >= 2 && v <= 50 ? Math.round(v) : DEFAULT);

function stored(): number {
  try {
    return valid(Number(JSON.parse(localStorage.getItem(KEY) ?? String(DEFAULT))));
  } catch {
    return DEFAULT;
  }
}

export function useScalpRsiPeriod(): number {
  const [p, setP] = useState(stored);
  useEffect(() => {
    const on = (e: Event) => setP(valid(Number((e as CustomEvent<number>).detail)));
    window.addEventListener(RSI_PERIOD_EVENT, on);
    return () => window.removeEventListener(RSI_PERIOD_EVENT, on);
  }, []);
  return p;
}
