import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { AnalysisText } from "./AnalysisText";
import { fmtUsd } from "../format";
import type { AdvisorState, PositionAdvice } from "../types";
import { actionText, ago, localTime } from "./advisorFormat";
import { srcLabel, srcColor } from "./sourceMeta";

interface Props {
  // Language the ANALYSIS prose is shown in ("en" | "ar"), from settings.
  lang?: string;
  // Bump to force a refresh (e.g. after a position is closed).
  refreshSignal?: number;
}

type Severity = PositionAdvice["severity"];

const TONE: Record<Severity, { box: string; chip: string; accent: string; label: string }> = {
  danger: { box: "border-bear/40 bg-bear/5", chip: "bg-bear/20 text-bear", accent: "border-l-bear", label: "Act now" },
  warn: { box: "border-warn/30 bg-warn/5", chip: "bg-warn/20 text-warn", accent: "border-l-warn", label: "Decide" },
  info: { box: "border-neutral-800 bg-neutral-900/40", chip: "bg-neutral-800 text-neutral-300", accent: "border-l-neutral-600", label: "OK" },
};

const THESIS: Record<PositionAdvice["thesis"], { text: string; cls: string }> = {
  intact: { text: "on track", cls: "text-bull" },
  weakening: { text: "losing steam", cls: "text-warn" },
  invalidated: { text: "trend flipped", cls: "text-bear" },
  unknown: { text: "no read", cls: "text-neutral-500" },
};

