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
  // If the backend ever sends a timezone-less timestamp, treat it as UTC (not local time).
  const safe = /[Z+]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const secs = Math.max(0, Math.round((Date.now() - new Date(safe).getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

function actionText(a: { action: string; kind?: string | null; stop?: number | null }): string {
  if (a.action === "close" || a.kind === "close") return "closed position";
  if (a.action === "close_pending") return "close pending confirmation";
  const at = a.stop != null ? ` @ ${a.stop}` : "";
  if (a.kind === "protect") return `attached protective stop${at}`;
  if (a.kind === "breakeven") return `moved stop → breakeven${at}`;
  if (a.kind === "trail") return `trailed stop${at}`;
  return a.action;
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

  const autoExecute = state?.auto_execute ?? false;

  const toggleAuto = async () => {
    setBusy(true);
    try {
      setState(await api.advisorConfig({ enabled: !enabled }));
    } finally {
      setBusy(false);
    }
  };

  const toggleAutoExecute = async () => {
    // Turning ON means the advisor may close an invalidated trade / lock a winner's stop by itself.
    if (!autoExecute) {
      const ok = window.confirm(
        "Enable AUTO-EXECUTE?\n\nThe advisor will then act on its own on open positions:\n" +
          "• CLOSE a position when its thesis is invalidated (trend flipped against you)\n" +
          "• move a winning trade's stop to breakeven before high-impact news\n\n" +
          "It never opens, sizes up, or flips a trade, respects the kill switch, and needs live\n" +
          "confirmation for a live account. Proceed?",
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      setState(await api.advisorConfig({ auto_execute: !autoExecute }));
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
            onClick={toggleAutoExecute}
            disabled={busy}
            className={`btn text-xs ${
              autoExecute ? "bg-bear/20 text-bear hover:bg-bear/30" : "bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
            }`}
            title="Let the advisor act on its own: close an invalidated trade / lock a winner's stop to breakeven"
          >
            Auto-execute {autoExecute ? "ON" : "OFF"}
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

      {autoExecute && (
        <div className="mb-2 rounded border border-bear/40 bg-bear/10 px-2 py-1 text-[11px] text-bear">
          Auto-execute is ON — the advisor may close an invalidated trade or move a winner's stop
          to breakeven by itself. It never opens or sizes up, and respects the kill switch.
        </div>
      )}

      {(state?.actions?.length ?? 0) > 0 && (
        <div className="mb-2 space-y-1">
          {state!.actions.map((act, i) => (
            <div
              key={`${act.symbol}-${i}`}
              className={`rounded px-2 py-1 text-xs ${
                act.ok
                  ? "bg-bull/15 text-bull"
                  : act.action === "close_pending"
                    ? "bg-warn/15 text-warn"
                    : "bg-bear/15 text-bear"
              }`}
            >
              {act.ok ? "✓ Auto-executed" : act.action === "close_pending" ? "⏳ Pending" : "✗ Auto-execute blocked"}:{" "}
              {act.symbol} · {actionText(act)}
              {act.reason ? ` — ${act.reason}` : ""}
              {act.error ? ` (${act.error})` : ""}
            </div>
          ))}
        </div>
      )}

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
                  {a.r_multiple != null && (
                    <span
                      className={`rounded bg-neutral-800 px-1 py-0.5 text-[10px] tabular-nums ${
                        a.r_multiple >= 0 ? "text-bull" : "text-bear"
                      }`}
                      title="Progress in R (profit ÷ planned risk)"
                    >
                      {a.r_multiple >= 0 ? "+" : ""}
                      {a.r_multiple.toFixed(1)}R
                    </span>
                  )}
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
