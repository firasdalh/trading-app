import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice, fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import type { AssetClass, StrategyActivity } from "../types";

/**
 * What the strategies did, newest first: what they opened and the rules that held, what they closed and
 * why (a close rule, the stop, the target, a hand close), the signals refused or held back, and the signals
 * of a group on watch only (it opens nothing - asked 2026-11-06). A row
 * opens to show the details. Shared by the Strategies board (one strategy) and the desk's sidebar (every
 * strategy, so the board needn't be opened to find out - asked for 2026-11-02). On the desk it comes in two tabs,
 * Notifications and Trade history, each with its count (2026-11-18, see DeskStrategyActivity).
 */

const TONE: Record<string, string> = {
  opened: "text-bull", closed: "text-sky-300", refused: "text-amber-300", blocked: "text-amber-300",
  not_filled: "text-amber-300", close_failed: "text-bear", started: "text-amber-200", stopped: "text-neutral-400",
  edited: "text-neutral-500", stop_moved: "text-sky-200", stop_move_failed: "text-bear", target_reached: "text-bull",
  signal: "text-fuchsia-300",   // the signals' colour: their pop-up and their 👁 markers on the chart
};
// How a trade ended when its strategy did not close it (the backend reads it from the exit price).
const BY: Record<string, string> = { stop: "stop hit", target: "target hit", other: "closed outside the strategy" };
// A refused or held-back signal repeats on every bar while the rules hold: one row per run of them.
const REPEATS = new Set(["refused", "blocked", "not_filled"]);
const RUN_GAP_MS = 15 * 60_000;
// The same reason with its numbers ticking ("1.7h to the Friday close", then "1.6h") is the same reason.
const sameReason = (r: unknown) => String(r ?? "").replace(/\d+(?:\.\d+)?/g, "#");
// A stop following a SuperTrend line moves at most once a bar of that line's timeframe: one row per trade.
const STOP_MOVES = new Set(["stop_moved", "stop_move_failed"]);
const STOP_GAP_MS = 30 * 60_000;
const FRESH_MS = 120_000;
const DESK_FEED = 200;      // the API's most: refusals and signals are many, the trades few
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
    const key = REPEATS.has(a.event) || STOP_MOVES.has(a.event) ? [a.event, a.symbol, d.side, sameReason(d.reason)].join("|") : "";
    if (!key) {
      // Anything else on this pair ends its runs: a refusal before an open stays its own row.
      for (const [k, r] of runs) if (r.a.symbol === a.symbol) runs.delete(k);
    }
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
        {n > 1 && <span className="text-neutral-500" title={`${n} times, ${hms(first)} - ${hms(a.at)}`}>×{n}</span>}
        {side && <span className={d.side === "long" ? "text-bull" : "text-bear"}>{side}</span>}
        {a.symbol && <span className="text-neutral-200">{displaySymbol(a.symbol)}</span>}
        {a.event === "opened" && lots != null && <span className="tabular-nums text-neutral-400">{lots} lot</span>}
        {a.event === "stop_moved" && num("to") != null && <span className="tabular-nums text-neutral-400">→ {fmtPrice(num("to"))}</span>}
        {a.event === "target_reached" && <span className="text-neutral-400">🎯 done for the day</span>}
        {a.event === "signal" && (
          <span className="text-neutral-400">
            👁 group {String(d.group ?? "")} · watch only{num("price") != null && ` · at ${fmtPrice(num("price"))}`}
          </span>
        )}
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
          {a.event === "signal" && (
            <Rules title={`Group ${String(d.group ?? "")} is on watch only: all of it held, and it opened nothing. Its rules:`} rules={rules} />
          )}
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

