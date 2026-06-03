import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AdvisorState, PositionAdvice } from "../types";

interface Props {
  // Bump to force a refresh (e.g. after a position is closed).
  refreshSignal?: number;
}

const TONE: Record<PositionAdvice["severity"], { box: string; chip: string; label: string }> = {
  danger: { box: "border-bear/50 bg-bear/10", chip: "bg-bear/20 text-bear", label: "Act now" },
  warn: { box: "border-warn/40 bg-warn/10", chip: "bg-warn/20 text-warn", label: "Decide" },
  info: { box: "border-neutral-800 bg-neutral-900/40", chip: "bg-neutral-800 text-neutral-300", label: "OK" },
};

const THESIS: Record<PositionAdvice["thesis"], { text: string; cls: string }> = {
  intact: { text: "thesis intact", cls: "text-bull" },
  weakening: { text: "thesis weakening", cls: "text-warn" },
  invalidated: { text: "thesis broken", cls: "text-bear" },
  unknown: { text: "thesis n/a", cls: "text-neutral-500" },
};

function ago(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

// AI guidance for OPEN positions — is each trade still on track vs. its plan, protect winners /
// cut losers, especially around news. Run on demand, or auto-watch on a set interval.
// Advisory only: it tells you what to consider; you act via the positions table below.
export function PositionAdvicePanel({ refreshSignal }: Props) {
  const [state, setState] = useState<AdvisorState | null>(null);
  const [busy, setBusy] = useState(false);
  const [intervalInput, setIntervalInput] = useState("300");

  const load = useCallback(async (run: boolean) => {
    setBusy(true);
    try {
      const s = run ? await api.advisorRun() : await api.advisorState();
      setState(s);
      setIntervalInput(String(s.interval_seconds));
    } finally {
      setBusy(false);
    }
  }, []);

  // Initial load + refresh when the parent signals (e.g. a position closed).
  useEffect(() => {
    void load(false);
  }, [load, refreshSignal]);

  // Auto-watch: while enabled, re-run on the configured interval (front-of-house; the backend
  // scheduler runs it headless too).
  const enabled = state?.enabled ?? false;
  const intervalSecs = state?.interval_seconds ?? 300;
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => void loadRef.current(true), intervalSecs * 1000);
    return () => clearInterval(id);
  }, [enabled, intervalSecs]);

  const toggleAuto = async () => {
    setBusy(true);
    try {
      setState(await api.advisorConfig({ enabled: !enabled }));
    } finally {
      setBusy(false);
    }
  };

  const saveInterval = async () => {
    const secs = Math.min(3600, Math.max(30, Number(intervalInput) || 300));
    setBusy(true);
    try {
      setState(await api.advisorConfig({ interval_seconds: secs }));
    } finally {
      setBusy(false);
    }
  };

  const advice = state?.advice ?? [];
  const order = { danger: 0, warn: 1, info: 2 } as const;
  const sorted = [...advice].sort((a, b) => order[a.severity] - order[b.severity]);

  return (
    <div className="card">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">Position advisor</span>
        <span className="text-xs text-neutral-500">last check {ago(state?.last_run_at ?? null)}</span>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-neutral-400">
            every
            <input
              value={intervalInput}
              onChange={(e) => setIntervalInput(e.target.value)}
              onBlur={saveInterval}
              onKeyDown={(e) => e.key === "Enter" && saveInterval()}
              inputMode="numeric"
              className="w-14 rounded bg-neutral-800 px-1.5 py-1 text-center tabular-nums"
            />
            s
          </label>
          <button
            onClick={toggleAuto}
            disabled={busy}
            className={`btn text-xs ${
              enabled ? "bg-bull/20 text-bull hover:bg-bull/30" : "bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
            }`}
            title="Automatically re-check open positions on the interval"
          >
            Auto-watch {enabled ? "ON" : "OFF"}
          </button>
          <button
            onClick={() => load(true)}
            disabled={busy}
            className="btn bg-blue-600 text-white hover:bg-blue-500"
          >
            {busy ? "Checking…" : "Run now"}
          </button>
        </div>
      </div>

      {advice.length === 0 ? (
        <div className="text-sm text-neutral-500">
          No open positions to advise on. {state?.last_run_at ? "" : "Run a check to evaluate."}
        </div>
      ) : (
        <div className="space-y-2">
          {sorted.map((a) => {
            const tone = TONE[a.severity];
            const th = THESIS[a.thesis];
            return (
              <div key={`${a.symbol}-${a.direction}`} className={`rounded-md border px-3 py-2 ${tone.box}`}>
                <div className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${tone.chip}`}>
                    {tone.label}
                  </span>
                  <span className="text-sm font-medium">{a.headline}</span>
                  <span className={`text-[10px] font-semibold uppercase ${th.cls}`}>{th.text}</span>
                  <span
                    className={`ml-auto text-xs tabular-nums ${
                      a.unrealized_pnl >= 0 ? "text-bull" : "text-bear"
                    }`}
                  >
                    {a.unrealized_pnl >= 0 ? "+" : ""}
                    {a.unrealized_pnl.toFixed(2)}
                  </span>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-neutral-300">{a.detail}</p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
