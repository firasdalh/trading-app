import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useLocalStorage } from "../hooks/useLocalStorage";
import type { RsiOverCandidate, RsiOverConfig, RsiOverScanResult } from "../types";

const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"];

// A checkbox toggle with consistent sizing + colour, so the control row reads cleanly.
function Toggle({ checked, onChange, label, title, tone = "neutral" }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  title?: string;
  tone?: "neutral" | "green" | "amber";
}) {
  const color = tone === "green" ? (checked ? "text-emerald-300" : "text-neutral-400")
    : tone === "amber" ? (checked ? "text-amber-300" : "text-neutral-400")
    : (checked ? "text-neutral-100" : "text-neutral-400");
  return (
    <label className={`flex cursor-pointer select-none items-center gap-1.5 text-xs ${color}`} title={title}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
             className="h-3.5 w-3.5 accent-violet-500" />
      {label}
    </label>
  );
}

// A column of clickable near-extreme pairs. Clicking loads the pair on the chart to watch/trade it.
function Candidates({ label, tone, items, onSelect }: {
  label: string;
  tone: "bull" | "bear";
  items?: RsiOverCandidate[];
  onSelect?: (p: { symbol: string; asset_class: string }) => void;
}) {
  const list = items ?? [];
  return (
    <div className="flex-1">
      <div className={`mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide ${tone === "bull" ? "text-bull" : "text-bear"}`}>
        {label}
        <span className="rounded bg-neutral-800 px-1 text-[10px] font-normal text-neutral-400">{list.length}</span>
      </div>
      {list.length === 0 ? (
        <div className="text-xs text-neutral-600">—</div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {list.map((c) => (
            <button
              key={c.symbol}
              onClick={() => onSelect?.({ symbol: c.symbol, asset_class: c.asset_class })}
              title={c.extreme ? "RSI is in the extreme zone — waiting for the turn to confirm. Click to open on the chart." : "Click to open on the chart"}
              className={`rounded-md border px-2 py-1 text-xs tabular-nums transition hover:bg-neutral-800 ${
                c.extreme ? "border-amber-500/60 text-amber-300" : "border-neutral-700 text-neutral-200"
              }`}
            >
              {c.symbol} <span className="ml-0.5 text-neutral-500">{c.rsi.toFixed(0)}</span>
            </button>
          ))}
        </div>
      )}
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
  const [rsiDiv, setRsiDiv] = useLocalStorage("rsiover.rsiDiv", false);
  const [trendFilter, setTrendFilter] = useLocalStorage("rsiover.trendFilter", true);
  const [autoApprove, setAutoApprove] = useLocalStorage("rsiover.autoApprove", false);
  const [everyMin, setEveryMin] = useLocalStorage("rsiover.everyMin", 15);  // auto-watch interval (minutes)
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
  const changeEvery = (min: number) => { setEveryMin(min); syncWatch({ interval_seconds: min * 60 }); };
  const changeRsiDiv = (v: boolean) => { setRsiDiv(v); syncWatch({ rsi_div: v }); };
  const changeTrendFilter = (v: boolean) => { setTrendFilter(v); syncWatch({ trend_filter: v }); };
  const changeAutoApprove = (v: boolean) => { setAutoApprove(v); syncWatch({ auto_approve: v }); };

  const toggleWatch = async () => {
    const next = !(watch?.enabled ?? false);
    try {
      // Watch with the panel's current TF + confirmation, every 15 min.
      const cfg = await api.setRsiOverConfig(
        next
          ? { enabled: true, timeframe: tf, confirm, macd, rsi_div: rsiDiv, trend_filter: trendFilter,
              auto_approve: autoApprove, interval_seconds: everyMin * 60 }
          : { enabled: false },
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
      const res = await api.rsiOverScan(tf, { confirm, macd, rsiDiv, trendFilter, autoApprove });
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
      {/* title */}
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="whitespace-nowrap text-sm font-semibold">📉 RSI-Over</span>
        <span className="text-xs text-neutral-500">overbought ≥72 → short · oversold ≤28 → long</span>
      </div>

      {/* controls — grouped so they read cleanly and wrap gracefully */}
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500">Confirm by</span>
          <div className="flex items-center gap-3 rounded-md border border-neutral-800 bg-neutral-900/40 px-2.5 py-1.5">
            <Toggle checked={confirm} onChange={changeConfirm} label="EMA10"
                    title="Strong confirmation: wait for a close through EMA10 (later, safer)." />
            <Toggle checked={macd} onChange={changeMacd} label="MACD"
                    title="Early entry: a MACD signal-line cross or divergence gets you into the pullback sooner." />
            <Toggle checked={rsiDiv} onChange={changeRsiDiv} label="RSI div"
                    title="Also accept an RSI divergence (price new extreme, RSI doesn't) — the exhaustion tell." />
          </div>
        </div>

        <Toggle checked={trendFilter} onChange={changeTrendFilter} label="Trend filter" tone="green"
                title="Don't fade AGAINST a clear higher-timeframe trend — protects against fading a runaway. Recommended ON." />

        <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase text-neutral-500">TF</span>
            <select value={tf} onChange={(e) => changeTf(e.target.value)}
                    title="Timeframe the sweep analyses every pair on"
                    className="rounded bg-neutral-800 px-2 py-1 text-xs text-neutral-100">
              {TIMEFRAMES.map((t) => (<option key={t} value={t}>{t}</option>))}
            </select>
            <button onClick={scan} disabled={busy}
                    className="btn bg-violet-600 text-white hover:bg-violet-500 disabled:opacity-60">
              {busy ? "Scanning…" : "Scan all pairs"}
            </button>
          </div>

          <div className="flex items-center gap-2 rounded-md border border-neutral-800 bg-neutral-900/40 px-2.5 py-1.5">
            <Toggle checked={autoApprove} onChange={changeAutoApprove} label="Auto-approve" tone="amber"
                    title="Open a found pair DIRECTLY without your click. Every gate still applies (3% cap, kill-switch, live-confirmation, anti-stacking, drift guard)." />
            <span className="text-[10px] uppercase text-neutral-500">every</span>
            <select value={everyMin} onChange={(e) => changeEvery(Number(e.target.value))} title="Auto-watch interval"
                    className="rounded bg-neutral-800 px-2 py-1 text-xs text-neutral-100">
              {[1, 2, 5, 10, 15, 30, 60].map((m) => (<option key={m} value={m}>{m < 60 ? `${m}m` : "1h"}</option>))}
            </select>
            <button onClick={toggleWatch}
                    title="Auto-watch: re-run this sweep on the interval and act on the first pair that confirms."
                    className={`btn ${watch?.enabled ? "bg-emerald-600 text-white hover:bg-emerald-500" : "bg-neutral-700 text-neutral-200 hover:bg-neutral-600"}`}>
              {watch?.enabled ? "Auto-watch ON" : "Auto-watch"}
            </button>
          </div>
        </div>
      </div>
      {watch?.enabled && (
        <div className={`mb-2 rounded border px-2 py-1 text-xs ${watch.auto_approve
          ? "border-amber-600/50 bg-amber-900/15 text-amber-300"
          : "border-emerald-700/40 bg-emerald-900/15 text-emerald-300"}`}>
          👀 Auto-watching every {Math.round(watch.interval_seconds / 60)} min on {watch.timeframe}
          {" ("}{[watch.confirm && "EMA10", watch.macd && "MACD"].filter(Boolean).join(" or ") || "extreme only"}{")"}
          {watch.auto_approve
            ? " — ⚡ AUTO-APPROVE ON: a confirmed pair is OPENED automatically (all gates still apply)."
            : " — a proposal appears in Pending approval when a pair confirms."}
          {watch.last_result && <span className="opacity-70"> · last: {watch.last_result}</span>}
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
          <div className="space-y-2.5">
            <div className="text-xs text-neutral-500">
              No confirmed setup · scanned {result.scanned}. Closest to the extremes — click a pair to open it on the chart:
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Candidates label="Overbought → short" tone="bear" items={result.candidates?.overbought} onSelect={onSelect} />
              <Candidates label="Oversold → long" tone="bull" items={result.candidates?.oversold} onSelect={onSelect} />
            </div>
          </div>
        )
      )}
    </div>
  );
}
