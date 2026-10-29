import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { displaySymbol } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import type { MonitorNote, MonitorRow, MonitorTf, PositionView } from "../types";
import { BIAS } from "./MarketMonitor";

// The server caches the 1m for 10s and the slow timeframes for minutes, so 15s keeps the 1m live
// without re-reading anything the cache would not have refreshed anyway.
const POLL_MS = 15_000;
// +DI and -DI must differ by at least this much, at ADX 20+, before either side "owns" the strength
// (the same rule as the Monitor).
const DI_MARGIN = 5;
// A timeframe "leans" one way only when that side leads by at least this many readings.
const LEAN_MARGIN = 2;

type Side = 1 | -1 | 0;

interface Read {
  side: Side;
  body: ReactNode;
  tip: string;
  /** Stretched (RSI 70+/30-): shown amber - extended, not a direction. */
  warn?: boolean;
  /** Faded: the reading exists but is weak. */
  dim?: boolean;
}

interface RowDef {
  key: string;
  label: string;
  hint: string;
  /** Counted in the per-timeframe score. Activity and zones are context, not a side. */
  counted: boolean;
  /** Shown in the compact view. */
  core: boolean;
  read: (t: MonitorTf) => Read;
}

const sideOf = (v: string | null | undefined): Side => (v === "up" ? 1 : v === "down" ? -1 : 0);
const arrow = (s: Side) => (s > 0 ? "▲" : s < 0 ? "▼" : "■");