export function ActivityFeed({ items, onPick, empty, newSince }: {
  items: StrategyActivity[] | null | undefined;
  onPick?: (symbol: string, ac: AssetClass) => void;
  empty: string;
  /** Rows after this time are marked new (the desk: what arrived since you last looked at the tab). */
  newSince?: string | null;
}) {
  const rows = rowsOf(items ?? []);
  if (!rows.length) return <p className="py-1 text-xs text-neutral-600">{empty}</p>;
  const now = Date.now();
  const since = newSince ? Date.parse(newSince) : null;
  return (
    <div>
      {rows.map((r) => {
        const t = r.a.at ? Date.parse(r.a.at) : null;
        // Your own edits are never "new" on the desk (they are not counted there either).
        const fresh = t != null && (now - t < FRESH_MS || (since != null && t > since && r.a.event !== "edited"));
        return <ActivityRow key={r.key} row={r} onPick={onPick} fresh={fresh} />;
      })}
    </div>
  );
}

// THE DESK'S TWO TABS (asked 2026-11-18: "the notifications and the trade history are mixed here - make them
// separate, notifications in one tab and the other in another, with the number of notifications there").
// TRADE HISTORY = what happened to a trade: opened, closed (by its rule, the stop, the target or by hand), its
// stop moved, a close or a stop move that failed. NOTIFICATIONS = everything else the strategies tell you: the
// 👁 watch-only signals (the ones that pop up), signals refused or held back, an order not filled, the daily
// target reached, a strategy started, stopped or edited.
type Tab = "notes" | "trades";
const TRADE_EVENTS = new Set(["opened", "closed", "stop_moved", "stop_move_failed", "close_failed"]);
const tabOf = (a: StrategyActivity): Tab => (TRADE_EVENTS.has(a.event) ? "trades" : "notes");
// Your own edits are listed, never counted: they are not news to you.
const counted = (a: StrategyActivity) => a.event !== "edited";
const TABS: { id: Tab; icon: string; label: string; empty: string; hint: string }[] = [
  {
    id: "notes", icon: "🔔", label: "Notifications",
    empty: "No notifications yet - your strategies' 👁 signals, the signals held back and their notices land here.",
    hint: "What the strategies tell you: 👁 watch-only signals, signals refused or held back, an order not filled, the daily target, started / stopped / edited",
  },
  {
    id: "trades", icon: "📒", label: "Trade history",
    empty: "No trades yet - what your strategies open and close lands here, with the reason.",
    hint: "What happened to the strategies' trades: opened, closed (its rule, the stop, the target or by hand), stop moved",
  },
];
const tms = (at: string | null | undefined) => (at ? Date.parse(at) : NaN);

