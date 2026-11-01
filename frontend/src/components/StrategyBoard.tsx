import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { assetLabel, displaySymbol, fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import { TF_SECONDS } from "../timeframes";
import type {
  AssetClass,
  StrategyActivity,
  StrategyCatalog,
  StrategyCheckCondition,
  StrategyCheckSet,
  StrategyCondition,
  StrategyConditionSpec,
  StrategyDraft,
  StrategyGroups,
  StrategyView,
} from "../types";

// The condition picker's names (the catalog carries the sentences; these are the menu entries).
const NAMES: Record<string, string> = {
  ema_cross: "EMA cross", ema_above: "EMA above / below", price_ema: "Closes vs an EMA", trend: "Trend (EMA 20 / 50)",
  supertrend: "SuperTrend", adx: "ADX trend strength", rsi_rising: "RSI rising / falling", rsi_level: "RSI level",
  macd: "MACD histogram", stoch_cross: "Stochastic cross", vwap: "VWAP side", volume: "Volume spike",
  bb_break: "Bollinger breakout", candles: "Candle colour", sr_room: "Room to S/R (1h / 4h / 1d)",
  zone_room: "Room to supply / demand", spread: "Spread filter", session: "Session filter",
  rsi_extreme: "Take profit: RSI stretched", profit_r: "Take profit: at +R", time_stop: "Time stop",
};

type Where = "long" | "short" | "exit";

// The cooldowns a strategy can pick, in minutes. The lowest are the floors the user confirmed for
// scalp-style trading (RISK.md override 2026-11-01): no wait between trades, 5 min after a loss.
const PAIR_WAITS = [0, 1, 2, 5, 10, 15, 30, 60, 120, 240];
const LOSS_WAITS = [5, 10, 15, 30, 60, 120, 180, 360];

const usable = (spec: StrategyConditionSpec, where: Where) => (where === "exit" ? spec.use !== "entry" : spec.use !== "exit");

function newCondition(spec: StrategyConditionSpec, tf: string): StrategyCondition {
  // A condition that reads only some timeframes (the 1h / 4h / 1d levels) starts on its own default.
  const t = spec.tfs && !spec.tfs.includes(tf) ? (spec.default_tf ?? spec.tfs[0]) : tf;
  return { type: spec.type, tf: t, params: Object.fromEntries(spec.params.map((p) => [p.key, p.default])) };
}

// The user's own example as the starting point: EMA 10 x 20 with RSI rising, the 5m trend up and
// SuperTrend green to buy (mirrored to sell); out when SuperTrend 5m turns, or when SuperTrend 1m
// turns AND two closes land beyond EMA 10.
function template(symbol: string, assetClass: AssetClass): StrategyDraft {
  const st = { period: 10, mult: 2.7, fresh: 0 };
  return {
    name: "EMA + SuperTrend scalp",
    pairs: symbol ? [{ symbol, asset_class: assetClass }] : [],
    timeframe: "1m",
    sides: "both",
    entry_long: [[
      { type: "ema_cross", tf: "1m", params: { fast: 10, slow: 20, within: 3 } },
      { type: "rsi_rising", tf: "1m", params: { period: 7, bars: 1, limit: 80 } },
      { type: "trend", tf: "5m", params: {} },
      { type: "supertrend", tf: "1m", params: st },
    ]],
    entry_short: [],
    mirror_short: true,
    exit_rules: [
      [{ type: "supertrend", tf: "5m", params: st }],
      [{ type: "supertrend", tf: "1m", params: st }, { type: "price_ema", tf: "1m", params: { period: 10, bars: 2 } }],
    ],
    risk_pct: 0.005,
    stop_atr: 1.5,
    target_r: 2,
    max_trades_day: 10,
    manage_manual: false,
    cooldown_minutes: 0,
    loss_cooldown_minutes: 5,
  };
}

export function draftOf(s: StrategyView): StrategyDraft {
  return {
    name: s.name, pairs: s.pairs, timeframe: s.timeframe, sides: s.sides, entry_long: s.entry_long,
    entry_short: s.entry_short, mirror_short: s.mirror_short, exit_rules: s.exit_rules, risk_pct: s.risk_pct,
    stop_atr: s.stop_atr, target_r: s.target_r, max_trades_day: s.max_trades_day, manage_manual: s.manage_manual,
    cooldown_minutes: s.cooldown_minutes ?? 0, loss_cooldown_minutes: s.loss_cooldown_minutes ?? 5,
  };
}

function Wait({ label, value, options, onChange, title }: {
  label: string; value: number; options: number[]; onChange: (v: number) => void; title: string;
}) {
  const opts = options.includes(value) ? options : [...options, value].sort((a, b) => a - b);
  return (
    <label className="flex items-center gap-1.5 text-xs text-neutral-400" title={title}>
      {label}
      <select
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="rounded border border-neutral-700 bg-neutral-950 px-1 py-0.5 tabular-nums text-amber-200"
      >
        {opts.map((m) => <option key={m} value={m}>{m === 0 ? "no wait" : `${m} min`}</option>)}
      </select>
    </label>
  );
}

// A sentence with its {params} filled in (read-only), as the mirrored SELL rules and the log show it.
function sentence(spec: StrategyConditionSpec | undefined, where: Where, params: Record<string, number>): string {
  if (!spec) return "?";
  const text = spec[where] || spec.long;
  return text.replace(/\{(~?)(\w+)\}/g, (_m, flip: string, k: string) => {
    const p = spec.params.find((x) => x.key === k);
    const v = params[k] ?? p?.default ?? 0;
    return p?.kind === "bool" ? (v ? "on" : "off") : String(flip ? 100 - v : v);
  });
}

// The engine reads a running strategy about a second after each candle closes on the fastest
// timeframe its rules use (app/agents/strategy.py, GRACE): when that is next, and on which timeframe.
function nextLook(d: StrategyDraft): { tf: string; secs: number } {
  const tfs = new Set([d.timeframe, ...[d.entry_long, d.entry_short, d.exit_rules].flat(2).map((c) => c.tf)]);
  const tf = [...tfs].sort((a, b) => (TF_SECONDS[a] ?? 60) - (TF_SECONDS[b] ?? 60))[0] ?? d.timeframe;
  const len = TF_SECONDS[tf] ?? 60;
  const now = Date.now() / 1000;
  return { tf, secs: Math.ceil(len - (now % len) + 1) };
}

function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
}