// AI guidance for OPEN positions — is each trade still on track vs. its plan, protect winners /
// cut losers, especially around news. Run on demand, or auto-watch on a set interval.
// Advisory only: it tells you what to consider; you act via the positions table below. The
// "Recent actions" timeline lives in its own card (AdvisorActivity), next to the Risk Dashboard.
export function PositionAdvicePanel({ refreshSignal, lang }: Props) {
  const [state, setState] = useState<AdvisorState | null>(null);
  const [busy, setBusy] = useState(false);
  const [intervalInput, setIntervalInput] = useState("300");
  const [maxHoldInput, setMaxHoldInput] = useState("0");

  const load = useCallback(async (run: boolean) => {
    setBusy(true);
    try {
      const s = run ? await api.advisorRun() : await api.advisorState();
      setState(s);
      setIntervalInput(String(s.interval_seconds));
      setMaxHoldInput(String(s.max_hold_hours ?? 0));
    } finally {
      setBusy(false);
    }
  }, []);

  // Initial load + refresh when the parent signals (e.g. a position closed).
  useEffect(() => {
    void load(false);
  }, [load, refreshSignal]);

  // The backend scheduler is the single runner for Auto-watch (it ticks at the configured
  // interval even with the tab closed). The panel just POLLS the state read-only every 15s so
  // "last check" + advice stay fresh — no double execution from the front-end.
  const enabled = state?.enabled ?? false;
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    const id = setInterval(() => void loadRef.current(false), 15_000);
    return () => clearInterval(id);
  }, []);

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
      // Good moment to ask for alert permission so headless auto-actions reach you.
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        void Notification.requestPermission();
      }
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

  const saveMaxHold = async () => {
    const h = Math.min(240, Math.max(0, Number(maxHoldInput) || 0));
    setBusy(true);
    try {
      setState(await api.advisorConfig({ max_hold_hours: h }));
    } finally {
      setBusy(false);
    }
  };

  const advice = state?.advice ?? [];
  const order = { danger: 0, warn: 1, info: 2 } as const;
  const sorted = useMemo(
    () => [...advice].sort((a, b) => order[a.severity] - order[b.severity]),
    [advice],
  );
  // At-a-glance summary across the advised positions.
  const counts = useMemo(() => {
    const c: Record<Severity, number> = { danger: 0, warn: 0, info: 0 };
    for (const a of advice) c[a.severity] += 1;
    return c;
  }, [advice]);
  const netPnl = useMemo(() => advice.reduce((s, a) => s + a.unrealized_pnl, 0), [advice]);

  return (
    <div className="card">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="card-title">Position advisor</span>
        {/* Live watch indicator */}
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
            enabled ? "bg-bull/15 text-bull" : "bg-neutral-800 text-neutral-500"
          }`}
          title={enabled ? "Auto-watch is on — re-checks on the interval" : "Auto-watch is off"}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${enabled ? "animate-pulse bg-bull" : "bg-neutral-600"}`} />
          {enabled ? "watching" : "manual"}
        </span>
        <span className="text-xs text-neutral-500">· last check {ago(state?.last_run_at ?? null)}</span>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-neutral-400">
            every
            <input
              name="advisor-interval"
              autoComplete="off"
              value={intervalInput}
              onChange={(e) => setIntervalInput(e.target.value)}
              onBlur={saveInterval}
              onKeyDown={(e) => e.key === "Enter" && saveInterval()}
              inputMode="numeric"
              className="field w-14 px-1.5 py-1 text-center tabular-nums"
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
          <button onClick={() => load(true)} disabled={busy} className="btn btn-primary">
            {busy ? "Checking…" : "Run now"}
          </button>
        </div>
      </div>

      {/* Summary strip — severity counts + net floating P&L across advised positions. */}
      {advice.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2 border-b border-neutral-800/70 pb-3 text-xs">
          {(["danger", "warn", "info"] as const).map((sev) => (
            <span
              key={sev}
              className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 font-medium ${
                counts[sev] > 0 ? TONE[sev].chip : "bg-neutral-800/40 text-neutral-600"
              }`}
            >
              <span className="tabular-nums font-bold">{counts[sev]}</span>
              {TONE[sev].label}
            </span>
          ))}
          <span className="ml-auto text-neutral-400">
            {advice.length} position{advice.length === 1 ? "" : "s"} · net{" "}
            <span className={`font-semibold tabular-nums ${netPnl >= 0 ? "text-bull" : "text-bear"}`}>
              {fmtUsd(netPnl, { sign: true })}
            </span>
          </span>
        </div>
      )}

      {autoExecute && (
        <div className="mb-2 space-y-1.5 rounded border border-bear/40 bg-bear/10 px-2 py-1.5 text-[11px] text-bear">
          <div>
            Auto-execute is ON — the advisor may close an invalidated trade or move a winner's stop
            to breakeven by itself. It never opens or sizes up, and respects the kill switch.
          </div>
          <label
            className="flex items-center gap-1.5 text-neutral-400"
            title="Time-stop: auto-close a stagnant position held this many hours and still roughly flat (neither target nor stop has resolved it), to free the slot. 0 = off."
          >
            Time-stop: close a flat trade after
            <input
              name="advisor-max-hold"
              autoComplete="off"
              value={maxHoldInput}
              onChange={(e) => setMaxHoldInput(e.target.value)}
              onBlur={saveMaxHold}
              onKeyDown={(e) => e.key === "Enter" && saveMaxHold()}
              inputMode="numeric"
              className="field w-14 px-1.5 py-1 text-center tabular-nums"
            />
            h {Number(maxHoldInput) > 0 ? "" : "(off)"}
          </label>
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
        <div className="flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-neutral-800 py-8 text-center">
          <span className="text-2xl opacity-40">🛡️</span>
          <div className="text-sm text-neutral-400">No open positions to advise on</div>
          <div className="text-xs text-neutral-600">
            {state?.last_run_at ? "The advisor will report here once a trade is open." : "Run a check to evaluate."}
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {sorted.map((a) => {
            const tone = TONE[a.severity];
            const th = THESIS[a.thesis];
            return (
              <div
                key={`${a.symbol}-${a.direction}`}
                className={`rounded-md border border-l-4 px-3 py-2 ${tone.box} ${tone.accent}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${tone.chip}`}>
                    {tone.label}
                  </span>
                  <span
                    className={`rounded px-1 py-0.5 text-[10px] font-bold uppercase ${
                      a.direction === "long" ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"
                    }`}
                  >
                    {a.direction === "long" ? "▲" : "▼"} {a.symbol}
                  </span>
                  {a.source && (
                    <span
                      className={`rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] font-semibold ${srcColor(a.source)}`}
                      title="Who opened this position"
                    >
                      {srcLabel(a.source)}
                    </span>
                  )}
                  {a.opened_at && (
                    <span className="text-[10px] text-neutral-500" title={`Opened ${localTime(a.opened_at)}`}>
                      opened {ago(a.opened_at)}
                    </span>
                  )}
                  <AnalysisText text={a.headline} lang={lang}
                                className="text-sm font-medium" />
                  <span className={`text-[10px] font-semibold uppercase ${th.cls}`}>{th.text}</span>
                  {a.r_multiple != null && (
                    <span
                      className={`rounded bg-neutral-800 px-1 py-0.5 text-[10px] tabular-nums ${
                        a.r_multiple >= 0 ? "text-bull" : "text-bear"
                      }`}
                      title="How far in profit vs. what you risked. +1.0R = you've made exactly what you put at risk; -1.0R = a full stop-out."
                    >
                      {a.r_multiple >= 0 ? "+" : ""}
                      {a.r_multiple.toFixed(1)}R
                    </span>
                  )}
                  <span
                    className={`ml-auto text-xs font-semibold tabular-nums ${
                      a.unrealized_pnl >= 0 ? "text-bull" : "text-bear"
                    }`}
                  >
                    {fmtUsd(a.unrealized_pnl, { sign: true })}
                  </span>
                </div>
                <AnalysisText as="p" text={a.detail} lang={lang}
                              className="mt-1 text-xs leading-relaxed text-neutral-300" />
                {a.events_soon && (
                  <div
                    className="mt-1 text-xs text-warn/90"
                    title="Upcoming medium-impact events (e.g. a central-bank speech) — a heads-up only. It does NOT pause the trade; the hard stand-aside is high-impact events only."
                  >
                    📅 {a.events_soon}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
