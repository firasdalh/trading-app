import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useLocalStorage } from "../hooks/useLocalStorage";
import type { RsiOverCandidate, RsiOverConfig, RsiOverScanResult } from "../types";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"];

// A column of clickable near-extreme pairs. Clicking loads the pair on the chart to watch/trade it.
function Candidates({ label, tone, items, onSelect }: {
  label: string;
  tone: "bull" | "bear";
  items?: RsiOverCandidate[];
  onSelect?: (p: { symbol: string; asset_class: string }) => void;
}) {
  if (!items || items.length === 0) return null;
  return (
    <div className="flex-1">
      <div className={`mb-1 text-[10px] font-semibold uppercase ${tone === "bull" ? "text-bull" : "text-bear"}`}>
        {label}
      </div>
      <div className="flex flex-wrap gap-1">
        {items.map((c) => (
          <button
            key={c.symbol}
            onClick={() => onSelect?.({ symbol: c.symbol, asset_class: c.asset_class })}
            title={c.extreme ? "RSI is in the extreme zone — waiting for the turn to confirm (EMA10 / MACD). Click to watch on the chart." : "Click to watch on the chart"}
            className={`rounded border px-1.5 py-0.5 text-xs tabular-nums hover:bg-neutral-800 ${
              c.extreme ? "border-amber-500/60 text-amber-300" : "border-neutral-700 text-neutral-300"
            }`}
          >
            {c.symbol} <span className="text-neutral-500">{c.rsi.toFixed(0)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// RSI-Over strategy: one click sweeps ALL available pairs (forex / metals / energy / indices / crypto)
// on the chosen timeframe for the first RSI-extreme (>=72 / <=28) reversal confirmed by EMA10, and
// stages it (Mode A queues it for your approval). Overbought -> short, oversold -> long.
export function RsiOverPanel({ onStaged, onSelect }: {
  onStaged?: () => void;
  onSelect?: (p: { symbol: string; asset_class: string }) => void;  // load the pair on the chart
} = {}) {
  const [tf, setTf] = useLocalStorage("rsiover.timeframe", "1h");
  const [confirm, setConfirm] = useLocalStorage("rsiover.confirm", true);
  const [macd, setMacd] = useLocalStorage("rsiover.macd", false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RsiOverScanResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [watch, setWatch] = useState<RsiOverConfig | null>(null);

  const shownScanRef = useRef<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.rsiOverConfig().then((cfg) => {
      if (!alive) return;
      setWatch(cfg);  // keep the status line live (interval / confirmations / last result)
      // Restore/refresh the near-extreme list from the last persisted sweep (survives refresh, and
      // updates when auto-watch runs a newer sweep). Skip while a manual scan is in flight.
      if (cfg.last_candidates && cfg.last_scan_at !== shownScanRef.current && !busy) {
        shownScanRef.current = cfg.last_scan_at;
        setResult({ ran: true, reason: cfg.last_result ?? "", scanned: cfg.last_scanned,
                    signals: 0, found: null, candidates: cfg.last_candidates });
      }
    }).catch(() => {});
    load();
    const id = setInterval(load, 60000);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy]);

  // Changing a setting while auto-watch is ON should re-tune the running watch (and its status line),
  // not silently leave it on the settings it was started with.
  const syncWatch = (patch: Partial<RsiOverConfig>) => {
    if (watch?.enabled) api.setRsiOverConfig(patch).then(setWatch).catch(() => {});
  };
  const changeConfirm = (v: boolean) => { setConfirm(v); syncWatch({ confirm: v }); };
  const changeMacd = (v: boolean) => { setMacd(v); syncWatch({ macd: v }); };
  const changeTf = (v: string) => { setTf(v); syncWatch({ timeframe: v }); };

  const toggleWatch = async () => {
    const next = !(watch?.enabled ?? false);
    try {
      // Watch with the panel's current TF + confirmation, every 15 min.
      const cfg = await api.setRsiOverConfig(
        next ? { enabled: true, timeframe: tf, confirm, macd, interval_seconds: 900 } : { enabled: false },
      );
      setWatch(cfg);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const scan = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.rsiOverScan(tf, confirm, macd);
      setResult(res);
      if (res.found) onStaged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const f = result?.found;
  return (
    <div className="card">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-sm font-semibold">📉 RSI-Over</span>
        <span className="text-xs text-neutral-500">
          overbought ≥72 → short · oversold ≤28 → long · confirmed by EMA10 &/or MACD
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <label
            className="flex items-center gap-1 text-xs text-neutral-400"
            title="Strong confirmation: wait for a close through EMA10 (later, safer)."
          >
            <input type="checkbox" checked={confirm} onChange={(e) => changeConfirm(e.target.checked)} />
            EMA10
          </label>
          <label
            className="flex items-center gap-1 text-xs text-neutral-400"
            title="Early entry: also accept a MACD signal-line cross or divergence (gets you into the pullback sooner). With EMA10 also on, whichever confirms first fires."
          >
            <input type="checkbox" checked={macd} onChange={(e) => changeMacd(e.target.checked)} />
            MACD early
          </label>
          <label className="text-xs text-neutral-500" title="Timeframe the sweep analyses every pair on">
            TF
          </label>
          <select
            value={tf}
            onChange={(e) => changeTf(e.target.value)}
            className="rounded bg-neutral-800 px-1.5 py-1 text-xs text-neutral-100"
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <button onClick={scan} disabled={busy} className="btn bg-violet-600 text-white hover:bg-violet-500">
            {busy ? "Scanning…" : "Scan all pairs"}
          </button>
          <button
            onClick={toggleWatch}
            title="Auto-watch: re-run this sweep every 15 min and queue the first pair that confirms."
            className={`btn ${watch?.enabled ? "bg-emerald-600 text-white hover:bg-emerald-500" : "bg-neutral-700 text-neutral-200 hover:bg-neutral-600"}`}
          >
            {watch?.enabled ? "Auto-watch ON" : "Auto-watch"}
          </button>
        </div>
      </div>
      <p className="mb-2 text-xs text-neutral-500">
        Sweeps all available pairs until it finds one at an RSI extreme with the turn confirmed (EMA10
        close and/or a MACD cross/divergence), then risk-sizes it and queues it for your approval
        (Modes B/C auto-open). Same 3% cap + all gates.
      </p>
      {watch?.enabled && (
        <div className="mb-2 rounded border border-emerald-700/40 bg-emerald-900/15 px-2 py-1 text-xs text-emerald-300">
          👀 Auto-watching every {Math.round(watch.interval_seconds / 60)} min on {watch.timeframe}
          {" ("}{[watch.confirm && "EMA10", watch.macd && "MACD"].filter(Boolean).join(" or ") || "extreme only"}{")"}
          {" "}— a proposal appears in Pending approval when a pair confirms.
          {watch.last_result && <span className="text-emerald-400/70"> · last: {watch.last_result}</span>}
        </div>
      )}

      {error && (
        <div className="rounded border border-bear/40 bg-bear/10 px-2 py-1 text-xs text-bear">{error}</div>
      )}

      {result && !error && (
        f ? (
          <div className="rounded-md border border-violet-700/50 bg-violet-900/20 px-3 py-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold">{f.symbol}</span>
              <span className="text-xs text-neutral-500">{f.timeframe} · {f.asset_class}</span>
              <span className={`text-[10px] font-bold uppercase ${f.direction === "long" ? "text-bull" : "text-bear"}`}>
                {f.direction}
              </span>
              {f.rsi != null && (
                <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-300">
                  RSI {f.rsi.toFixed(0)}
                </span>
              )}
              <span className="ml-auto text-xs text-neutral-400">{f.status.replace("_", " ")}</span>
            </div>
            <div className="mt-1 text-xs text-neutral-400">
              {f.approved
                ? "Risk-sized and queued — approve it in Pending approval below."
                : "Found, but the Risk Manager did not approve it (see Pending approval / reason)."}
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="text-xs text-neutral-500">
              No confirmed setup · scanned {result.scanned}. Closest to the extremes (click to open on the chart):
            </div>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Candidates label="Overbought → short" tone="bear" items={result.candidates?.overbought} onSelect={onSelect} />
              <Candidates label="Oversold → long" tone="bull" items={result.candidates?.oversold} onSelect={onSelect} />
            </div>
          </div>
        )
      )}
    </div>
  );
}
