import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { AssetClass, AutoTradeView } from "../types";

/**
 * Per-pair AI auto-trader. Toggle it ON for the charted pair and, every ~15 min, the AI analyses it
 * and auto-opens a >=60% setup following the scenario's levels; the monitor rides it to TP/SL and it
 * re-enters after a short cooldown. PAPER-ONLY; every risk gate (3% cap, exposure, daily-loss,
 * kill-switch) still applies. Off by default.
 */
export function AutoTradePanel({
  symbol,
  assetClass,
  timeframe,
}: {
  symbol: string;
  assetClass: AssetClass;
  timeframe: string;
}) {
  const [cfg, setCfg] = useState<AutoTradeView | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setCfg(await api.autoTrade());
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(load, 15_000); // keep "last result" + pair list fresh
    return () => clearInterval(id);
  }, [load]);

  const on = !!cfg?.pairs?.some((p) => p.symbol.toUpperCase() === symbol.toUpperCase());
  const enabledPairs = cfg?.pairs ?? [];

  const toggle = async () => {
    setBusy(true);
    try {
      if (!on) {
        const ok = window.confirm(
          `Auto-trade ${symbol}?\n\nEvery ~${Math.round((cfg?.interval_seconds ?? 900) / 60)} min the AI ` +
            `will analyse ${symbol} and AUTO-OPEN a setup at ${((cfg?.min_confidence ?? 0.6) * 100).toFixed(0)}%+ ` +
            `confidence (paper), riding it to target/stop and re-entering after a ` +
            `${cfg?.cooldown_minutes ?? 5}-min cooldown.\n\nEvery risk gate + the kill-switch still apply. Proceed?`,
        );
        if (!ok) {
          setBusy(false);
          return;
        }
      }
      setCfg(await api.autoTradePair(symbol, assetClass, !on, timeframe));
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    try {
      await api.autoTradeRun();
      await load();
    } catch {
      /* no pairs / error — ignore */
    } finally {
      setBusy(false);
    }
  };

  const setNum = async (patch: Partial<Pick<AutoTradeView, "min_confidence" | "min_rr" | "min_profit_usd" | "cooldown_minutes" | "interval_seconds">>) => {
    setBusy(true);
    try {
      setCfg(await api.autoTradeConfig(patch));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-semibold text-neutral-200">🤖 Auto-trade — {symbol}</span>
        <span className="text-[10px] text-neutral-500">paper · AI · risk-gated</span>
      </div>

      <button
        onClick={toggle}
        disabled={busy}
        className={`w-full rounded py-2 text-sm font-semibold disabled:opacity-50 ${
          on ? "bg-bull text-white hover:bg-green-700" : "bg-neutral-800 text-neutral-200 hover:bg-neutral-700"
        }`}
        title="Let the AI auto-open/close trades on this pair, following the scenario levels"
      >
        {on ? `● Auto-trading ${symbol} — ON (tap to stop)` : `Auto-trade ${symbol} — OFF`}
      </button>

      {/* Params */}
      <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] uppercase text-neutral-500">
        <label>
          Min conf
          <select
            value={cfg?.min_confidence ?? 0.6}
            onChange={(e) => void setNum({ min_confidence: Number(e.target.value) })}
            className="mt-0.5 w-full rounded bg-neutral-800 px-1 py-1 text-xs text-neutral-100"
          >
            {[0.5, 0.55, 0.6, 0.65, 0.7].map((v) => (
              <option key={v} value={v}>
                {(v * 100).toFixed(0)}%
              </option>
            ))}
          </select>
        </label>
        <label>
          Min R:R
          <select
            value={cfg?.min_rr ?? 1.2}
            onChange={(e) => void setNum({ min_rr: Number(e.target.value) })}
            className="mt-0.5 w-full rounded bg-neutral-800 px-1 py-1 text-xs text-neutral-100"
            title="Reward:risk floor. Below 1.5 takes smaller-reward trades (weaker expectancy)."
          >
            {[1.0, 1.2, 1.5, 2.0].map((v) => (
              <option key={v} value={v}>
                {v.toFixed(1)}R
              </option>
            ))}
          </select>
        </label>
        <label>
          Re-check
          <select
            value={cfg?.interval_seconds ?? 900}
            onChange={(e) => void setNum({ interval_seconds: Number(e.target.value) })}
            className="mt-0.5 w-full rounded bg-neutral-800 px-1 py-1 text-xs text-neutral-100"
          >
            {[
              [300, "5m"],
              [600, "10m"],
              [900, "15m"],
              [1800, "30m"],
            ].map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label>
          Cooldown
          <select
            value={cfg?.cooldown_minutes ?? 5}
            onChange={(e) => void setNum({ cooldown_minutes: Number(e.target.value) })}
            className="mt-0.5 w-full rounded bg-neutral-800 px-1 py-1 text-xs text-neutral-100"
          >
            {[2, 5, 10, 15].map((v) => (
              <option key={v} value={v}>
                {v}m
              </option>
            ))}
          </select>
        </label>
        <label>
          Min $ gain
          <select
            value={cfg?.min_profit_usd ?? 20}
            onChange={(e) => void setNum({ min_profit_usd: Number(e.target.value) })}
            className="mt-0.5 w-full rounded bg-neutral-800 px-1 py-1 text-xs text-neutral-100"
            title="Skip a setup unless there's at least this much $ to the next target — no trades too small to matter."
          >
            {[0, 10, 20, 30, 50].map((v) => (
              <option key={v} value={v}>
                {v === 0 ? "off" : `$${v}`}
              </option>
            ))}
          </select>
        </label>
      </div>

      {enabledPairs.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-[11px]">
          <span className="text-neutral-500">Auto-trading:</span>
          {enabledPairs.map((p) => (
            <span
              key={p.symbol}
              className={`rounded px-1.5 py-0.5 ${
                p.symbol.toUpperCase() === symbol.toUpperCase() ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-300"
              }`}
            >
              {p.symbol}
            </span>
          ))}
          <button onClick={runNow} disabled={busy} className="ml-auto text-[11px] text-brand-400 hover:text-brand-300">
            Run now ↻
          </button>
        </div>
      )}

      {cfg?.last_result && (
        <div className="mt-1.5 text-[11px] text-neutral-500">Last pass: {cfg.last_result}</div>
      )}
      <div className="mt-1.5 text-[10px] text-neutral-600">
        Auto-opens paper trades on the interval at ≥{((cfg?.min_confidence ?? 0.6) * 100).toFixed(0)}% following the
        AI's scenario levels; rides to TP/SL, re-enters after the cooldown. Every risk gate + the kill-switch apply.
      </div>
    </div>
  );
}
