import { useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice, fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { AssetClass, StrategyActivity } from "../types";

/**
 * What the strategies did, newest first: what they opened and the rules that held, what they closed and
 * why (a close rule, the stop, the target, a hand close), and the signals refused or held back. A row
 * opens to show the details. Shared by the Strategies board (one strategy) and the desk's sidebar (every
 * strategy, so the board needn't be opened to find out - asked for 2026-11-02).
 */

const TONE: Record<string, string> = {
  opened: "text-bull", closed: "text-sky-300", refused: "text-amber-300", blocked: "text-amber-300",
  not_filled: "text-amber-300", close_failed: "text-bear", started: "text-amber-200", stopped: "text-neutral-400",
  edited: "text-neutral-500", stop_moved: "text-sky-200", stop_move_failed: "text-bear", target_reached: "text-bull",
};
// How a trade ended when its strategy did not close it (the backend reads it from the exit price).
const BY: Record<string, string> = { stop: "stop hit", target: "target hit", other: "closed outside the strategy" };
// A refused or held-back signal repeats on every bar while the rules hold: one row per run of them.
const REPEATS = new Set(["refused", "blocked", "not_filled"]);
const RUN_GAP_MS = 120_000;
// A stop following a SuperTrend line moves at most once a bar of that line's timeframe: one row per trade.
const STOP_MOVES = new Set(["stop_moved", "stop_move_failed"]);
const STOP_GAP_MS = 30 * 60_000;
const FRESH_MS = 120_000;
const DESK_FEED = 100;
const DESK_POLL_MS = 5000;

interface Row {
  a: StrategyActivity;      // the newest of the run
  n: number;
  first: string | null;     // when the run started
  key: string;              // the run's oldest event: steady while newer repeats arrive
}

function rowsOf(items: StrategyActivity[]): Row[] {
  const rows: Row[] = [];
  const runs = new Map<string, Row>();
  for (const a of items) {
    const d = a.detail ?? {};
    const key = REPEATS.has(a.event) || STOP_MOVES.has(a.event) ? [a.event, a.symbol, d.side, d.reason].join("|") : "";
    const run = key ? runs.get(key) : undefined;
    const gap = STOP_MOVES.has(a.event) ? STOP_GAP_MS : RUN_GAP_MS;
    if (run && run.first && a.at && Date.parse(run.first) - Date.parse(a.at) <= gap) {
      run.n += 1;
      run.first = a.at;
      run.key = String(a.id);
      continue;
    }
    const row = { a, n: 1, first: a.at, key: String(a.id) };
    rows.push(row);
    if (key) runs.set(key, row);
  }
  return rows;
}

// 24-hour, so the time fits its column (a 12-hour "06:58:21 PM" wrapped onto two lines).
const hms = (at: string | null) =>
  at ? new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "";

function Rules({ title, rules }: { title: string; rules: string[] }) {
  if (!rules.length) return null;
  return (
    <div>
      <div className="text-neutral-500">{title}</div>
      <ul className="mt-0.5 space-y-0.5">
        {rules.map((r, i) => (
          <li key={i} className="flex gap-1.5 text-neutral-300">
            <span className="text-neutral-600">•</span>
            <span className="min-w-0">{r}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ActivityRow({ row, fresh, onPick }: {
  row: Row; fresh: boolean; onPick?: (symbol: string, ac: AssetClass) => void;
}) {
  const [open, setOpen] = useState(false);
  const { a, n, first } = row;
  const d = (a.detail ?? {}) as Record<string, unknown>;
  const num = (k: string) => (typeof d[k] === "number" ? (d[k] as number) : null);
  const side = d.side === "long" ? "▲ BUY" : d.side === "short" ? "▼ SELL" : "";
  const rules = Array.isArray(d.rules) ? (d.rules as string[]) : [];
  const pnl = num("pnl");
  const by = typeof d.by === "string" ? d.by : null;
  const reason = typeof d.reason === "string" ? d.reason : null;
  const name = typeof d.name === "string" ? d.name : null;
  const lots = num("lots");
  const ruleClose = a.event === "closed" && !by;

  return (
    <div className={`border-b border-neutral-800/60 py-1 text-xs ${fresh ? "border-l-2 border-l-amber-400/70 pl-1.5" : ""}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={open ? "Hide the details" : "Show the details"}
        className="flex w-full flex-wrap items-baseline gap-x-2 text-left hover:bg-neutral-800/30"
      >
        <span className="w-16 shrink-0 tabular-nums text-neutral-500">{hms(a.at)}</span>
        <span className={`font-semibold ${TONE[a.event] ?? "text-neutral-300"}`}>{a.event.replace(/_/g, " ")}</span>
        {n > 1 && <span className="text-neutral-500">×{n}</span>}
        {side && <span className={d.side === "long" ? "text-bull" : "text-bear"}>{side}</span>}
        {a.symbol && <span className="text-neutral-200">{displaySymbol(a.symbol)}</span>}
        {a.event === "opened" && lots != null && <span className="tabular-nums text-neutral-400">{lots} lot</span>}
        {a.event === "stop_moved" && num("to") != null && <span className="tabular-nums text-neutral-400">→ {fmtPrice(num("to"))}</span>}
        {a.event === "target_reached" && <span className="text-neutral-400">🎯 done for the day</span>}
        {pnl != null && <b className={pnl >= 0 ? "text-bull" : "text-bear"}>{fmtUsd(pnl, { sign: true })}</b>}
        {by && (
          <span className={by === "stop" ? "text-bear/90" : by === "target" ? "text-bull/90" : "text-neutral-400"}>
            · {BY[by] ?? by}
          </span>
        )}
        {ruleClose && <span className="text-neutral-400">· its close rule{d.manual === true ? " (your trade)" : ""}</span>}
        <span className="ml-auto text-neutral-600" aria-hidden="true">{open ? "▾" : "▸"}</span>
        {reason && !open &&<span className="min-w-0 basis-full truncate pl-[4.5rem] text-amber-300/80">{reason}</span>}
      </button>

      {open && (
        <div className="mt-1 space-y-1.5 rounded bg-neutral-950/50 px-2 py-1.5 text-[11px] leading-snug">
          {a.event === "opened" && (
            <>
              <div className="tabular-nums text-neutral-300">
                Entry {fmtPrice(num("entry"))} · Stop {fmtPrice(num("stop"))} · Target {num("target") != null ? fmtPrice(num("target")) : "none"}
                {lots != null && ` · ${lots} lot`}
              </div>
              <Rules title="Why it opened - the rules that held:" rules={rules} />
            </>
          )}
          {ruleClose && (
            <>
              {pnl == null && <div className="text-neutral-400">P&L: waiting for the broker's figure</div>}
              <Rules title={d.manual === true ? "Why it closed your trade - its close rule:" : "Why it closed - its close rule:"} rules={rules} />
            </>
          )}
          {by && (
            <div className="space-y-0.5 tabular-nums text-neutral-300">
              <div>
                {by === "stop"
                  ? `The price reached its stop (${fmtPrice(num("stop"))}): closed at ${fmtPrice(num("exit"))}.`
                  : by === "target"
                    ? `The price reached its target (${fmtPrice(num("target"))}): closed at ${fmtPrice(num("exit"))}.`
                    : `Closed at ${fmtPrice(num("exit"))}, not by the strategy: by hand, the kill-switch or another tool.`}
              </div>
              <div className="text-neutral-400">
                Entry {fmtPrice(num("entry"))}
                {lots != null && ` · ${lots} lot`}
                {pnl == null && " · P&L: waiting for the broker's figure"}
              </div>
            </div>
          )}
          {REPEATS.has(a.event) && (
            <>
              {reason && <div className="text-amber-300/90">{reason}</div>}
              {n > 1 && (
                <div className="text-neutral-500">
                  {n} times, {hms(first)} - {hms(a.at)}: the signal kept holding and was held back each time.
                </div>
              )}
              <Rules title="The rules that held:" rules={rules} />
            </>
          )}
          {a.event === "close_failed" && <Rules title="It tried to close on:" rules={rules} />}
          {a.event === "stop_moved" && (
            <div className="tabular-nums text-neutral-300">
              {n > 1 && `${n} moves, ${hms(first)} - ${hms(a.at)}. The last: `}
              {d.yours ? "your trade's" : "its"} stop followed the {String(d.tf)} SuperTrend line ({fmtPrice(num("line"))}), {fmtPrice(num("frm"))} → {fmtPrice(num("to"))}.
            </div>
          )}
          {a.event === "target_reached" && (
            <div className="tabular-nums text-neutral-300">
              The {num("trades") ?? ""} trades it closed today made {fmtUsd(pnl, { sign: true })}
              {typeof d.at === "string" && ` by ${hms(d.at)}`}: its daily target is +{fmtUsd(num("target"))}. It opens
              nothing more until 00:00 UTC; the trades it holds keep their close rules, stop and target.
            </div>
          )}
          {a.event === "stop_move_failed" && (
            <div className="tabular-nums text-amber-300/90">
              The broker refused the move to {fmtPrice(num("to"))}{reason ? `: ${reason}` : ""}. The stop stayed where it was.
            </div>
          )}
          <div className="flex items-center gap-2 text-neutral-500">
            {name && <span className="min-w-0 truncate">Strategy: {name}</span>}
            {a.symbol && a.asset_class && onPick && (
              <button
                type="button"
                onClick={() => onPick(a.symbol as string, a.asset_class as AssetClass)}
                className="ml-auto shrink-0 rounded px-1.5 py-px text-amber-200 hover:bg-neutral-800"
              >
                Chart {displaySymbol(a.symbol)} ›
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function ActivityFeed({ items, onPick, empty }: {
  items: StrategyActivity[] | null | undefined;
  onPick?: (symbol: string, ac: AssetClass) => void;
  empty: string;
}) {
  const rows = rowsOf(items ?? []);
  if (!rows.length) return <p className="py-1 text-xs text-neutral-600">{empty}</p>;
  const now = Date.now();
  return (
    <div>
      {rows.map((r) => (
        <ActivityRow key={r.key} row={r} onPick={onPick} fresh={!!r.a.at && now - Date.parse(r.a.at) < FRESH_MS} />
      ))}
    </div>
  );
}

/** The desk sidebar's copy: every strategy's activity, read every few seconds. */
export function DeskStrategyActivity({ onPick }: { onPick?: (symbol: string, ac: AssetClass) => void }) {
  const { data, error } = usePolling(() => api.strategyActivityAll(DESK_FEED), DESK_POLL_MS);
  return (
    <div className="max-h-96 overflow-y-auto overscroll-contain px-3 py-1">
      {data == null && error ? (
        <p className="py-1 text-xs text-amber-300/80">Can't read the strategies' activity: {error}</p>
      ) : (
        <ActivityFeed
          items={data}
          onPick={onPick}
          empty={data == null ? "Loading…" : "Nothing yet - what your strategies open, close and are refused shows here."}
        />
      )}
    </div>
  );
}