const ROWS: RowDef[] = [
  {
    key: "trend", label: "Trend", counted: true, core: true,
    hint: "EMA 20 vs EMA 50 (and the 200 when all three stack) - the engine's own direction read.",
    read: (t) => {
      const s = sideOf(t.trend);
      return {
        side: s,
        dim: t.strength === "weak",
        body: <>{arrow(s)} {s > 0 ? "Up" : s < 0 ? "Down" : "Flat"}{t.ema_stacked && <span className="ml-0.5 opacity-70">≡</span>}</>,
        tip: `${t.trend ?? "?"}${t.ema_stacked ? " - EMA 20/50/200 fully stacked" : ""}${t.strength ? ` (${t.strength} trend)` : ""}`,
      };
    },
  },
  {
    key: "st", label: "Supertrend", counted: true, core: false,
    hint: "ATR trailing line: which side of it price is closing on.",
    read: (t) => {
      const s = sideOf(t.supertrend);
      return { side: s, body: s ? `${arrow(s)} ${s > 0 ? "Buy" : "Sell"}` : "—", tip: `Supertrend ${t.supertrend ?? "n/a"}` };
    },
  },
  {
    key: "macd", label: "MACD", counted: true, core: true,
    hint: "Histogram side (above / below zero), and whether it is building or fading. ✕ = signal-line cross on this bar.",
    read: (t) => {
      const h = t.macd_hist;
      if (h == null) return { side: 0, body: "—", tip: "MACD not available" };
      const s: Side = h > 0 ? 1 : h < 0 ? -1 : 0;
      const p = t.macd_hist_prev;
      const building = p != null && Math.sign(p) === Math.sign(h) && Math.abs(h) > Math.abs(p);
      const cross = t.macd_cross === 1 ? 1 : t.macd_cross === -1 ? -1 : 0;
      return {
        side: s,
        dim: !building && !cross,
        body: cross ? <>{arrow(cross as Side)} cross ✕</> : <>{arrow(s)} {building ? "building" : "fading"}</>,
        tip: `Histogram ${h.toPrecision(3)}${p != null ? ` (prev ${p.toPrecision(3)})` : ""}` +
          (cross ? ` - ${cross > 0 ? "bullish" : "bearish"} signal-line cross this bar` : ""),
      };
    },
  },
  {
    key: "rsi", label: "RSI", counted: true, core: true,
    hint: "14-period. Counts as up at 55+, down at 45-, neutral between. Amber = 70+ / 30- (stretched, either way). ↑↓ = turning since the last bar.",
    read: (t) => {
      const v = t.rsi;
      if (v == null) return { side: 0, body: "—", tip: "RSI not available" };
      const s: Side = v >= 55 ? 1 : v <= 45 ? -1 : 0;
      const turning = t.rsi_prev != null ? (v > t.rsi_prev + 0.5 ? "↑" : v < t.rsi_prev - 0.5 ? "↓" : "") : "";
      const ob = t.rsi_zone === "overbought";
      const os = t.rsi_zone === "oversold";
      // Rounded TOWARD 50 so the label never crosses a threshold the reading has not (as the Monitor).
      const shown = v >= 50 ? Math.floor(v) : Math.ceil(v);
      return {
        side: s,
        warn: ob || os,
        body: <>{shown}{turning && <span className="ml-0.5 opacity-70">{turning}</span>}{ob && " OB"}{os && " OS"}</>,
        tip: `RSI ${v.toFixed(1)}${t.rsi_prev != null ? ` (prev ${t.rsi_prev.toFixed(1)})` : ""}`,
      };
    },
  },
  {
    key: "adx", label: "ADX · DI", counted: true, core: false,
    hint: "ADX = trend strength (25+ strong, under 20 weak). +DI / −DI = which side owns it, shown only at ADX 20+ with a clear gap. ↗ building, ↘ fading.",
    read: (t) => {
      if (t.adx == null) return { side: 0, body: "—", tip: "ADX not available" };
      const own =
        t.plus_di != null && t.minus_di != null && t.adx >= 20 && Math.abs(t.plus_di - t.minus_di) >= DI_MARGIN
          ? (t.plus_di > t.minus_di ? 1 : -1) as Side
          : 0;
      return {
        side: own,
        dim: t.adx < 20,
        body: (
          <>
            {Math.floor(t.adx)}
            {t.adx_dir === "rising" && <span className="opacity-70">↗</span>}
            {t.adx_dir === "falling" && <span className="opacity-50">↘</span>}
            {own !== 0 && <span className="ml-1">{own > 0 ? "+DI" : "−DI"}</span>}
          </>
        ),
        tip: `ADX ${t.adx.toFixed(1)} (${t.strength ?? "?"})` +
          (t.plus_di != null && t.minus_di != null ? ` | +DI ${t.plus_di.toFixed(1)} / −DI ${t.minus_di.toFixed(1)}` : ""),
      };
    },
  },
  {
    key: "struct", label: "Structure", counted: true, core: false,
    hint: "Swing structure: HH·HL up, LH·LL down. CHoCH = price just closed beyond the last opposite swing - the structure has broken that way.",
    read: (t) => {
      if (t.choch_side) {
        const s = sideOf(t.choch_side);
        return { side: s, body: <>CHoCH {arrow(s)}</>, tip: `${s > 0 ? "Bullish" : "Bearish"} change of character: closed beyond the last swing ${s > 0 ? "high" : "low"}` };
      }
      const s = t.structure === "up" ? 1 : t.structure === "down" ? -1 : 0;
      return {
        side: s,
        body: s > 0 ? "HH·HL" : s < 0 ? "LH·LL" : "Range",
        tip: `Last swing high ${t.swing_high ?? "—"}, last swing low ${t.swing_low ?? "—"}`,
      };
    },
  },
  {
    key: "flow", label: "Buyers / sellers", counted: true, core: true,
    hint: "Share of the last 20 completed bars' volume that traded on UP bars (green) vs DOWN bars (red). 60%+ one way counts as pressure. MT5 tick volume - a proxy for who is pushing, not real order flow.",
    read: (t) => {
      const u = t.volume?.up_share;
      if (u == null) return { side: 0, body: "—", tip: "Not enough completed bars" };
      const s: Side = u >= 0.6 ? 1 : u <= 0.4 ? -1 : 0;
      const pct = Math.round(u * 100);
      return {
        side: s,
        body: (
          <span className="flex items-center justify-center gap-1">
            <span className="flex h-1.5 w-10 overflow-hidden rounded-full bg-bear/70" aria-hidden="true">
              <span className="h-full bg-bull" style={{ width: `${pct}%` }} />
            </span>
            <span>{s < 0 ? `${100 - pct}% S` : `${pct}% B`}</span>
          </span>
        ),
        tip: `${pct}% of recent volume on up bars, ${100 - pct}% on down bars`,
      };
    },
  },
  {
    key: "act", label: "Activity", counted: false, core: true,
    hint: "How busy the last completed bar was vs normal for that time (same hour on 1H/4H, same slot on 15M, last 20 bars on 1M/5M, last 20 sessions on 1D). Bold = 1.5x+, dim = 0.7x or less. ↑ building / ↓ fading.",
    read: (t) => {
      const v = t.volume;
      if (!v) return { side: 0, body: "—", tip: "Not enough completed bars" };
      return {
        side: 0,
        dim: v.level === "low",
        body: (
          <span className={v.level === "high" ? "font-semibold text-neutral-100" : undefined}>
            {v.ratio.toFixed(1)}×{v.trend === "expanding" ? "↑" : v.trend === "fading" ? "↓" : ""}
          </span>
        ),
        tip: `Last completed bar: ${v.ratio.toFixed(2)}x normal volume${v.trend ? `, ${v.trend}` : ""}`,
      };
    },
  },
  {
    key: "zone", label: "Zone", counted: false, core: false,
    hint: "Order blocks on that timeframe (the chart's overlay): inside one, or within 0.5 ATR of one. Cyan = demand, amber = supply.",
    read: (t) => {
      const z = t.zones;
      const kinds = z.inside_kinds?.length ? z.inside_kinds : z.inside ? [z.inside.kind] : [];
      const cls = (k: string) => (k === "demand" ? "text-cyan-300" : "text-amber-300");
      if (kinds.length) {
        return {
          side: 0,
          body: <>{kinds.map((k) => <span key={k} className={`${cls(k)} mr-1 last:mr-0`}>in {k === "demand" ? "D" : "S"}</span>)}</>,
          tip: `Price is inside a ${kinds.join(" and a ")} zone`,
        };
      }
      const near = (
        [
          z.demand?.dist_atr != null && z.demand.dist_atr <= 0.5 ? ["demand", z.demand.dist_atr] : null,
          z.supply?.dist_atr != null && z.supply.dist_atr <= 0.5 ? ["supply", z.supply.dist_atr] : null,
        ].filter(Boolean) as [string, number][]
      ).sort((a, b) => a[1] - b[1]);
      if (!near.length) return { side: 0, dim: true, body: "—", tip: "No zone within 0.5 ATR" };
      const [k, d] = near[0];
      return { side: 0, body: <span className={cls(k)}>near {k === "demand" ? "D" : "S"} {d.toFixed(1)}</span>, tip: `${k} zone ${d.toFixed(1)} ATR away` };
    },
  },
];

