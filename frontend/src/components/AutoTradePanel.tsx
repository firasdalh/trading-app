import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { useLocalStorage } from "../hooks/useLocalStorage";
import type { AssetClass, AutoTradeResult, AutoTradeView } from "../types";
import { ago, localTime } from "./advisorFormat";

// One line per pair: what happened last pass + why (green if it opened, muted if it stood aside).
function resultRow(r: AutoTradeResult) {
  const opened = !!r.opened;
  const outcome = r.opened
    ? `opened ${r.opened.toUpperCase()}${r.confidence != null ? ` @ ${Math.round(r.confidence * 100)}%` : ""}`
    : r.blocked ? "blocked" : r.error ? "error" : "no trade";
  const reason = r.note || r.skipped || r.blocked || r.error || "";
  return (
    <div key={r.symbol} className="flex items-baseline gap-1.5 text-[11px]">
      <span className={`font-semibold ${opened ? "text-bull" : "text-neutral-400"}`}>{r.symbol}</span>
      <span className={opened ? "text-bull" : "text-neutral-500"}>{outcome}</span>
      {reason && <span className="text-neutral-600">— {reason}</span>}
    </div>
  );
}

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
  onSelect,
}: {
  symbol: string;
  assetClass: AssetClass;
  timeframe: string;
  onSelect?: (p: { symbol: string; asset_class: string }) => void;
}) {
  const [cfg, setCfg] = useState<AutoTradeView | null>(null);
  const [maxPos, setMaxPos] = useState(3);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setCfg(await api.autoTrade());
    } catch {
      /* ignore */
    }
    try {
      const s = await api.settings();
      setMaxPos(s.risk.max_open_positions);
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

  const setNum = async (patch: Partial<Pick<AutoTradeView, "min_confidence" | "min_rr" | "min_profit_usd" | "cooldown_minutes" | "interval_seconds" | "strategy" | "timeframe">>) => {
    setBusy(true);
    try {
      setCfg(await api.autoTradeConfig(patch));
    } finally {
      setBusy(false);
    }
  };

  const [open, setOpen] = useLocalStorage("autotrade.open", false);
  const strategy = cfg?.strategy ?? "scenario";
  const superTrend = strategy === "supertrend";
  const reversal = strategy === "reversal";

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 p-3">
      {/* Collapsible: this panel carries a lot of controls (strategy, timeframe, per-pair list,
          last-run results) that you set once and rarely revisit. Collapsed it still reports the two
          things that matter at a glance — whether THIS pair is armed, and how many pairs are on. */}
      <button
        onClick={() => setOpen((o) => !o)}
        className="mb-2 flex w-full items-center gap-2 text-left"
        title={open ? "Collapse auto-trade" : "Expand auto-trade"}
      >
        <span className="text-xs text-neutral-500">{open ? "▾" : "▸"}</span>
        <span className="text-sm font-semibold text-neutral-200">🤖 Auto-trade — {symbol}</span>
        {on && (
          <span className="rounded bg-bull/20 px-1.5 py-0.5 text-[10px] font-bold uppercase text-bull">
            on
          </span>
        )}
        {!open && enabledPairs.length > 0 && (
          <span className="text-[10px] text-neutral-500">
            {enabledPairs.length} pair{enabledPairs.length === 1 ? "" : "s"} active
          </span>
        )}
        <span className="ml-auto text-[10px] text-neutral-500">paper · AI · risk-gated</span>
      </button>

      {!open ? null : (<>
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

      {/* Strategy + timeframe — global, applied to ALL auto-traded pairs */}
      <div className="mt-2 rounded-md border border-neutral-800 bg-neutral-900/40 px-2.5 py-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[10px] uppercase text-neutral-500">Strategy</span>
          <select
            value={strategy}
            onChange={(e) => void setNum({ strategy: e.target.value as "scenario" | "supertrend" | "reversal" })}
            disabled={busy}
            className="rounded bg-neutral-800 px-1.5 py-1 text-xs font-semibold text-neutral-100"
            title="Which engine opens the trades. Applies to every auto-traded pair."
          >
            <option value="reversal">🔄 Level bounce (mechanical scalp)</option>
            <option value="supertrend">📈 SuperTrend (mechanical, no AI)</option>
            <option value="scenario">🤖 AI scenario (follows the AI read)</option>
          </select>
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2">
          <span className="text-[10px] uppercase text-neutral-500">Timeframe</span>
          <select
            value={cfg?.timeframe ?? "1h"}
            onChange={(e) => void setNum({ timeframe: e.target.value })}
            disabled={busy}
            className="rounded bg-neutral-800 px-1.5 py-1 text-xs font-semibold text-neutral-100"
            title="The chart timeframe the auto-trader analyses on — one value for ALL auto-traded pairs."
          >
            {["5m", "15m", "30m", "1h", "4h", "1d"].map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </div>
        <div className="mt-1 text-[10px] text-neutral-600">
          {reversal
            ? "Mechanical scalp (no LLM): when price rejects an S/R level it takes the quick move to the opposite level — sells a resistance rejection to support, buys a support rejection to resistance. Buys dips in uptrends / sells rallies in downtrends; won't fade a strong trend."
            : superTrend
              ? "Mechanical: opens ONLY a fresh SuperTrend + EMA20-band breakout (no LLM = no tokens). No fresh break → it waits. Best on 1h."
              : "AI-driven: opens the AI decider's trade or its primary scenario's next move at market."}
        </div>
      </div>

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
            <button
              key={p.symbol}
              onClick={() => onSelect?.({ symbol: p.symbol, asset_class: p.asset_class })}
              title={`Load ${p.symbol} on the chart`}
              className={`rounded px-1.5 py-0.5 hover:ring-1 hover:ring-neutral-500 ${
                p.symbol.toUpperCase() === symbol.toUpperCase() ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-300"
              }`}
            >
              {p.symbol}
            </button>
          ))}
          <button onClick={runNow} disabled={busy} className="ml-auto text-[11px] text-brand-400 hover:text-brand-300">
            Run now ↻
          </button>
        </div>
      )}

      {(cfg?.last_run_at || cfg?.last_result) && (
        <div className="mt-2 rounded-md border border-neutral-800 bg-neutral-900/40 px-2.5 py-1.5">
          <div className="flex items-baseline justify-between text-[11px]">
            <span className="font-medium text-neutral-400">Last check</span>
            {cfg?.last_run_at && (
              <span className="text-neutral-500" title={localTime(cfg.last_run_at)}>{ago(cfg.last_run_at)}</span>
            )}
          </div>
          {cfg?.last_results && cfg.last_results.length > 0 ? (
            <div className="mt-1 space-y-0.5">{cfg.last_results.map(resultRow)}</div>
          ) : (
            cfg?.last_result && <div className="mt-0.5 text-[11px] text-neutral-500">{cfg.last_result}</div>
          )}
        </div>
      )}
      <div className="mt-1.5 text-[10px] text-neutral-600">
        {reversal
          ? "Scalps a level rejection at market (never a pending order) — a quick move to the opposite level, riding to TP/SL, then re-entering after the cooldown. "
          : superTrend
            ? "Opens a fresh SuperTrend + EMA20-band breakout at market (never a pending order), riding to TP/SL, then re-entering after the cooldown. "
            : `Opens the AI's next scenario move at market (never a pending order) at ≥${((cfg?.min_confidence ?? 0.6) * 100).toFixed(0)}% — a quick win, riding to TP/SL, then re-entering after the cooldown. `}
        Won't fire once {maxPos} position{maxPos === 1 ? " is" : "s are"} open. Every risk gate + the kill-switch apply.
      </div>
      </>)}
    </div>
  );
}