/** On screen: its section unfolded, scrolled into the sidebar's view, and the page in front. */
function useShown(ref: RefObject<HTMLElement>) {
  const [inView, setInView] = useState(false);
  const [pageOn, setPageOn] = useState(() => document.visibilityState === "visible");
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(([e]) => setInView(e.isIntersecting));
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  useEffect(() => {
    const on = () => setPageOn(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return inView && pageOn;
}

/** The desk sidebar's copy: every strategy's activity, read every few seconds, in two tabs. */
export function DeskStrategyActivity({ onPick }: { onPick?: (symbol: string, ac: AssetClass) => void }) {
  const { data, error } = usePolling(() => api.strategyActivityAll(DESK_FEED), DESK_POLL_MS);
  const [tab, setTab] = useLocalStorage<Tab>("desk.activity.tab", "notes");
  // The newest row each tab had when you last had it on screen: anything later is "new" (its count).
  const [seen, setSeen] = useLocalStorage<Record<Tab, string | null>>("desk.activity.seen", { notes: null, trades: null });
  const seenRef = useRef(seen);
  seenRef.current = seen;
  const boxRef = useRef<HTMLDivElement>(null);
  const shown = useShown(boxRef);
  // While a tab is on screen, the rows that were new when you opened it stay marked new.
  const [since, setSince] = useState<{ tab: Tab; at: string | null } | null>(null);

  const items = useMemo(() => data ?? [], [data]);
  const byTab = useMemo(() => ({
    notes: items.filter((a) => tabOf(a) === "notes"),
    trades: items.filter((a) => tabOf(a) === "trades"),
  }), [items]);
  const newest = (t: Tab) =>
    byTab[t].reduce<string | null>((m, a) => (a.at && (m == null || tms(a.at) > tms(m)) ? a.at : m), null);

  // The very first time: what is already there counts as seen (no count of 40 old rows on day one).
  useEffect(() => {
    if (!data) return;
    const s = seenRef.current;
    if (s.notes != null && s.trades != null) return;
    const now = new Date().toISOString();
    setSeen({ notes: s.notes ?? newest("notes") ?? now, trades: s.trades ?? newest("trades") ?? now });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);
  // The tab on screen is read: its count clears, and what was new when you opened it stays marked.
  useEffect(() => {
    if (!shown) {
      setSince(null);
      return;
    }
    const at = seenRef.current[tab];          // read NOW: the updater runs after this render's seen has moved on
    setSince((s) => (s && s.tab === tab ? s : { tab, at }));
  }, [shown, tab]);
  useEffect(() => {
    if (!data || !shown) return;
    const n = newest(tab);
    const was = seenRef.current[tab];
    if (n && (was == null || tms(n) > tms(was))) setSeen((s) => ({ ...s, [tab]: n }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, shown, tab]);

  // Per tab: how many rows arrived since you last looked, and how many today (a run of the same refusal is
  // one row, as listed). "+" when the feed's window does not reach back to midnight.
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const reachesMidnight = items.length < DESK_FEED || items.some((a) => tms(a.at) < midnight.getTime());
  const tally = (t: Tab) => {
    const rows = rowsOf(byTab[t]).filter((r) => counted(r.a));
    const s = seen[t];
    return {
      fresh: s == null ? 0 : rows.filter((r) => tms(r.a.at) > tms(s)).length,
      today: rows.filter((r) => tms(r.a.at) >= midnight.getTime()).length,
    };
  };

  const cur = TABS.find((t) => t.id === tab) ?? TABS[0];
  return (
    <div ref={boxRef}>
      <div role="tablist" aria-label="Strategy activity" className="flex gap-1 border-b border-neutral-800/80 px-2 py-1.5">
        {TABS.map((t) => {
          const on = t.id === cur.id;
          const { fresh, today } = tally(t.id);
          const todayTxt = `${today}${reachesMidnight ? "" : "+"}`;
          const tip = `${t.hint}.${fresh ? ` ${fresh} new since you last looked.` : ""} ${todayTxt} today${t.id === "notes" ? " (your own edits not counted)" : ""}.`;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => setTab(t.id)}
              title={tip}
              className={`flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition
                focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50
                ${on ? "bg-neutral-800 text-neutral-100 ring-1 ring-inset ring-neutral-700" : "text-neutral-400 hover:bg-neutral-800/50 hover:text-neutral-200"}`}
            >
              <span aria-hidden="true">{t.icon}</span>
              <span className="truncate">{t.label}</span>
              {fresh > 0 ? (
                <span className="shrink-0 rounded bg-warn px-1.5 py-px text-[11px] font-bold leading-4 tabular-nums text-black">
                  {fresh} new
                </span>
              ) : today > 0 ? (
                <span className="shrink-0 rounded bg-neutral-800 px-1.5 py-px text-[11px] leading-4 tabular-nums text-neutral-400 ring-1 ring-inset ring-neutral-700/60">
                  {todayTxt}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      <div role="tabpanel" aria-label={cur.label} className="max-h-96 overflow-y-auto overscroll-contain px-3 py-1">
        {data == null && error ? (
          <p className="py-1 text-xs text-amber-300/80">Can't read the strategies' activity: {error}</p>
        ) : (
          <ActivityFeed
            items={byTab[cur.id]}
            onPick={onPick}
            newSince={since && since.tab === cur.id ? since.at : null}
            empty={data == null ? "Loading…" : cur.empty}
          />
        )}
      </div>
    </div>
  );
}