type Score = { bull: number; bear: number; n: number; lean: Side };

function score(t: MonitorTf | null | undefined): Score | null {
  if (!t) return null;
  let bull = 0;
  let bear = 0;
  let n = 0;
  for (const r of ROWS) {
    if (!r.counted) continue;
    n++;
    const s = r.read(t).side;
    if (s > 0) bull++;
    else if (s < 0) bear++;
  }
  const lean: Side = bull - bear >= LEAN_MARGIN ? 1 : bear - bull >= LEAN_MARGIN ? -1 : 0;
  return { bull, bear, n, lean };
}

const tint = (r: Read) =>
  r.warn
    ? "bg-warn/15 text-warn"
    : r.side > 0
      ? "bg-bull/10 text-bull"
      : r.side < 0
        ? "bg-bear/10 text-bear"
        : "text-neutral-400";

const leanCls = (s: Score | null, strong = false) =>
  !s
    ? "bg-neutral-800/60 text-neutral-600"
    : s.lean > 0
      ? strong ? "bg-bull/20 text-bull" : "bg-bull/15 text-bull"
      : s.lean < 0
        ? strong ? "bg-bear/20 text-bear" : "bg-bear/15 text-bear"
        : "bg-neutral-800 text-neutral-400";

// ✓ / ✗ against the side you are holding; nothing when flat or the timeframe has no lean.
const mineMark = (s: Score | null, posSide: Side) =>
  s && posSide !== 0 && s.lean !== 0 ? (s.lean === posSide ? " ✓" : " ✗") : "";