function State({ c, prefix }: { c?: StrategyCheckCondition; prefix?: string }) {
  if (!c) return null;
  const tone = c.ok === true ? "bg-bull/20 text-bull" : c.ok === false ? "bg-bear/15 text-bear" : "bg-neutral-800 text-neutral-500";
  return (
    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${tone}`} title={`${c.label}\nnow: ${c.detail}`}>
      {prefix}{c.ok === true ? "✓" : c.ok === false ? "✗" : "…"} {c.detail}
    </span>
  );
}

function ConditionRow({ cond, where, catalog, tfs, onChange, onRemove, states }: {
  cond: StrategyCondition;
  where: Where;
  catalog: StrategyCatalog;
  tfs: string[];
  onChange: (c: StrategyCondition) => void;
  onRemove: () => void;
  states: { c?: StrategyCheckCondition; prefix?: string }[];
}) {
  const spec = catalog.conditions.find((s) => s.type === cond.type);
  const groups = useMemo(() => {
    const m = new Map<string, StrategyConditionSpec[]>();
    for (const s of catalog.conditions.filter((x) => usable(x, where))) m.set(s.group, [...(m.get(s.group) ?? []), s]);
    return [...m.entries()];
  }, [catalog, where]);
  const text = spec ? spec[where] || spec.long : cond.type;
  const parts = text.split(/(\{~?\w+\})/g).filter(Boolean);
  return (
    <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 rounded border border-neutral-800 bg-neutral-950/60 px-2 py-1 text-xs">
      <select
        value={cond.type}
        onChange={(e) => {
          const s = catalog.conditions.find((x) => x.type === e.target.value);
          if (s) onChange(newCondition(s, cond.tf));
        }}
        className="rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 text-neutral-200"
        title={spec?.tip}
      >
        {groups.map(([g, specs]) => (
          <optgroup key={g} label={g}>
            {specs.map((s) => (
              <option key={s.type} value={s.type}>{NAMES[s.type] ?? s.type}{s.suggested ? "  ★" : ""}</option>
            ))}
          </optgroup>
        ))}
      </select>
      <span className="text-neutral-300" title={spec?.tip}>
        {parts.map((part, i) => {
          const m = part.match(/^\{(~?)(\w+)\}$/);
          const p = m && spec?.params.find((x) => x.key === m[2]);
          if (!p) return <span key={i}>{part}</span>;
          // "~": shown and typed from the other side of the 0-100 scale (a SELL's 20 is a BUY's 80).
          const flip = m[1] === "~";
          const raw = cond.params[p.key] ?? p.default;
          const v = flip ? 100 - raw : raw;
          const set = (x: number) => onChange({ ...cond, params: { ...cond.params, [p.key]: flip ? 100 - x : x } });
          return p.kind === "bool" ? (
            <input key={i} type="checkbox" checked={v >= 1} onChange={(e) => set(e.target.checked ? 1 : 0)} className="mx-0.5 align-middle accent-amber-500" title={p.label} />
          ) : (
            <input
              key={i}
              type="number"
              value={v}
              min={flip ? 100 - p.max : p.min}
              max={flip ? 100 - p.min : p.max}
              step={p.step}
              onChange={(e) => set(Number(e.target.value))}
              className="mx-0.5 w-14 rounded border border-neutral-700 bg-neutral-900 px-1 text-center tabular-nums text-amber-200"
              title={`${p.label}: ${flip ? 100 - p.max : p.min} to ${flip ? 100 - p.min : p.max}`}
            />
          );
        })}
      </span>
      <span className="text-neutral-500">on</span>
      <select
        value={cond.tf}
        onChange={(e) => onChange({ ...cond, tf: e.target.value })}
        className="rounded border border-neutral-700 bg-neutral-900 px-1 py-0.5 font-semibold text-sky-300"
        title="The timeframe this condition reads (closed bars only)"
      >
        {(spec?.tfs ?? tfs).map((t) => <option key={t} value={t}>{t}</option>)}
      </select>
      <span className="ml-auto flex flex-wrap items-center gap-1">
        {states.map((s, i) => <State key={i} {...s} />)}
        <button onClick={onRemove} className="rounded px-1 text-neutral-500 hover:bg-bear/20 hover:text-bear" title="Remove this condition">×</button>
      </span>
    </div>
  );
}

// One rule set: groups of conditions. A group holds when ALL its conditions do; the set fires when
// ANY group does. `check` is the live read for the BUY / SELL set; `checks` for the CLOSE set (one per side).
function RuleSet({ where, groups, onChange, catalog, tfs, baseTf, check, checks }: {
  where: Where;
  groups: StrategyGroups;
  onChange: (g: StrategyGroups) => void;
  catalog: StrategyCatalog;
  tfs: string[];
  baseTf: string;
  check?: StrategyCheckSet | null;
  checks?: { set: StrategyCheckSet | null | undefined; prefix: string }[];
}) {
  const first = catalog.conditions.find((s) => usable(s, where) && (where === "exit" ? s.type === "supertrend" : s.type === "ema_cross"))
    ?? catalog.conditions.find((s) => usable(s, where))!;
  const setGroup = (gi: number, g: StrategyCondition[]) => onChange(groups.map((x, i) => (i === gi ? g : x)));
  return (
    <div className="space-y-2">
      {groups.map((g, gi) => {
        const held = check?.groups[gi]?.ok || (checks ?? []).some((c) => c.set?.groups[gi]?.ok);
        const closes = (checks ?? []).filter((c) => c.set?.groups[gi]?.ok).map((c) => c.prefix.replace("if ", "").trim());
        return (
          <div key={gi} className={`rounded-lg border p-2 ${held ? "border-bull/60 bg-bull/[0.05]" : "border-neutral-800 bg-neutral-900/40"}`}>
            <div className="mb-1.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
              {gi > 0 && <span className="rounded bg-amber-500/20 px-1.5 text-amber-300">or</span>}
              {where === "exit" ? `Close rule ${gi + 1}` : `Group ${gi + 1}`} - all of these
              {held && (
                <span className="rounded bg-bull/20 px-1.5 text-bull">
                  {closes.length ? `would close a ${closes.join(" or a ")} now` : "holds now"}
                </span>
              )}
              <button
                onClick={() => onChange(groups.filter((_, i) => i !== gi))}
                className="ml-auto rounded px-1.5 normal-case text-neutral-500 hover:bg-bear/20 hover:text-bear"
                title="Remove this whole group"
              >
                remove
              </button>
            </div>
            <div className="space-y-1">
              {g.map((c, ci) => (
                <ConditionRow
                  key={ci}
                  cond={c}
                  where={where}
                  catalog={catalog}
                  tfs={tfs}
                  onChange={(nc) => setGroup(gi, g.map((x, i) => (i === ci ? nc : x)))}
                  onRemove={() => setGroup(gi, g.filter((_, i) => i !== ci))}
                  states={check ? [{ c: check.groups[gi]?.conditions[ci] }] : (checks ?? []).map((x) => ({ c: x.set?.groups[gi]?.conditions[ci], prefix: x.prefix }))}
                />
              ))}
            </div>
            {g.length < catalog.max_conditions && (
              <button
                onClick={() => setGroup(gi, [...g, newCondition(first, baseTf)])}
                className="mt-1.5 rounded border border-dashed border-neutral-700 px-2 py-0.5 text-[11px] text-neutral-400 hover:border-amber-500/60 hover:text-amber-200"
              >
                + and…
              </button>
            )}
          </div>
        );
      })}
      {groups.length < catalog.max_groups && (
        <button
          onClick={() => onChange([...groups, [newCondition(first, baseTf)]])}
          className="rounded border border-dashed border-neutral-700 px-2 py-1 text-xs text-neutral-400 hover:border-amber-500/60 hover:text-amber-200"
        >
          {groups.length ? (where === "exit" ? "+ or another close rule" : "+ or another group") : where === "exit" ? "+ add a close rule" : "+ add a group"}
        </button>
      )}
    </div>
  );
}

function Num({ label, value, onChange, min, max, step, suffix, title }: {
  label: string; value: number; onChange: (v: number) => void; min: number; max: number; step: number; suffix?: string; title?: string;
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs text-neutral-400" title={title}>
      {label}
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-16 rounded border border-neutral-700 bg-neutral-950 px-1 py-0.5 text-center tabular-nums text-amber-200"
      />
      {suffix}
    </label>
  );
}

const EVENT_TONE: Record<string, string> = {
  opened: "text-bull", closed: "text-sky-300", refused: "text-amber-300", blocked: "text-amber-300",
  not_filled: "text-amber-300", close_failed: "text-bear", started: "text-amber-200", stopped: "text-neutral-400",
  edited: "text-neutral-500",
};

function ActivityRow({ a }: { a: StrategyActivity }) {
  const d = (a.detail ?? {}) as Record<string, unknown>;
  const side = d.side === "long" ? "▲ BUY" : d.side === "short" ? "▼ SELL" : "";
  const rules = Array.isArray(d.rules) ? (d.rules as string[]).join(" · ") : "";
  const pnl = typeof d.pnl === "number" ? fmtUsd(d.pnl, { sign: true }) : null;
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 border-b border-neutral-800/60 py-1 text-xs">
      <span className="w-16 shrink-0 tabular-nums text-neutral-500">{a.at ? new Date(a.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}</span>
      <span className={`font-semibold ${EVENT_TONE[a.event] ?? "text-neutral-300"}`}>{a.event.replace("_", " ")}</span>
      {side && <span className={d.side === "long" ? "text-bull" : "text-bear"}>{side}</span>}
      {a.symbol && <span className="text-neutral-300">{displaySymbol(a.symbol)}</span>}
      {typeof d.lots === "number" && <span className="tabular-nums text-neutral-400">{d.lots} lot</span>}
      {pnl && <b className={(d.pnl as number) >= 0 ? "text-bull" : "text-bear"}>{pnl}</b>}
      {d.manual === true && <span className="text-amber-300">(your trade)</span>}
      {typeof d.reason === "string" && <span className="text-amber-300/90">{d.reason}</span>}
      {rules && <span className="min-w-0 basis-full truncate pl-[4.5rem] text-neutral-500" title={rules}>{rules}</span>}
    </div>
  );
}

const MARKETS: AssetClass[] = ["crypto", "forex", "metal", "energy", "index", "stock"];

// "+ Add a pair": the broker's symbols for a market, searchable by ticker or name, with the watchlist
// and the chart's pair on top. Stays open so several can be added in a row; pairs already in the
// strategy are ticked.
function PairPicker({ added, defaultClass, chartSymbol, onAdd }: {
  added: { symbol: string; asset_class: AssetClass }[];
  defaultClass: AssetClass;
  chartSymbol: string;
  onAdd: (symbol: string, ac: AssetClass) => void;
}) {
  const [open, setOpen] = useState(false);
  const [market, setMarket] = useState<AssetClass>(defaultClass);
  const [query, setQuery] = useState("");
  const boxRef = useRef<HTMLSpanElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { data: list, error: listErr } = usePolling(
    () => (open ? api.symbols(market) : Promise.resolve(null)), 600_000, [open, market]);
  const { data: watch } = usePolling(() => (open ? api.watchlist() : Promise.resolve(null)), 600_000, [open]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setTimeout(() => inputRef.current?.focus(), 0);
    const away = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const isAdded = (sym: string) => added.some((p) => p.symbol.toUpperCase() === sym.toUpperCase());
  const q = query.trim().toUpperCase();
  const desc = list?.descriptions ?? {};
  const matches = (list?.symbols ?? []).filter((x) => x.toUpperCase().includes(q) || (desc[x] ?? "").toUpperCase().includes(q));
  const watched = (watch?.items ?? [])
    .filter((w) => w.asset_class === market && (w.symbol.toUpperCase().includes(q) || (desc[w.symbol] ?? "").toUpperCase().includes(q)))
    .map((w) => w.symbol);
  const top = [...new Set([...(market === defaultClass && chartSymbol && chartSymbol.toUpperCase().includes(q) ? [chartSymbol] : []), ...watched])];

  const Row = ({ sym, tag }: { sym: string; tag?: string }) => {
    const done = isAdded(sym);
    return (
      <button
        onClick={() => !done && onAdd(sym, market)}
        disabled={done}
        className={`flex w-full items-center gap-2 px-2 py-1.5 text-left ${done ? "cursor-default opacity-60" : "hover:bg-neutral-700"}`}
        title={done ? "Already in this strategy" : `Add ${displaySymbol(sym)} to the strategy`}
      >
        <span className={`w-4 shrink-0 text-center ${done ? "text-bull" : "text-neutral-500"}`}>{done ? "✓" : "+"}</span>
        <span className="text-sm font-medium text-neutral-100">{displaySymbol(sym)}</span>
        {desc[sym] && <span className="truncate text-xs text-neutral-500">{desc[sym]}</span>}
        {tag && <span className="ml-auto shrink-0 rounded bg-neutral-800 px-1 text-[10px] text-neutral-400">{tag}</span>}
      </button>
    );
  };

  return (
    <span ref={boxRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="rounded-full border border-dashed border-amber-500/60 px-2.5 py-0.5 font-semibold text-amber-200 hover:bg-amber-500/10"
      >
        + Add a pair ▾
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-[24rem] rounded-lg border border-neutral-700 bg-neutral-900 text-xs shadow-xl">
          <div className="flex flex-wrap gap-1 border-b border-neutral-800 p-2" role="group" aria-label="Market">
            {MARKETS.map((m) => (
              <button
                key={m}
                onClick={() => { setMarket(m); setQuery(""); }}
                className={`rounded px-2 py-0.5 ${market === m ? "bg-amber-500 font-bold text-neutral-950" : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"}`}
              >
                {assetLabel(m)}
              </button>
            ))}
          </div>
          <div className="border-b border-neutral-800 p-2">
            <input
              ref={inputRef}
              name="strategy-pair-search"
              autoComplete="off"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.preventDefault();          // close this list, not the whole drawer
                  setOpen(false);
                } else if (e.key === "Enter") {
                  const first = [...top, ...matches].find((x) => !isAdded(x));
                  if (first) onAdd(first, market);
                  else if (q && !isAdded(q)) onAdd(q, market);   // a symbol the list doesn't have
                  setQuery("");
                }
              }}
              placeholder={`Search ${assetLabel(market).toLowerCase()} - ticker or name (e.g. "gold", "BTC")`}
              className="w-full rounded bg-neutral-800 px-2 py-1 text-sm uppercase text-neutral-100 placeholder:normal-case"
            />
          </div>
          <div className="max-h-80 overflow-y-auto py-1">
            {top.length > 0 && (
              <>
                <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-neutral-500">The chart's and your watchlist</div>
                {top.map((sym) => <Row key={`top-${sym}`} sym={sym} tag={sym === chartSymbol ? "chart" : "watchlist"} />)}
                <div className="my-1 border-t border-neutral-800" />
              </>
            )}
            <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-neutral-500">
              All {assetLabel(market).toLowerCase()} pairs at the broker{list ? ` (${matches.length})` : ""}
            </div>
            {!list && !listErr && <div className="px-2 py-2 text-neutral-500">Loading the broker's list…</div>}
            {listErr && <div className="px-2 py-2 text-amber-300">Can't load the list: {listErr}</div>}
            {matches.filter((x) => !top.includes(x)).map((sym) => <Row key={sym} sym={sym} />)}
            {list && matches.length === 0 && (
              <div className="px-2 py-2 text-neutral-500">
                {q ? <>No match. <button onClick={() => { onAdd(q, market); setQuery(""); }} className="text-amber-300 underline">Add “{q}” anyway</button></> : "No pairs."}
              </div>
            )}
          </div>
          <div className="flex items-center justify-between border-t border-neutral-800 px-2 py-1.5 text-neutral-500">
            <span>{added.length} pair{added.length === 1 ? "" : "s"} in the strategy</span>
            <button onClick={() => setOpen(false)} className="rounded px-2 py-0.5 text-neutral-300 hover:bg-neutral-700">Done</button>
          </div>
        </div>
      )}
    </span>
  );
}