function ago(iso: string | undefined, now: number): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
}

/**
 * The Monitor, for the charted pair only, live: every indicator the engine reads on 1M / 5M / 15M /
 * 1H / 4H / 1D. Above the chart it is ONE line - each timeframe's verdict (green leans up, red down) and,
 * with a position open, whether each one is with you (✓) or against you (✗). Clicking it opens the
 * full read as a popover OVER the chart, so the chart never moves and the read is gone the moment you
 * go back to the candles.
 *
 * Read-out only: nothing here opens, sizes or closes a trade.
 */
export function PairMonitor({
  symbol,
  assetClass,
  timeframe,
  selectable,
  onTimeframe,
  position,
}: {
  symbol: string;
  assetClass: string;
  /** The chart's timeframe - its column is highlighted. */
  timeframe: string;
  /** Timeframes the chart can switch to; those column headers are clickable. */
  selectable: string[];
  onTimeframe: (tf: string) => void;
  position: PositionView | null;
}) {
  const { data, error } = usePolling(() => api.monitorPair(symbol, assetClass), POLL_MS, [symbol, assetClass]);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const c = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(c);
  }, []);

  // Out of the way the moment you go back to the chart: outside click or Esc closes it.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // The poll keeps the previous pair's answer until the new one lands - never show it under this name.
  const fresh = data && data.row.symbol.toUpperCase() === symbol.toUpperCase() ? data : null;
  const row = fresh?.row ?? null;
  const tfs = fresh?.timeframes ?? ["1m", "5m", "15m", "1h", "4h", "1d"];
  const scores = tfs.map((tf) => score(row?.tfs[tf]));
  const posSide: Side = position ? (position.direction === "long" ? 1 : -1) : 0;
  const read = scores.filter((s): s is Score => s != null);
  const withYou = posSide ? read.filter((s) => s.lean === posSide).length : 0;
  const against = posSide ? read.filter((s) => s.lean === -posSide).length : 0;
  const bias = row ? BIAS[row.bias] : null;
  const watch = (row?.notes ?? []).filter((n) => n.tone !== "info").length;
  const posCls = against === 0 ? "bg-bull/20 text-bull" : against >= withYou ? "bg-bear/20 text-bear" : "bg-warn/20 text-warn";

  return (
    <div ref={rootRef} className="relative mb-2 shrink-0">
      {/* ── the strip: one line, always visible ── */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title="Every indicator on 1M to 1D for this pair - click for the full read"
        className={`flex w-full flex-wrap items-center gap-x-1.5 gap-y-1 rounded-md border px-2 py-1 text-left text-xs transition ${
          open ? "border-brand-500/60 bg-neutral-900" : "border-neutral-800 bg-neutral-900/60 hover:border-neutral-600"
        }`}
      >
        <span className="mr-0.5 font-semibold text-neutral-500">Timeframes</span>
        {tfs.map((tf, i) => {
          const s = scores[i];
          return (
            <span
              key={tf}
              title={s ? `${tf.toUpperCase()}: ${s.bull} up · ${s.bear} down, of ${s.n} readings` : undefined}
              className={`inline-flex items-center gap-1 rounded px-1.5 py-px font-semibold tabular-nums ${leanCls(s)} ${
                tf === timeframe ? "ring-1 ring-inset ring-brand-500/70" : ""
              }`}
            >
              <span className="font-medium opacity-60">{tf.toUpperCase()}</span>
              {!s ? (fresh ? "—" : "…") : s.lean > 0 ? `▲ ${s.bull}/${s.n}` : s.lean < 0 ? `▼ ${s.bear}/${s.n}` : "≈"}
              {mineMark(s, posSide)}
            </span>
          );
        })}
        {bias && (
          <span className={`hidden rounded-md px-1.5 py-px font-semibold sm:inline ${bias.cls}`} title={`${bias.hint} (1H, 4H, 1D)`}>
            {bias.label}
          </span>
        )}
        {posSide !== 0 && read.length > 0 && (
          <span className={`rounded px-1.5 py-px font-bold ${posCls}`} title="Timeframes leaning WITH your position vs against it">
            {posSide > 0 ? "LONG" : "SHORT"} {withYou}✓ {against}✗
          </span>
        )}
        {row?.market_open === false && <span className="text-neutral-500">⏸ closed</span>}
        {row?.events_soon && <span className="font-medium text-warn" title={row.events_soon}>⚑ news</span>}
        {watch > 0 && <span className="font-medium text-orange-300">! {watch} to watch</span>}
        {error && !fresh && <span className="text-bear">readings unavailable</span>}
        <span className="ml-auto text-neutral-500">{open ? "Close ▴" : "Details ▾"}</span>
      </button>

      {/* ── the full read: over the chart, never pushing it down ── */}
      {open && (
        <div
          role="dialog"
          aria-label={`${displaySymbol(symbol)} across timeframes`}
          className="absolute left-0 top-full z-50 mt-1 max-h-[70vh] w-[calc(100vw-2rem)] max-w-[60rem] overflow-y-auto rounded-lg border border-neutral-700 bg-neutral-900/95 p-3 shadow-xl backdrop-blur-sm"
        >
          <FullRead
            symbol={symbol}
            row={row}
            tfs={tfs}
            scores={scores}
            posSide={posSide}
            withYou={withYou}
            against={against}
            posCls={posCls}
            timeframe={timeframe}
            selectable={selectable}
            onTimeframe={onTimeframe}
            updated={fresh ? ago(fresh.generated_at, now) : error ? "" : "reading…"}
            error={fresh ? null : error}
            onClose={() => setOpen(false)}
          />
        </div>
      )}
    </div>
  );
}

function FullRead({
  symbol, row, tfs, scores, posSide, withYou, against, posCls, timeframe, selectable, onTimeframe, updated, error, onClose,
}: {
  symbol: string;
  row: MonitorRow | null;
  tfs: string[];
  scores: (Score | null)[];
  posSide: Side;
  withYou: number;
  against: number;
  posCls: string;
  timeframe: string;
  selectable: string[];
  onTimeframe: (tf: string) => void;
  updated: string;
  error: string | null;
  onClose: () => void;
}) {
  const [compact, setCompact] = useLocalStorage("ta.pairMonitor.compact", false);
  const [legend, setLegend] = useState(false);
  const [allNotes, setAllNotes] = useState(false);
  const rows = compact ? ROWS.filter((r) => r.core) : ROWS;
  const read = scores.filter((s): s is Score => s != null);
  const leanUp = read.filter((s) => s.lean > 0).length;
  const leanDown = read.filter((s) => s.lean < 0).length;
  const bias = row ? BIAS[row.bias] : null;
  const notes = row?.notes ?? [];

  return (
    <>
      {/* ── header: the one-line answer ── */}
      <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className="font-semibold text-neutral-200">{displaySymbol(symbol)} across timeframes</span>
        {bias && (
          <span className={`rounded-md px-1.5 py-px font-semibold ${bias.cls}`} title={`${bias.hint} (1H, 4H, 1D)`}>
            {bias.label}
          </span>
        )}
        {read.length > 0 && (
          <span className="text-neutral-400" title="A timeframe leans one way when that side leads by 2+ of the 7 counted readings">
            leaning <span className="font-semibold text-bull">▲ {leanUp}</span>
            {" · "}
            <span className="font-semibold text-bear">▼ {leanDown}</span>
            {" "}of {read.length}
          </span>
        )}
        {posSide !== 0 && read.length > 0 && (
          <span
            className={`rounded px-1.5 py-px font-bold ${posCls}`}
            title="Timeframes leaning WITH your position vs against it. Against on the timeframes you trade is the exit question."
          >
            Your {posSide > 0 ? "LONG" : "SHORT"}: {withYou} with · {against} against
          </span>
        )}
        {row?.market_open === false && (
          <span className="rounded bg-neutral-800 px-1.5 py-px text-neutral-400">⏸ closed - last session&apos;s readings</span>
        )}
        {row?.events_soon && (
          <span className="rounded bg-warn/15 px-1.5 py-px font-medium text-warn" title={row.events_soon}>⚑ news</span>
        )}
        <span className="ml-auto flex items-center gap-2 text-neutral-500">
          <span className="tabular-nums">{updated}</span>
          <button onClick={() => setCompact((v) => !v)} className="hover:text-neutral-200" title={compact ? "Show every reading" : "Show only trend, MACD, RSI, buyers/sellers and activity"}>
            {compact ? "More" : "Less"}
          </button>
          <button onClick={() => setLegend((v) => !v)} aria-expanded={legend} className="hover:text-neutral-200">
            {legend ? "Hide guide" : "How to read"}
          </button>
          <button onClick={onClose} aria-label="Close" className="text-sm leading-none hover:text-neutral-200">
            ✕
          </button>
        </span>
      </div>

      {legend && (
        <dl className="mb-2 grid gap-1 rounded border border-neutral-800 bg-neutral-950/60 p-2 text-[11px] md:grid-cols-2">
          <div className="flex gap-2"><dt className="w-24 shrink-0 font-semibold text-neutral-300">Colours</dt><dd className="text-neutral-400">Green reads up, red reads down, grey neutral, amber stretched (RSI 70+ / 30-). Faded = the reading is weak.</dd></div>
          <div className="flex gap-2"><dt className="w-24 shrink-0 font-semibold text-neutral-300">Score</dt><dd className="text-neutral-400">How many of the 7 counted readings (trend, Supertrend, MACD, RSI, ADX·DI, structure, buyers/sellers) point up vs down. A lead of 2+ is a lean. With a position open, ✓ = that timeframe leans your way, ✗ = against you.</dd></div>
          {ROWS.map((r) => (
            <div key={r.key} className="flex gap-2"><dt className="w-24 shrink-0 font-semibold text-neutral-300">{r.label}</dt><dd className="text-neutral-400">{r.hint}{!r.counted && " Not counted in the score."}</dd></div>
          ))}
          <div className="flex gap-2"><dt className="w-24 shrink-0 font-semibold text-neutral-300">Columns</dt><dd className="text-neutral-400">The chart&apos;s timeframe is highlighted; click a header to switch the chart to it. Bias is read from 1H / 4H / 1D only.</dd></div>
        </dl>
      )}

      {error && <div className="mb-2 text-xs text-bear">Readings unavailable: {error}</div>}

      <div className="flex flex-col gap-3 md:flex-row">
        {/* ── the grid ── */}
        <div className="min-w-0 flex-1 overflow-x-auto">
          <table className="w-full min-w-[30rem] border-collapse text-[11px] tabular-nums">
            <thead>
              <tr className="text-neutral-500">
                <th className="w-28 px-1.5 py-0.5 text-left font-medium" />
                {tfs.map((tf) => {
                  const active = tf === timeframe;
                  const can = selectable.includes(tf) && !active;
                  return (
                    <th key={tf} className="px-0.5 py-0.5 font-semibold">
                      <button
                        disabled={!can}
                        onClick={() => onTimeframe(tf)}
                        title={active ? "The chart's timeframe" : can ? `Switch the chart to ${tf}` : undefined}
                        className={`w-full rounded px-1 py-0.5 ${
                          active ? "bg-brand-600/30 text-white" : can ? "hover:bg-neutral-800 hover:text-neutral-200" : "cursor-default"
                        }`}
                      >
                        {tf.toUpperCase()}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {/* Score: the per-timeframe verdict, against your side when you are in a trade. */}
              <tr className="border-b border-neutral-800">
                <td className="px-1.5 py-1 font-semibold text-neutral-300" title="Counted readings pointing up vs down. With a position: ✓ leans your way, ✗ against.">
                  Score
                </td>
                {scores.map((s, i) => (
                  <td key={tfs[i]} className="px-0.5 py-1">
                    {!s ? (
                      <div className="text-center text-neutral-600">{row ? "no data" : "…"}</div>
                    ) : (
                      <div
                        className={`rounded px-1 py-0.5 text-center font-bold ${leanCls(s, true)} ${
                          tfs[i] === timeframe ? "ring-1 ring-inset ring-brand-500/60" : ""
                        }`}
                        title={`${s.bull} up · ${s.bear} down · ${s.n - s.bull - s.bear} neutral, of ${s.n}`}
                      >
                        {s.lean > 0 ? `▲ ${s.bull}/${s.n}` : s.lean < 0 ? `▼ ${s.bear}/${s.n}` : `≈ ${s.bull}·${s.bear}`}
                        {mineMark(s, posSide)}
                      </div>
                    )}
                  </td>
                ))}
              </tr>
              {rows.map((r) => (
                <tr key={r.key} className="border-b border-neutral-800/50 last:border-b-0">
                  <td className="px-1.5 py-0.5 text-neutral-400" title={r.hint}>{r.label}</td>
                  {tfs.map((tf) => {
                    const t = row?.tfs[tf];
                    if (!t) return <td key={tf} className="px-0.5 py-0.5 text-center text-neutral-700">—</td>;
                    const v = r.read(t);
                    return (
                      <td key={tf} className="px-0.5 py-0.5">
                        <div
                          title={`${tf.toUpperCase()} ${r.label}: ${v.tip}`}
                          className={`cursor-help whitespace-nowrap rounded px-1 py-px text-center ${tint(v)} ${v.dim ? "opacity-60" : ""}`}
                        >
                          {v.body}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* ── what to watch ── */}
        {notes.length > 0 && (
          <div className="shrink-0 border-neutral-800 text-xs md:w-64 md:border-l md:pl-3">
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-neutral-500">What to watch</div>
            <Notes notes={allNotes ? notes : notes.slice(0, 4)} />
            {notes.length > 4 && (
              <button onClick={() => setAllNotes((v) => !v)} className="mt-0.5 pl-4 text-[11px] text-neutral-500 hover:text-neutral-300">
                {allNotes ? "show less" : `+${notes.length - 4} more`}
              </button>
            )}
          </div>
        )}
      </div>
    </>
  );
}

const TONE: Record<MonitorNote["tone"], { icon: string; cls: string }> = {
  warn: { icon: "⚑", cls: "text-warn" },
  caution: { icon: "!", cls: "text-orange-300" },
  info: { icon: "•", cls: "text-neutral-400" },
};

function Notes({ notes }: { notes: MonitorNote[] }) {
  return (
    <ul className="space-y-0.5">
      {notes.map((n, i) => (
        <li key={i} className="flex gap-1.5 leading-snug">
          <span className={`w-2.5 shrink-0 text-center font-bold ${TONE[n.tone].cls}`} aria-hidden="true">{TONE[n.tone].icon}</span>
          <span className={n.tone === "info" ? "text-neutral-400" : "text-neutral-200"}>{n.text}</span>
        </li>
      ))}
    </ul>
  );
}