/**
 * THE STRATEGIES BOARD: build your own BUY / SELL / CLOSE rules from indicators on any timeframes,
 * watch what they read on a pair right now, and let them trade.
 *
 * A strategy runs on the backend (app/agents/strategy.py), every few seconds, on closed bars; it keeps
 * running with this board closed. It only trades a demo / paper account, every order carries a
 * broker-side stop and goes through the Risk Manager, and it is saved stopped: running it is its own
 * click.
 */
export function StrategyBoard({ assetClass }: { assetClass: AssetClass }) {
  const [chartSymbol] = useLocalStorage("ta.symbol", "");
  const { data: catalog } = usePolling(() => api.strategyCatalog(), 600_000);
  const { data: list, error: listErr } = usePolling(() => api.strategies(), 5000);
  const [sel, setSel] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState<StrategyDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [checkPair, setCheckPair] = useState(0);
  // A once-a-second redraw, for the countdown to the next candle close.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const saved = typeof sel === "number" ? (list ?? []).find((s) => s.id === sel) ?? null : null;
  // Open the first strategy, or a new one from the template when there is none yet.
  useEffect(() => {
    if (sel !== null || !list) return;
    if (list.length) {
      setSel(list[0].id);
      setDraft(draftOf(list[0]));
    } else {
      setSel("new");
      setDraft(template(chartSymbol, assetClass));
    }
  }, [list, sel, chartSymbol, assetClass]);

  const dirty = !!draft && (sel === "new" || (!!saved && JSON.stringify(draftOf(saved)) !== JSON.stringify(draft)));
  const pair = draft?.pairs[Math.min(checkPair, Math.max(0, (draft?.pairs.length ?? 1) - 1))];
  const draftKey = JSON.stringify([draft, pair]);
  const { data: check, error: checkErr } = usePolling(
    () => (draft && pair ? api.checkStrategy(draft, pair.symbol, pair.asset_class) : Promise.resolve(null)),
    5000,
    [draftKey],
  );
  const { data: activity } = usePolling(
    () => (typeof sel === "number" ? api.strategyActivity(sel) : Promise.resolve([] as StrategyActivity[])),
    8000,
    [sel, saved?.enabled, saved?.open_trades],
  );

  if (!catalog || !draft) {
    return <div className="p-4 text-sm text-neutral-500">{listErr ? `Can't load strategies: ${listErr}` : "Loading…"}</div>;
  }
  const tfs = catalog.timeframes;
  const set = (patch: Partial<StrategyDraft>) => setDraft({ ...draft, ...patch });

  const pick = (s: StrategyView | "new") => {
    if (dirty && !window.confirm("Leave this strategy without saving your changes?")) return;
    setMsg(null);
    setCheckPair(0);
    if (s === "new") {
      setSel("new");
      setDraft(template(chartSymbol, assetClass));
    } else {
      setSel(s.id);
      setDraft(draftOf(s));
    }
  };

  const save = async (): Promise<StrategyView | null> => {
    setBusy(true);
    setMsg(null);
    try {
      const out = sel === "new" || sel === null ? await api.createStrategy(draft) : await api.updateStrategy(sel, draft);
      setSel(out.id);
      setDraft(draftOf(out));
      setMsg({ tone: "ok", text: out.enabled ? "Saved - the running strategy uses it from its next pass." : "Saved (stopped)." });
      return out;
    } catch (e) {
      setMsg({ tone: "err", text: e instanceof Error ? e.message : String(e) });
      return null;
    } finally {
      setBusy(false);
    }
  };

  const run = async (on: boolean) => {
    const s = dirty ? await save() : saved;
    if (!s) return;
    setBusy(true);
    try {
      const out = await api.runStrategy(s.id, on);
      setDraft(draftOf(out));
      setMsg({ tone: "ok", text: on ? "Running - on a demo / paper account only." : "Stopped. Its open trade (if any) stays on its stop and target." });
    } catch (e) {
      setMsg({ tone: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!saved || !window.confirm(`Delete the strategy "${saved.name}"?`)) return;
    setBusy(true);
    try {
      await api.deleteStrategy(saved.id);
      setSel(null);
      setDraft(null);
      setMsg(null);
    } catch (e) {
      setMsg({ tone: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const addPair = (symbol: string, ac: AssetClass) => {
    const s = symbol.trim();
    if (!s || draft.pairs.some((p) => p.symbol.toUpperCase() === s.toUpperCase())) return;
    set({ pairs: [...draft.pairs, { symbol: s, asset_class: ac }] });
  };

  const note = pair && saved?.state?.[pair.symbol]?.note;
  const sell = draft.mirror_short ? draft.entry_long : draft.entry_short;
  const summary = (c: StrategyCheckSet | null | undefined) => {
    if (!c) return "—";
    if (!c.groups.length) return "no rule";
    const best = Math.max(...c.groups.map((g) => g.conditions.filter((x) => x.ok === true).length));
    const of = c.groups.length === 1 ? `/${c.groups[0].conditions.length}` : "";
    return c.ok ? "fires now" : `${best}${of} hold`;
  };

  return (
    <div className="space-y-3 text-sm">
      {/* The strategies, each with its state. */}
      <div className="flex flex-wrap items-center gap-1.5">
        {(list ?? []).map((s) => (
          <button
            key={s.id}
            onClick={() => pick(s)}
            className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold transition ${
              sel === s.id ? "border-amber-400 bg-amber-500/15 text-amber-100" : "border-neutral-700 text-neutral-300 hover:border-neutral-500"
            }`}
            title={s.enabled ? "Running" : "Stopped"}
          >
            <span className={`h-2 w-2 rounded-full ${s.enabled ? "animate-pulse bg-bull" : "bg-neutral-600"}`} />
            {s.name}
            {s.open_trades > 0 && <span className="rounded bg-sky-500/20 px-1 text-sky-300">{s.open_trades} open</span>}
          </button>
        ))}
        <button
          onClick={() => pick("new")}
          className={`rounded-full border border-dashed px-3 py-1 text-xs ${sel === "new" ? "border-amber-400 text-amber-200" : "border-neutral-700 text-neutral-400 hover:text-neutral-200"}`}
        >
          + New strategy
        </button>
      </div>

      {/* What it trades. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-neutral-800 bg-neutral-900/40 p-2.5">
        <label className="flex items-center gap-1.5 text-xs text-neutral-400">
          Name
          <input
            value={draft.name}
            onChange={(e) => set({ name: e.target.value })}
            className="w-56 rounded border border-neutral-700 bg-neutral-950 px-2 py-0.5 text-sm text-neutral-100"
            maxLength={64}
          />
        </label>
        <span className="flex items-center gap-1.5 text-xs text-neutral-400" title="The strategy's own bar clock: at most one entry per closed bar, and the stop's ATR is read here">
          Base timeframe
          <span className="inline-flex rounded-md border border-neutral-700 bg-neutral-800/60 p-0.5">
            {tfs.map((t) => (
              <button key={t} onClick={() => set({ timeframe: t })} className={`rounded px-1.5 py-0.5 tabular-nums ${draft.timeframe === t ? "bg-amber-500 font-bold text-neutral-950" : "text-neutral-300 hover:bg-neutral-700"}`}>
                {t}
              </button>
            ))}
          </span>
        </span>
        <span className="inline-flex rounded-md border border-neutral-700 bg-neutral-800/60 p-0.5 text-xs" role="group" aria-label="Sides">
          {([["both", "Buy & sell"], ["long", "Buy only"], ["short", "Sell only"]] as const).map(([k, l]) => (
            <button key={k} onClick={() => set({ sides: k })} className={`rounded px-2 py-0.5 ${draft.sides === k ? "bg-neutral-600 text-white" : "text-neutral-400 hover:text-neutral-200"}`}>
              {l}
            </button>
          ))}
        </span>
        <span className="flex basis-full flex-wrap items-center gap-1.5 text-xs text-neutral-400">
          Pairs
          {draft.pairs.map((p, i) => (
            <span key={p.symbol} className="flex items-center gap-1 rounded-full bg-neutral-800 px-2 py-0.5 text-neutral-200">
              <button onClick={() => setCheckPair(i)} className={i === checkPair ? "font-semibold text-amber-200" : ""} title="Show the live read for this pair">
                {displaySymbol(p.symbol)}
              </button>
              <button onClick={() => set({ pairs: draft.pairs.filter((_, j) => j !== i) })} className="text-neutral-500 hover:text-bear" aria-label={`Remove ${p.symbol}`}>×</button>
            </span>
          ))}
          {chartSymbol && !draft.pairs.some((p) => p.symbol.toUpperCase() === chartSymbol.toUpperCase()) && (
            <button onClick={() => addPair(chartSymbol, assetClass)} className="rounded-full border border-dashed border-neutral-600 px-2 py-0.5 hover:text-neutral-200">
              + {displaySymbol(chartSymbol)} (the chart's)
            </button>
          )}
          <PairPicker added={draft.pairs} defaultClass={assetClass} chartSymbol={chartSymbol} onAdd={addPair} />
        </span>
      </div>

      {/* BUY */}
      <section>
        <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
          <h3 className="font-black tracking-wide text-bull">▲ BUY</h3>
          <span className="text-xs text-neutral-500">opens a long when every condition of a group holds (any one group is enough)</span>
          {draft.sides !== "short" && <span className={`ml-auto rounded px-1.5 text-xs font-semibold ${check?.long?.ok ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-400"}`}>now: {summary(check?.long)}</span>}
        </div>
        {draft.sides === "short"
          ? <p className="text-xs text-neutral-500">Off - this strategy only sells.{draft.mirror_short && " (Its SELL rules are these, mirrored - edit them here with “Buy & sell”.)"}</p>
          : <RuleSet where="long" groups={draft.entry_long} onChange={(g) => set({ entry_long: g })} catalog={catalog} tfs={tfs} baseTf={draft.timeframe} check={check?.long} />}
      </section>

      {/* SELL */}
      <section>
        <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
          <h3 className="font-black tracking-wide text-bear">▼ SELL</h3>
          <span className="inline-flex rounded-md border border-neutral-700 bg-neutral-800/60 p-0.5 text-xs">
            <button onClick={() => set({ mirror_short: true })} className={`rounded px-2 py-0.5 ${draft.mirror_short ? "bg-neutral-600 text-white" : "text-neutral-400"}`}>the BUY rules, mirrored</button>
            <button
              onClick={() => set({ mirror_short: false, entry_short: draft.entry_short.length ? draft.entry_short : draft.entry_long })}
              className={`rounded px-2 py-0.5 ${!draft.mirror_short ? "bg-neutral-600 text-white" : "text-neutral-400"}`}
            >
              my own SELL rules
            </button>
          </span>
          {draft.sides !== "long" && <span className={`ml-auto rounded px-1.5 text-xs font-semibold ${check?.short?.ok ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-400"}`}>now: {summary(check?.short)}</span>}
        </div>
        {draft.sides === "long" ? (
          <p className="text-xs text-neutral-500">Off - this strategy only buys.</p>
        ) : draft.mirror_short ? (
          <div className="space-y-1 rounded-lg border border-neutral-800 bg-neutral-900/30 p-2">
            {sell.length === 0 && <p className="text-xs text-neutral-500">Add BUY rules - the SELL ones are their mirror image.</p>}
            {sell.map((g, gi) => (
              <div key={gi} className="flex flex-wrap items-start gap-1.5 text-xs">
                <span className="mt-0.5 shrink-0 rounded bg-neutral-800 px-1.5 text-[10px] font-semibold uppercase text-neutral-500">{gi > 0 ? "or " : ""}group {gi + 1}</span>
                <span className="flex flex-1 flex-wrap gap-1">
                  {g.map((c, ci) => {
                    const st = check?.short?.groups[gi]?.conditions[ci];
                    return (
                      <span key={ci} className="flex items-center gap-1 rounded border border-neutral-800 bg-neutral-950/60 px-1.5 py-0.5 text-neutral-300">
                        {sentence(catalog.conditions.find((x) => x.type === c.type), "short", c.params)} <b className="text-sky-300">{c.tf}</b>
                        <State c={st} />
                      </span>
                    );
                  })}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <RuleSet where="short" groups={draft.entry_short} onChange={(g) => set({ entry_short: g })} catalog={catalog} tfs={tfs} baseTf={draft.timeframe} check={check?.short} />
        )}
      </section>

      {/* CLOSE */}
      <section>
        <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
          <h3 className="font-black tracking-wide text-sky-300">✕ CLOSE</h3>
          <span className="text-xs text-neutral-500">closes the trade - long or short - when every condition of a rule holds against it (any one rule is enough). The broker's stop and target stay as the backstop.</span>
        </div>
        <RuleSet
          where="exit"
          groups={draft.exit_rules}
          onChange={(g) => set({ exit_rules: g })}
          catalog={catalog}
          tfs={tfs}
          baseTf={draft.timeframe}
          checks={[{ set: check?.exit_long, prefix: "if long " }, { set: check?.exit_short, prefix: "if short " }]}
        />
      </section>

      {/* Money */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-neutral-800 bg-neutral-900/40 p-2.5">
        <Num label="Risk per trade" value={+(draft.risk_pct * 100).toFixed(2)} onChange={(v) => set({ risk_pct: v / 100 })} min={0.1} max={3} step={0.1} suffix="% of equity"
             title="What a stopped-out trade loses. The Risk Manager re-clamps it to the per-trade ceiling (RISK.md) whatever you type." />
        <Num label="Stop" value={draft.stop_atr} onChange={(v) => set({ stop_atr: v })} min={0.3} max={10} step={0.1} suffix={`× ATR of the ${draft.timeframe} bars`}
             title="The broker-side stop every order carries, pushed further out when the spread gate needs it" />
        <span className="flex items-center gap-2">
          {draft.target_r != null ? (
            <Num label="Target" value={draft.target_r} onChange={(v) => set({ target_r: v })} min={0.3} max={20} step={0.1} suffix="R" title="The broker-side target, in multiples of the stop distance" />
          ) : <span className="text-xs text-neutral-400">Target: none - the close rules exit</span>}
          <label className="flex items-center gap-1 text-xs text-neutral-500">
            <input type="checkbox" checked={draft.target_r == null} onChange={(e) => set({ target_r: e.target.checked ? null : 2 })} className="accent-amber-500" />
            no target
          </label>
        </span>
        <Num label="At most" value={draft.max_trades_day} onChange={(v) => set({ max_trades_day: Math.round(v) })} min={1} max={200} step={1} suffix="trades a day" />
        <Wait
          label="After a trade closes, re-enter the pair after"
          value={draft.cooldown_minutes}
          options={PAIR_WAITS}
          onChange={(v) => set({ cooldown_minutes: v })}
          title="The wait after ANY trade on the pair closes, before this strategy opens another there. RISK.md's default for other openers is 30 min; scalp mode uses no wait."
        />
        <Wait
          label="After a LOSING trade, the same side waits"
          value={draft.loss_cooldown_minutes}
          options={LOSS_WAITS}
          onChange={(v) => set({ loss_cooldown_minutes: v })}
          title="After a stopped-out trade, the same direction waits this long (the other direction doesn't). RISK.md's default is 180 min; it can't go under 5 here - that is the floor you confirmed, and it stops a rule that still holds after a stop-out from re-entering on the very next candle."
        />
        <label className="flex items-center gap-1.5 text-xs text-neutral-300" title="Off: the strategy only closes the trades it opened. On: its CLOSE rules also close YOUR trades on these pairs (the scalp ticket's, the chart's).">
          <input type="checkbox" checked={draft.manage_manual} onChange={(e) => set({ manage_manual: e.target.checked })} className="accent-amber-500" />
          its CLOSE rules also close my own trades on these pairs
        </label>
      </div>

      {/* Run it */}
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={() => void save()} disabled={busy || !dirty} className="btn btn-primary disabled:opacity-50">
          {sel === "new" ? "Save" : dirty ? "Save changes" : "Saved"}
        </button>
        {saved?.enabled ? (
          <button onClick={() => void run(false)} disabled={busy} className="btn border border-bear/60 bg-bear/15 text-bear hover:bg-bear/25">■ Stop</button>
        ) : (
          <button onClick={() => void run(true)} disabled={busy} className="btn border border-bull/60 bg-bull/15 text-bull hover:bg-bull/25"
                  title="Start it: from now on the backend checks it every few seconds and trades by it - on a demo / paper account only">
            ▶ {dirty ? "Save & run" : "Run"} (paper)
          </button>
        )}
        {saved && !saved.enabled && (
          <button onClick={() => void remove()} disabled={busy} className="btn btn-ghost text-neutral-400 hover:text-bear">Delete</button>
        )}
        {saved && (
          <span className="ml-auto flex flex-wrap items-center gap-3 text-xs text-neutral-400">
            <span className={saved.enabled ? "font-semibold text-bull" : ""}>{saved.enabled ? "● running" : "○ stopped"}</span>
            {saved.enabled && <span>checked {ago(saved.last_run_at)}</span>}
            {saved.enabled && (() => {
              const n = nextLook(draftOf(saved));
              return (
                <span className="tabular-nums text-sky-300" title="A running strategy is read about a second after each candle closes on any timeframe its rules use, and at least every 15 seconds for its +R and time exits.">
                  next look: the {n.tf} close, in {n.secs}s
                </span>
              );
            })()}
            <span>{saved.open_trades} open · {saved.trades_today} today</span>
            {note && <span className="text-neutral-300">{displaySymbol(pair!.symbol)}: {note}</span>}
          </span>
        )}
      </div>
      {msg && <div className={`rounded border px-3 py-1.5 text-xs ${msg.tone === "ok" ? "border-bull/40 bg-bull/10 text-bull" : "border-bear/40 bg-bear/10 text-bear"}`}>{msg.text}</div>}
      {checkErr && <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300">Live read unavailable: {checkErr}</div>}
      {check && !check.paper && (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300">
          This broker is a REAL account right now: a running strategy will not trade it (paper / demo only).
        </div>
      )}

      <p className="rounded-lg border border-neutral-800 bg-neutral-900/30 px-3 py-2 text-[11px] leading-relaxed text-neutral-500">
        How it trades: the backend reads a running strategy about a second after each candle closes on any timeframe its
        rules use - every 5 s with a 5s rule, every minute if they are all 1m - and at least every 15 s for its +R and time
        exits. It reads CLOSED bars only (the forming bar repaints),
        opens at most once per {draft.timeframe} bar, and closes on a bar that closed after the entry. Only on a demo / paper account.
        Every order carries a broker-side stop (and the target), and goes through the Risk Manager like any other: the per-trade
        ceiling, exposure, max open positions, one trade per side per pair, correlation, the spread gate. Its cooldowns are
        its own (set above): after a trade closes on a pair it re-enters {draft.cooldown_minutes ? `after ${draft.cooldown_minutes} min` : "with no wait"},
        and after a LOSING trade the same direction waits {draft.loss_cooldown_minutes} min - never under 5, so a rule that still
        holds after a stop-out can't re-enter on the very next candle and lose again. Automated entries also stand aside
        around high-impact news. None of these rules was backtested here: let it run on paper and read its
        results in the Journal before trusting it.
      </p>

      {typeof sel === "number" && (
        <section>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">Activity</h3>
          {(activity ?? []).length === 0 ? (
            <p className="text-xs text-neutral-600">Nothing yet. Opens, closes and refusals appear here.</p>
          ) : (
            <div className="max-h-64 overflow-y-auto">{(activity ?? []).map((a) => <ActivityRow key={a.id} a={a} />)}</div>
          )}
        </section>
      )}
    </div>
  );
}
