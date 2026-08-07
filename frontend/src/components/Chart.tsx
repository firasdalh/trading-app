import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickData,
  ColorType,
  createChart,
  HistogramData,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  LineData,
  LineStyle,
  Logical,
  LogicalRange,
  SeriesMarker,
  Time,
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";
import { api } from "../api/client";
import { ScenarioCard } from "./ScenarioCard";
import { fmtPrice } from "../format";
import type { AiScenarioRead, AssetClass, Candle, MarketContext, PositionView, TradeProposal } from "../types";
import type { LiveQuote } from "../hooks/useQuoteSocket";

// Persist a small chart UI toggle across refreshes AND pair changes (localStorage-backed useState).
function usePersisted<T>(key: string, initial: T, merge = false) {
  const [v, setV] = useState<T>(() => {
    try {
      const s = localStorage.getItem(key);
      if (s === null) return initial;
      const parsed = JSON.parse(s) as T;
      // merge=true: overlay stored keys onto the defaults so a NEW default key (e.g. a new EMA
      // period) appears for existing users instead of being masked by their older stored object.
      if (merge && parsed && typeof parsed === "object" && initial && typeof initial === "object") {
        return { ...(initial as object), ...(parsed as object) } as T;
      }
      return parsed;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(v));
    } catch {
      /* storage unavailable — non-fatal */
    }
  }, [key, v]);
  return [v, setV] as const;
}

// Drag a floating panel around by its header. The position is remembered: a panel you have to
// shove out of the way every time you open it is a panel you stop opening.
//
// The panel starts anchored with CSS (top-right). On the first drag we switch it to explicit
// left/top at wherever it currently sits — computing that from its real rectangle, so it doesn't
// jump on the first pixel of the drag.
function useDragPanel(key: string, rootRef: React.RefObject<HTMLDivElement | null>) {
  const [pos, setPos] = usePersisted<{ x: number; y: number } | null>(key, null);
  const grab = useRef<{ dx: number; dy: number; el: HTMLElement } | null>(null);

  const onPointerDown = (e: React.PointerEvent<HTMLElement>) => {
    // Buttons living in the header (refresh, close) must still be clickable.
    if ((e.target as HTMLElement).closest("button")) return;
    const panel = e.currentTarget.closest("[data-panel]") as HTMLElement | null;
    const root = rootRef.current;
    if (!panel || !root) return;
    const pr = panel.getBoundingClientRect();
    const rr = root.getBoundingClientRect();
    grab.current = { dx: e.clientX - pr.left, dy: e.clientY - pr.top, el: panel };
    setPos({ x: pr.left - rr.left, y: pr.top - rr.top });
    e.currentTarget.setPointerCapture(e.pointerId);
    e.preventDefault();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLElement>) => {
    const g = grab.current;
    const root = rootRef.current;
    if (!g || !root) return;
    const rr = root.getBoundingClientRect();
    // Always leave a strip of the header reachable. Drag it fully off the edge and the only way
    // back would be clearing storage.
    const KEEP = 80;
    const x = Math.min(Math.max(e.clientX - rr.left - g.dx, KEEP - g.el.offsetWidth), rr.width - KEEP);
    const y = Math.min(Math.max(e.clientY - rr.top - g.dy, 0), Math.max(0, rr.height - 30));
    setPos({ x, y });
  };

  const onPointerUp = (e: React.PointerEvent<HTMLElement>) => {
    grab.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };

  return {
    style: pos ? ({ left: pos.x, top: pos.y, right: "auto", bottom: "auto" } as const) : undefined,
    moved: pos != null,
    reset: () => setPos(null),
    handle: {
      onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp,
      onDoubleClick: () => setPos(null),
    },
  };
}

// --- Hand-drawn lines ------------------------------------------------------------------------
// Lines you draw yourself, kept per PAIR — not per timeframe. A drawing is stored as absolute time
// + price, so a trendline drawn on the 1h is the same real line on the 5m and the 4h; storing it
// per timeframe would mean drawing the same level five times over.
//
// They live in localStorage, not the database: they're personal chart annotations, they must
// survive a refresh, and nothing on the server ever needs to read them.
//
// Trend lines and pen strokes are painted on a CANVAS over the chart, not added as chart series.
// A series forces every point onto a candle, which is why lines landed next to where they were
// drawn instead of on it. On the canvas a point is kept as (time, price) and converted back to
// pixels each frame: it sits exactly where you put it, tracks pan/zoom, and can curve freely.
type Drawing =
  | { id: string; kind: "h"; price: number; color: string }
  | { id: string; kind: "t"; t1: number; p1: number; t2: number; p2: number; color: string }
  | { id: string; kind: "f"; pts: { t: number; p: number }[]; color: string };

// Cycled so consecutive drawings are visually separable when several sit close together.
const DRAW_COLORS = ["#38bdf8", "#f59e0b", "#a78bfa", "#22c55e", "#ef4444"];
const drawKey = (symbol: string) => `chart.draw.${symbol.toUpperCase()}`;
const newDrawId = () => `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

function readDrawings(key: string): Drawing[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || "[]");
    return Array.isArray(parsed) ? (parsed as Drawing[]) : [];
  } catch {
    return [];
  }
}

// Deliberately NOT usePersisted(): that hook's key is fixed, and useState's initialiser runs once —
// with a per-symbol key, switching pairs would write the previous pair's drawings under the new
// pair's key and lose both. Here the key change re-reads, and every save writes straight through.
function useDrawings(symbol: string) {
  const key = drawKey(symbol);
  const [items, setItems] = useState<Drawing[]>(() => readDrawings(key));
  useEffect(() => setItems(readDrawings(key)), [key]);
  const save = useCallback(
    (next: (prev: Drawing[]) => Drawing[]) => {
      setItems((prev) => {
        const out = next(prev);
        try {
          localStorage.setItem(key, JSON.stringify(out));
        } catch {
          /* storage unavailable — the drawing still shows, it just won't survive a refresh */
        }
        return out;
      });
    },
    [key],
  );
  return [items, save] as const;
}

// Decimals an instrument actually uses (EURUSD 5, JPY pairs 3, gold 2, indices ~1), inferred from the
// data so the right price axis + last-value label format correctly. Lightweight-charts otherwise
// defaults to 2 decimals, which shows "1.10" for EURUSD instead of "1.10234".
function inferDecimals(candles: Candle[]): number {
  // Up to 8 decimals so crypto-ratio pairs (ETHBTC ~0.0280025 = 7dp) and low-priced coins format
  // fully, while FX (5), JPY (3), metals (2) and indices (1) resolve to their own precision first.
  const sample = candles.slice(-200).map((c) => c.close).filter((p) => Number.isFinite(p) && p > 0);
  if (!sample.length) return 2;
  for (let d = 0; d <= 8; d++) {
    const m = 10 ** d;
    if (sample.every((p) => Math.abs(p * m - Math.round(p * m)) <= 1e-6 * Math.max(1, p * m))) return d;
  }
  return 8;
}

export interface ArmedLevel {
  id: number;
  symbol: string;
  direction: string;
  order_type: string;
  trigger_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  // BREAK-AND-RETEST (two-stage). `break_level` must CLOSE-break before `trigger_price` — the
  // retest entry — goes live. `break_confirmed_at` says which stage the setup is in.
  break_level?: number | null;
  break_confirmed_at?: string | null;
}

interface Props {
  symbol: string;
  assetClass: AssetClass;
  timeframe: string;
  proposal: TradeProposal | null;
  liveQuote: LiveQuote | null;
  positions: PositionView[] | null;
  armed?: ArmedLevel[];
  // Commit a dragged SL/TP for the charted symbol's open position (null = leave that one unchanged).
  onSetSlTp?: (sl: number | null, tp: number | null) => void;
  // Commit a dragged trigger/SL/TP for an armed setup (only the changed level is sent).
  onSetArmedLevels?: (id: number, levels: { trigger_price?: number; stop_loss?: number; take_profit?: number }) => void;
  // The AI scenario's cited S/R to plot on the chart — lifted to the Dashboard so BOTH the floating
  // scenario card AND the Run-analysis scenario card toggle the same lines. null = hidden.
  scenLevels?: { support: number | null; resistance: number | null; target: number | null; invalidation: number | null } | null;
  // Full screen (owned by the Dashboard so the position strip is included).
  isFullscreen?: boolean;
  onToggleFullscreen?: () => void;
  scenLevelsShown?: boolean;
  onToggleScenLevels?: (levels: { support: number | null; resistance: number | null; target: number | null; invalidation: number | null } | null) => void;
}

const EMA_CONFIG = [
  { period: 10, color: "#22d3ee" },   // the RSI-Over confirmation line
  { period: 20, color: "#e879f9" },
  { period: 50, color: "#f59e0b" },
  { period: 100, color: "#eab308" },
  { period: 200, color: "#2962ff" },
];

interface Legend {
  open: number;
  high: number;
  low: number;
  close: number;
}

function ema(values: number[], period: number): (number | undefined)[] {
  const k = 2 / (period + 1);
  const out: (number | undefined)[] = [];
  let prev = 0;
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) out.push(undefined);
    else if (i === period - 1) {
      prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
      out.push(prev);
    } else {
      prev = values[i] * k + prev * (1 - k);
      out.push(prev);
    }
  }
  return out;
}

// Wilder's RSI, aligned to closes (null during warmup).
function rsiCalc(closes: number[], period = 14): (number | null)[] {
  const res: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return res;
  let gains = 0;
  let losses = 0;
  for (let i = 1; i <= period; i++) {
    const ch = closes[i] - closes[i - 1];
    if (ch >= 0) gains += ch;
    else losses -= ch;
  }
  let ag = gains / period;
  let al = losses / period;
  res[period] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  for (let i = period + 1; i < closes.length; i++) {
    const ch = closes[i] - closes[i - 1];
    ag = (ag * (period - 1) + (ch > 0 ? ch : 0)) / period;
    al = (al * (period - 1) + (ch < 0 ? -ch : 0)) / period;
    res[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }
  return res;
}

// MACD: macd = EMA(fast) − EMA(slow); signal = EMA(signalP) of macd; hist = macd − signal.
// Returns full-length arrays aligned to the closes (null during warmup) so the sub-pane lines up
// bar-for-bar with the main chart. Reuses the SMA-seeded ema() above for both stages.
function macdCalc(closes: number[], fast = 12, slow = 26, signalP = 9) {
  const emaFast = ema(closes, fast);
  const emaSlow = ema(closes, slow);
  const macd: (number | null)[] = closes.map((_, i) =>
    emaFast[i] !== undefined && emaSlow[i] !== undefined ? (emaFast[i] as number) - (emaSlow[i] as number) : null,
  );
  const signal: (number | null)[] = new Array(closes.length).fill(null);
  const firstDefined = macd.findIndex((v) => v !== null);
  if (firstDefined >= 0) {
    const sig = ema(macd.slice(firstDefined) as number[], signalP);
    for (let j = 0; j < sig.length; j++) {
      if (sig[j] !== undefined) signal[firstDefined + j] = sig[j] as number;
    }
  }
  const hist: (number | null)[] = closes.map((_, i) =>
    macd[i] !== null && signal[i] !== null ? (macd[i] as number) - (signal[i] as number) : null,
  );
  return { macd, signal, hist };
}

// SuperTrend (ATR bands that flip with trend). Returns, per bar, the line value and direction
// (1 = up/bullish support below price, -1 = down/bearish resistance above price); null in warmup.
// Params: ATR period 10, multiplier 2.7, Wilder-smoothed ATR.
function superTrend(candles: Candle[], period = 10, mult = 2.7): { st: (number | null)[]; dir: number[] } {
  const n = candles.length;
  const st: (number | null)[] = new Array(n).fill(null);
  const dir: number[] = new Array(n).fill(0);
  if (n < period + 1) return { st, dir };
  const tr = (i: number) =>
    Math.max(
      candles[i].high - candles[i].low,
      Math.abs(candles[i].high - candles[i - 1].close),
      Math.abs(candles[i].low - candles[i - 1].close),
    );
  const atr: number[] = new Array(n).fill(0);
  let seed = 0;
  for (let i = 1; i <= period; i++) seed += tr(i);
  atr[period] = seed / period;
  for (let i = period + 1; i < n; i++) atr[i] = (atr[i - 1] * (period - 1) + tr(i)) / period;

  let finalUpper = 0;
  let finalLower = 0;
  let prevDir = 1;
  for (let i = period; i < n; i++) {
    const hl2 = (candles[i].high + candles[i].low) / 2;
    const bu = hl2 + mult * atr[i];
    const bl = hl2 - mult * atr[i];
    if (i === period) {
      finalUpper = bu;
      finalLower = bl;
      prevDir = 1;
      dir[i] = 1;
      st[i] = bl;
      continue;
    }
    const prevUpper = finalUpper;
    const prevLower = finalLower;
    finalUpper = bu < prevUpper || candles[i - 1].close > prevUpper ? bu : prevUpper;
    finalLower = bl > prevLower || candles[i - 1].close < prevLower ? bl : prevLower;
    let d = prevDir;
    if (prevDir === 1 && candles[i].close < finalLower) d = -1;
    else if (prevDir === -1 && candles[i].close > finalUpper) d = 1;
    dir[i] = d;
    prevDir = d;
    st[i] = d === 1 ? finalLower : finalUpper;
  }
  return { st, dir };
}

// Pullback detector layered on SuperTrend. A pullback is NOTICED only when BOTH trigger conditions
// hold: in an UPTREND (green) a full candle closes BELOW EMA20 AND RSI < 53 (a dip → buy-the-dip);
// in a DOWNTREND (red) a full candle closes ABOVE EMA20 AND RSI > 46 (a bounce → sell-the-rally).
// Confidence (max 100): trend +25 · (the EMA20-close + RSI trigger together) +35 · volume falling
// +15 · engulfing +10 · structure (higher-low / lower-high) intact +15. Returns qualifying bars
// (score >= 70), one per contiguous pullback (its strongest bar) — an EARLY entry.
function pullbackSignals(candles: Candle[]): { i: number; score: number; bullish: boolean }[] {
  const n = candles.length;
  const out: { i: number; score: number; bullish: boolean }[] = [];
  if (n < 30) return out;
  const closes = candles.map((c) => c.close);
  const ema20 = ema(closes, 20);
  const rsi = rsiCalc(closes, 14);
  const { dir } = superTrend(candles);
  const L = 3; // swing-pivot half-window
  const pivLow: boolean[] = new Array(n).fill(false);
  const pivHigh: boolean[] = new Array(n).fill(false);
  for (let j = L; j < n - L; j++) {
    let lo = true;
    let hi = true;
    for (let k = j - L; k <= j + L; k++) {
      if (candles[k].low < candles[j].low) lo = false;
      if (candles[k].high > candles[j].high) hi = false;
    }
    pivLow[j] = lo;
    pivHigh[j] = hi;
  }
  const volAvg = (i: number) => {
    let s = 0;
    let c = 0;
    for (let k = Math.max(0, i - 10); k < i; k++) {
      s += candles[k].volume;
      c++;
    }
    return c ? s / c : 0;
  };
  const lastTwo = (arr: boolean[], upto: number) => {
    const idx: number[] = [];
    for (let j = upto; j >= 0 && idx.length < 2; j--) if (arr[j]) idx.push(j);
    return idx;
  };
  const scoreAt = (i: number): { score: number; bullish: boolean } | null => {
    const e = ema20[i];
    const r = rsi[i];
    if (e === undefined || r === null || dir[i] === 0) return null;
    const c = candles[i];
    const p = candles[i - 1];
    if (dir[i] === 1) {
      // Trigger (both required): a full candle closed BELOW EMA20 AND RSI < 53.
      if (c.close >= e || r >= 53) return null;
      let s = 25 + 35; // trend +25 ; (full candle below EMA20 AND RSI < 53) +35
      if (c.volume < volAvg(i)) s += 15;
      if (c.close > c.open && p.close < p.open && c.close >= p.open && c.open <= p.close) s += 10; // bull engulf
      const piv = lastTwo(pivLow, i - L);
      if (piv.length === 2 && candles[piv[0]].low > candles[piv[1]].low) s += 15; // higher-low intact
      return { score: s, bullish: true };
    }
    // Downtrend mirror trigger (both required): a full candle closed ABOVE EMA20 AND RSI > 46.
    if (c.close <= e || r <= 46) return null;
    let s = 25 + 35; // trend +25 ; (full candle above EMA20 AND RSI > 46) +35
    if (c.volume < volAvg(i)) s += 15;
    if (c.close < c.open && p.close > p.open && c.close <= p.open && c.open >= p.close) s += 10; // bear engulf
    const piv = lastTwo(pivHigh, i - L);
    if (piv.length === 2 && candles[piv[0]].high < candles[piv[1]].high) s += 15; // lower-high intact
    return { score: s, bullish: false };
  };
  const TH = 70;
  let run: { i: number; score: number; bullish: boolean }[] = [];
  const flush = () => {
    if (!run.length) return;
    let best = run[0];
    for (const x of run) if (x.score > best.score) best = x;
    out.push(best);
    run = [];
  };
  for (let i = 1; i < n; i++) {
    const r = scoreAt(i);
    if (r && r.score >= TH) run.push({ i, score: r.score, bullish: r.bullish });
    else flush();
  }
  flush();
  return out;
}

// Market STRUCTURE: label swing pivots HH / HL / LH / LL — the chart-reader's trend map (uptrend =
// higher highs + higher lows; downtrend = lower highs + lower lows). A pivot high is a bar whose high
// tops the `w` bars on each side (mirror for a pivot low); each swing is labelled by comparing to the
// previous SAME-type swing. This is the same swing structure the deterministic engine scores
// internally (market_structure / swing_high / swing_low / CHoCH), now drawn on the chart.
type StructPoint = { i: number; price: number; high: boolean; label: "HH" | "LH" | "HL" | "LL" };
function structureSwings(candles: Candle[], w = 3): StructPoint[] {
  const out: StructPoint[] = [];
  let lastHigh: number | null = null;
  let lastLow: number | null = null;
  for (let i = w; i < candles.length - w; i++) {
    const h = candles[i].high;
    const l = candles[i].low;
    let isHigh = true;
    let isLow = true;
    for (let k = 1; k <= w; k++) {
      if (candles[i - k].high >= h || candles[i + k].high > h) isHigh = false;
      if (candles[i - k].low <= l || candles[i + k].low < l) isLow = false;
    }
    if (isHigh) {
      out.push({ i, price: h, high: true, label: lastHigh !== null && h < lastHigh ? "LH" : "HH" });
      lastHigh = h;
    } else if (isLow) {
      out.push({ i, price: l, high: false, label: lastLow !== null && l < lastLow ? "LL" : "HL" });
      lastLow = l;
    }
  }
  return out;
}

// The dollar P&L an open position would show AT a given price level (e.g. its SL or TP). Derived
// from the live floating P&L per price unit — which already encodes lot size, contract size and
// FX conversion — so it's exact in the account currency. Empty until price moves off entry (the
// ratio is undefined at entry). SL => a loss (negative), TP => a gain (positive).
function usdAtLevel(pos: PositionView, level: number | null | undefined): string {
  if (level == null || pos.last_price == null || pos.last_price === pos.entry_price) return "";
  const perPrice = pos.unrealized_pnl / (pos.last_price - pos.entry_price);
  if (!isFinite(perPrice)) return "";
  const usd = (level - pos.entry_price) * perPrice;
  return `${usd >= 0 ? "+" : "−"}$${Math.abs(usd).toFixed(2)}`;
}

// A tiny line-style swatch for the overlay legend, so dashed (proposal) / solid (open position) /
// large-dashed (armed) are distinguishable at a glance.
function LineSwatch({ dash }: { dash: string }) {
  return (
    <svg width="22" height="6" className="inline-block align-middle">
      <line x1="0" y1="3" x2="22" y2="3" stroke="#d4d4d4" strokeWidth="2" strokeDasharray={dash} />
    </svg>
  );
}

// --- Sub-pane sizing -------------------------------------------------------------------------
// RSI and MACD ship at 120px, but how much room an indicator deserves is a personal call: someone
// reading divergence wants a tall RSI, someone who just wants the histogram sign wants it short.
// So the heights are draggable and remembered — resizing a pane every session is a chore nobody
// keeps doing, and an indicator too cramped to read is an indicator you stop looking at.
const PANE_MIN = 60;
const PANE_MAX = 420;
const PANE_DEFAULT = 120;
const clampPaneH = (h: number) => Math.min(PANE_MAX, Math.max(PANE_MIN, Math.round(h)));

function storedPaneH(key: string): number {
  try {
    const v = Number(localStorage.getItem(key));
    return Number.isFinite(v) && v > 0 ? clampPaneH(v) : PANE_DEFAULT;
  } catch {
    return PANE_DEFAULT;
  }
}

// The grab-strip that sits on a pane's TOP edge. Pointer capture (not window listeners) keeps the
// drag alive when the cursor leaves the 8px strip — which it does immediately on any real drag.
function PaneResizer({ height, onChange, label }: { height: number; onChange: (h: number) => void; label: string }) {
  const drag = useRef<{ y: number; h: number } | null>(null);
  return (
    <div
      onPointerDown={(e) => {
        drag.current = { y: e.clientY, h: height };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d) return;
        // The handle IS the top edge, so dragging up must grow the pane downward-anchored.
        onChange(clampPaneH(d.h - (e.clientY - d.y)));
      }}
      onPointerUp={(e) => {
        drag.current = null;
        e.currentTarget.releasePointerCapture(e.pointerId);
      }}
      onPointerCancel={() => { drag.current = null; }}
      onDoubleClick={() => onChange(PANE_DEFAULT)}
      title={`Drag to resize the ${label} pane · double-click to reset`}
      className="group flex h-2 shrink-0 cursor-ns-resize touch-none items-center justify-center"
    >
      <div className="h-[3px] w-10 rounded-full bg-neutral-800 transition group-hover:w-16 group-hover:bg-sky-500" />
    </div>
  );
}

export function Chart({ symbol, assetClass, timeframe, proposal, liveQuote, positions, armed, onSetSlTp, onSetArmedLevels, scenLevels, isFullscreen, onToggleFullscreen,
                        scenLevelsShown, onToggleScenLevels }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  // Full screen is owned by the Dashboard (the position strip must travel with the chart).
  const isFull = !!isFullscreen;
  const toggleFullscreen = () => onToggleFullscreen?.();
  const [rsiH, setRsiH] = useState(() => storedPaneH("chart.rsiH"));
  const [macdH, setMacdH] = useState(() => storedPaneH("chart.macdH"));
  const rsiContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const emaRefs = useRef<Record<number, ISeriesApi<"Line">>>({});
  const stRef = useRef<ISeriesApi<"Line"> | null>(null);  // SuperTrend (ONE line, per-point colour)
  const emaHiRef = useRef<ISeriesApi<"Line"> | null>(null);  // EMA20 of highs (upper band)
  const emaLoRef = useRef<ISeriesApi<"Line"> | null>(null);  // EMA20 of lows (lower band)
  const chanMidRef = useRef<ISeriesApi<"Line"> | null>(null);   // regression channel mid (trend line)
  const chanUpRef = useRef<ISeriesApi<"Line"> | null>(null);    // upper band (dynamic resistance)
  const chanLoRef = useRef<ISeriesApi<"Line"> | null>(null);    // lower band (dynamic support)
  const srLinesRef = useRef<IPriceLine[]>([]);                  // multi-TF support/resistance lines
  const rsiChartRef = useRef<IChartApi | null>(null);
  const rsiSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdContainerRef = useRef<HTMLDivElement>(null);
  const macdChartRef = useRef<IChartApi | null>(null);
  const macdLineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdSignalRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdHistRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const candlesRef = useRef<Candle[]>([]);
  const lastBarRef = useRef<CandlestickData | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const posLinesRef = useRef<IPriceLine[]>([]);
  const armedLinesRef = useRef<IPriceLine[]>([]);
  const scenLinesRef = useRef<IPriceLine[]>([]);   // the AI scenario's cited S/R, plotted on request
  // Drag-to-adjust price lines (position SL/TP + armed trigger/SL/TP). The overlay effects register
  // a DragHandle per draggable line; the (once-attached) mouse handlers read them from refs so they
  // never need re-binding. `validate` keeps a level on the correct side; `commit` persists on drop.
  type DragHandle = {
    key: string;            // namespaced; e.g. "pos-sl", "armed-7-trigger"
    label: string;          // shown while dragging ("SL"/"TP"/"Trigger")
    refPos: PositionView | null;  // for the $-at-level hint (position handles only)
    line: IPriceLine;
    price: number;          // the handle's current price
    validate: (p: number) => boolean;
    commit: (p: number) => void;
  };
  const handlesRef = useRef<DragHandle[]>([]);
  const dragRef = useRef<DragHandle | null>(null);
  const dragPriceRef = useRef<number | null>(null);
  const onSetSlTpRef = useRef<Props["onSetSlTp"]>(onSetSlTp);
  const onSetArmedRef = useRef<Props["onSetArmedLevels"]>(onSetArmedLevels);
  onSetSlTpRef.current = onSetSlTp;
  onSetArmedRef.current = onSetArmedLevels;
  const [dragHint, setDragHint] = useState<{ label: string; price: number; pos: PositionView | null } | null>(null);

  // Hand drawing: the saved set, the stroke in progress, and the canvas it's painted on.
  const [drawings, saveDrawings] = useDrawings(symbol);
  const [drawMode, setDrawMode] = useState<"off" | "f" | "t" | "h">("off");
  const [drawList, setDrawList] = useState(false);
  const drawModeRef = useRef(drawMode);
  drawModeRef.current = drawMode;
  const drawingsRef = useRef<Drawing[]>(drawings);
  drawingsRef.current = drawings;
  const drawCanvasRef = useRef<HTMLCanvasElement>(null);
  const draftRef = useRef<Drawing | null>(null);            // the line/stroke under the cursor
  const lastPxRef = useRef<{ x: number; y: number } | null>(null);
  const drawLinesRef = useRef<IPriceLine[]>([]);            // horizontal levels (real price lines)
  const barTimesRef = useRef<number[]>([]);                 // candle times, for pixel↔time mapping
  const dirtyRef = useRef(0);                               // bumped to force a repaint
  // Bumped when a new candle set lands, so drawings re-render against the new bar times.
  const [dataV, setDataV] = useState(0);

  const [legend, setLegend] = useState<Legend | null>(null);
  const [showEma, setShowEma] = usePersisted<Record<number, boolean>>(
    "chart.showEma", { 10: true, 20: true, 50: true, 100: false, 200: true }, true);
  const [showRsi, setShowRsi] = usePersisted("chart.showRsi", true);
  const [showMacd, setShowMacd] = usePersisted("chart.showMacd", true);
  const [showSt, setShowSt] = usePersisted("chart.showSt", true);
  const [showPb, setShowPb] = usePersisted("chart.showPb", true);
  const [showBand, setShowBand] = usePersisted("chart.showBand", true);
  const [showStructure, setShowStructure] = usePersisted("chart.showStructure", false);
  const [showChannel, setShowChannel] = usePersisted("chart.showChannel", false);
  const [showSR, setShowSR] = usePersisted<Record<string, boolean>>(
    "chart.srByTf", { "1h": false, "4h": false, "1d": false });
  const [ctx, setCtx] = useState<MarketContext | null>(null);
  const [ctxBusy, setCtxBusy] = useState(false);
  const [ctxAt, setCtxAt] = useState<Date | null>(null);   // when this reading was taken
  const rootRef = useRef<HTMLDivElement>(null);            // drag frame for the floating panels
  const ctxDrag = useDragPanel("chart.ctxPos", rootRef);
  // The price map's S/R ladder, plotted on request. Held as its OWN copy rather than read from
  // `ctx`, so closing the map panel to actually look at the chart doesn't wipe the levels you just
  // asked it to draw — which is the whole reason you drew them.
  type Lv = { price: number; tf: string; tests: number };
  const [ctxLevels, setCtxLevels] = useState<{ res: Lv[]; sup: Lv[] } | null>(null);
  const ctxLinesRef = useRef<IPriceLine[]>([]);
  const [scen, setScen] = useState<AiScenarioRead | null>(null);
  const [scenBusy, setScenBusy] = useState(false);

  const toTime = (c: Candle) => Math.floor(Date.parse(c.ts) / 1000) as UTCTimestamp;

  // Create the main chart + series once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a3a3a3" },
      grid: { vertLines: { color: "#1f1f1f" }, horzLines: { color: "#1f1f1f" } },
      timeScale: {
        timeVisible: true, secondsVisible: false, borderColor: "#404040",
        barSpacing: 9, minBarSpacing: 4, rightOffset: 6,  // fatter candles + a little right gap
      },
      // Tighter top/bottom padding so the candles fill the pane vertically (less dead space).
      // minimumWidth pins the price-axis width so the RSI pane below lines up bar-for-bar.
      rightPriceScale: {
        borderColor: "#404040", scaleMargins: { top: 0.08, bottom: 0.16 }, minimumWidth: 72,
      },
      crosshair: { mode: 1 },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350",
      borderUpColor: "#26a69a", borderDownColor: "#ef5350",
      wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    });
    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" }, priceScaleId: "vol",
      lastValueVisible: false, priceLineVisible: false,  // keep the right axis clean
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    chartRef.current = chart;
    seriesRef.current = series;
    volumeRef.current = volume;
    for (const { period, color } of EMA_CONFIG) {
      emaRefs.current[period] = chart.addLineSeries({
        color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
    }
    // SuperTrend = ONE line whose per-point colour flips (green bullish / red bearish). A single
    // coloured line (not two series) avoids lightweight-charts connecting an inactive series across
    // the bars where it should be hidden — which drew two parallel lines.
    stRef.current = chart.addLineSeries({
      lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });
    // EMA20 of highs / lows = a band around price (the SuperTrend-band breakout strategy).
    emaHiRef.current = chart.addLineSeries({
      color: "#22d3ee", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });
    emaLoRef.current = chart.addLineSeries({
      color: "#f97316", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });
    // Regression channel = the algorithmic trend line (mid) + dynamic resistance/support (bands).
    chanMidRef.current = chart.addLineSeries({
      color: "#a78bfa", lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    chanUpRef.current = chart.addLineSeries({
      color: "#a78bfa", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });
    chanLoRef.current = chart.addLineSeries({
      color: "#a78bfa", lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false,
    });

    chart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData.get(series) as CandlestickData | undefined;
      if (bar) setLegend({ open: bar.open, high: bar.high, low: bar.low, close: bar.close });
      else if (lastBarRef.current) {
        const b = lastBarRef.current;
        setLegend({ open: b.open, high: b.high, low: b.low, close: b.close });
      }
    });

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeRef.current = null;
      emaRefs.current = {};
      stRef.current = null;
      emaHiRef.current = null;
      emaLoRef.current = null;
    };
  }, []);

  // Remember the pane heights. Both sub-charts are autoSize, so resizing the wrapper is all it
  // takes — no manual chart.resize() call, and no re-create on drag.
  useEffect(() => {
    try {
      localStorage.setItem("chart.rsiH", String(rsiH));
      localStorage.setItem("chart.macdH", String(macdH));
    } catch {
      /* private mode / quota — the heights just won't survive a reload */
    }
  }, [rsiH, macdH]);

  // Create/destroy the RSI sub-pane chart when toggled; sync its time axis to the main chart.
  useEffect(() => {
    if (!showRsi || !rsiContainerRef.current) return;
    const rsiChart = createChart(rsiContainerRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a3a3a3" },
      grid: { vertLines: { color: "#1f1f1f" }, horzLines: { color: "#1f1f1f" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#404040" },
      // Same minimumWidth as the main chart so both price axes match and the bars line up.
      rightPriceScale: { borderColor: "#404040", minimumWidth: 72 },
      crosshair: { mode: 1 },
    });
    const line = rsiChart.addLineSeries({ color: "#c084fc", lineWidth: 2, priceLineVisible: false });
    line.createPriceLine({ price: 70, color: "#ef5350", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "70" });
    line.createPriceLine({ price: 30, color: "#26a69a", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "30" });
    rsiChartRef.current = rsiChart;
    rsiSeriesRef.current = line;
    applyRsi();

    // Keep the RSI pane time-aligned with the main chart as you pan/zoom.
    const main = chartRef.current;
    let syncing = false;
    const mainToRsi = (range: LogicalRange | null) => {
      if (syncing || !range) return;
      syncing = true;
      rsiChart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    };
    const rsiToMain = (range: LogicalRange | null) => {
      if (syncing || !range || !main) return;
      syncing = true;
      main.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    };
    main?.timeScale().subscribeVisibleLogicalRangeChange(mainToRsi);
    rsiChart.timeScale().subscribeVisibleLogicalRangeChange(rsiToMain);
    const mainRange = main?.timeScale().getVisibleLogicalRange();
    if (mainRange) rsiChart.timeScale().setVisibleLogicalRange(mainRange);

    return () => {
      main?.timeScale().unsubscribeVisibleLogicalRangeChange(mainToRsi);
      rsiChart.remove();
      rsiChartRef.current = null;
      rsiSeriesRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showRsi]);

  // Create/destroy the MACD sub-pane (histogram + macd/signal lines) when toggled; sync its time
  // axis to the main chart the same way the RSI pane does.
  useEffect(() => {
    if (!showMacd || !macdContainerRef.current) return;
    const macdChart = createChart(macdContainerRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a3a3a3" },
      grid: { vertLines: { color: "#1f1f1f" }, horzLines: { color: "#1f1f1f" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#404040" },
      rightPriceScale: { borderColor: "#404040", minimumWidth: 72 },
      crosshair: { mode: 1 },
    });
    // Histogram first so the lines render on top; all share the right price scale (centred on 0).
    const hist = macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    const macdLine = macdChart.addLineSeries({ color: "#3b82f6", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    const signalLine = macdChart.addLineSeries({ color: "#f59e0b", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    macdLine.createPriceLine({ price: 0, color: "#404040", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false });
    macdChartRef.current = macdChart;
    macdLineRef.current = macdLine;
    macdSignalRef.current = signalLine;
    macdHistRef.current = hist;
    applyMacd();

    const main = chartRef.current;
    let syncing = false;
    const mainToMacd = (range: LogicalRange | null) => {
      if (syncing || !range) return;
      syncing = true;
      macdChart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    };
    const macdToMain = (range: LogicalRange | null) => {
      if (syncing || !range || !main) return;
      syncing = true;
      main.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    };
    main?.timeScale().subscribeVisibleLogicalRangeChange(mainToMacd);
    macdChart.timeScale().subscribeVisibleLogicalRangeChange(macdToMain);
    const mainRange = main?.timeScale().getVisibleLogicalRange();
    if (mainRange) macdChart.timeScale().setVisibleLogicalRange(mainRange);

    return () => {
      main?.timeScale().unsubscribeVisibleLogicalRangeChange(mainToMacd);
      macdChart.remove();
      macdChartRef.current = null;
      macdLineRef.current = null;
      macdSignalRef.current = null;
      macdHistRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showMacd]);

  // Load candles + volume + EMAs + RSI when symbol/asset/timeframe changes.
  useEffect(() => {
    let cancelled = false;
    api
      .ohlcv(symbol, assetClass, timeframe, 400)
      .then((series) => {
        if (cancelled || !seriesRef.current || !volumeRef.current) return;
        candlesRef.current = series.candles;
        // Drawings map pixels↔time through the bar times, so refresh that lookup with the data.
        barTimesRef.current = series.candles.map((c) => Math.floor(Date.parse(c.ts) / 1000));
        setDataV((v) => v + 1);
        const candleData: CandlestickData[] = series.candles.map((c) => ({
          time: toTime(c), open: c.open, high: c.high, low: c.low, close: c.close,
        }));
        seriesRef.current.setData(candleData);
        // Format the right price axis + last-value label to the instrument's real precision
        // (per pair) instead of the default 2 decimals.
        const dec = inferDecimals(series.candles);
        seriesRef.current.applyOptions({
          priceFormat: { type: "price", precision: dec, minMove: 10 ** -dec },
        });
        volumeRef.current.setData(
          series.candles.map((c): HistogramData => ({
            time: toTime(c), value: c.volume,
            color: c.close >= c.open ? "rgba(38,166,154,0.4)" : "rgba(239,83,80,0.4)",
          })),
        );
        applyEmas(series.candles);
        applyRsi();
        applyMacd();
        applySuperTrend(series.candles);
        applyMarkers(series.candles);
        applyBand(series.candles);
        applyChannel(series.candles);
        lastBarRef.current = candleData[candleData.length - 1] ?? null;
        const last = series.candles[series.candles.length - 1];
        if (last) setLegend({ open: last.open, high: last.high, low: last.low, close: last.close });
        // Show the most recent ~110 bars (not all 400) so candles are big and the price scale
        // hugs current price — much less dead space than fitContent(). Pan/zoom still works.
        const ts = chartRef.current?.timeScale();
        const n = candleData.length;
        if (ts && n) {
          const VISIBLE = 110;
          ts.setVisibleLogicalRange({ from: Math.max(0, n - VISIBLE), to: n + 6 });
        }
        // Re-fit the price (y) axis to the NEW pair. autoScale gets turned OFF the moment the user
        // drags the price axis, and stays off — which otherwise leaves the next pair's candles
        // off-screen behind a pinned scale (e.g. BNB at 588 hidden under a 10000+ range).
        seriesRef.current.priceScale().applyOptions({ autoScale: true });
      })
      .catch(() => {
        // Don't leave the PREVIOUS pair's chart up when this one fails to load — clear it so it's
        // obviously empty (not silently showing the wrong pair's data/scale).
        if (cancelled || !seriesRef.current || !volumeRef.current) return;
        candlesRef.current = [];
        seriesRef.current.setData([]);
        seriesRef.current.setMarkers([]);
        volumeRef.current.setData([]);
        for (const { period } of EMA_CONFIG) emaRefs.current[period]?.setData([]);
        stRef.current?.setData([]);
        emaHiRef.current?.setData([]);
        emaLoRef.current?.setData([]);
        chanMidRef.current?.setData([]);
        chanUpRef.current?.setData([]);
        chanLoRef.current?.setData([]);
        rsiSeriesRef.current?.setData([]);
        macdLineRef.current?.setData([]);
        macdSignalRef.current?.setData([]);
        macdHistRef.current?.setData([]);
        setLegend(null);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, assetClass, timeframe]);

  useEffect(() => {
    applyEmas(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showEma]);

  useEffect(() => {
    applySuperTrend(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSt]);

  // `positions` is in here so the entry marker follows the trade: it appears the moment a position
  // opens, its P&L text tracks the poll, and it disappears on close.
  useEffect(() => {
    applyMarkers(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showPb, showStructure, positions, symbol]);

  useEffect(() => {
    applyBand(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showBand]);

  useEffect(() => {
    applyChannel(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showChannel]);

  function applyEmas(candles: Candle[]) {
    if (!candles.length) return;
    const closes = candles.map((c) => c.close);
    const times = candles.map(toTime);
    for (const { period } of EMA_CONFIG) {
      const lineSeries = emaRefs.current[period];
      if (!lineSeries) continue;
      if (!showEma[period]) {
        lineSeries.setData([]);
        continue;
      }
      const vals = ema(closes, period);
      const data: LineData[] = [];
      for (let i = 0; i < vals.length; i++) {
        if (vals[i] !== undefined) data.push({ time: times[i], value: vals[i] as number });
      }
      lineSeries.setData(data);
    }
  }

  function applyRsi() {
    const line = rsiSeriesRef.current;
    const candles = candlesRef.current;
    if (!line || !candles.length) return;
    const closes = candles.map((c) => c.close);
    const times = candles.map(toTime);
    const vals = rsiCalc(closes, 14);
    // Emit a point for EVERY candle (whitespace during the 14-bar warmup) so the RSI series has
    // the SAME length/logical indices as the candles — otherwise the logical-range sync between
    // the two charts is offset by the warmup and the panes don't line up.
    const data: (LineData | WhitespaceData)[] = [];
    for (let i = 0; i < vals.length; i++) {
      data.push(vals[i] !== null ? { time: times[i], value: vals[i] as number } : { time: times[i] });
    }
    line.setData(data);
  }

  function applyMacd() {
    const macdLine = macdLineRef.current;
    const signalLine = macdSignalRef.current;
    const histSeries = macdHistRef.current;
    const candles = candlesRef.current;
    if (!macdLine || !signalLine || !histSeries || !candles.length) return;
    const times = candles.map(toTime);
    const { macd, signal, hist } = macdCalc(candles.map((c) => c.close));
    // A point for EVERY candle (whitespace during warmup) so logical indices match the main pane.
    const macdData: (LineData | WhitespaceData)[] = [];
    const signalData: (LineData | WhitespaceData)[] = [];
    const histData: (HistogramData | WhitespaceData)[] = [];
    for (let i = 0; i < candles.length; i++) {
      macdData.push(macd[i] !== null ? { time: times[i], value: macd[i] as number } : { time: times[i] });
      signalData.push(signal[i] !== null ? { time: times[i], value: signal[i] as number } : { time: times[i] });
      histData.push(
        hist[i] !== null
          ? { time: times[i], value: hist[i] as number,
              color: (hist[i] as number) >= 0 ? "rgba(38,166,154,0.5)" : "rgba(239,83,80,0.5)" }
          : { time: times[i] },
      );
    }
    histSeries.setData(histData);
    macdLine.setData(macdData);
    signalLine.setData(signalData);
  }

  function applySuperTrend(candles: Candle[]) {
    const line = stRef.current;
    if (!line) return;
    if (!showSt || !candles.length) {
      line.setData([]);
      return;
    }
    const times = candles.map(toTime);
    const { st, dir } = superTrend(candles);
    // ONE line; each point carries its own colour so the line flips green<->red at trend changes.
    // Whitespace during the ATR warmup keeps the leading gap (no spurious line before it's valid).
    const data: (LineData | WhitespaceData)[] = [];
    for (let i = 0; i < candles.length; i++) {
      data.push(
        st[i] === null
          ? { time: times[i] }
          : { time: times[i], value: st[i] as number, color: dir[i] === 1 ? "#26a69a" : "#ef5350" },
      );
    }
    line.setData(data);
  }

  // Pullback arrows AND market-structure (HH/HL/LH/LL) labels share the candle series' single marker
  // set (setMarkers replaces), so both toggles feed one sorted array.
  function applyMarkers(candles: Candle[]) {
    const series = seriesRef.current;
    if (!series) return;
    if (!candles.length) {
      series.setMarkers([]);
      return;
    }
    const markers: SeriesMarker<Time>[] = [];
    if (showPb) {
      for (const s of pullbackSignals(candles)) {
        markers.push({
          time: toTime(candles[s.i]),
          position: s.bullish ? "belowBar" : "aboveBar",
          color: s.bullish ? "#26a69a" : "#ef5350",
          shape: s.bullish ? "arrowUp" : "arrowDown",
          text: `PB ${s.score}`,
        });
      }
    }
    if (showStructure) {
      for (const p of structureSwings(candles)) {
        markers.push({
          time: toTime(candles[p.i]),
          position: p.high ? "aboveBar" : "belowBar",
          color: p.label === "HH" || p.label === "HL" ? "#26a69a" : "#ef5350",
          shape: "circle",
          text: p.label,
        });
      }
    }
    // ENTRY marker — pins the open position to the CANDLE it was opened on.
    //
    // Kept deliberately spare. The price, P&L, risk and reward are ALL already on screen (header bar
    // + the entry/SL/TP line labels), so repeating them here would just crowd the candles. The one
    // thing no other element can show is WHICH BAR you got in on, so that's all this says.
    //
    // Coloured with the entry line's blue rather than by profit: the arrow and the horizontal line
    // then read as one object ("here is your entry, in time AND in price") instead of competing with
    // the red/green of the candles around it. Live P&L is the header's job.
    for (const p of positions ?? []) {
      if (p.symbol.toUpperCase() !== symbol.toUpperCase() || !p.opened_at) continue;
      // Broker timestamps may arrive without a zone marker; they are UTC, and the candles are too.
      const iso = /[Zz]|[+-]\d{2}:?\d{2}$/.test(p.opened_at) ? p.opened_at : `${p.opened_at}Z`;
      const at = Date.parse(iso) / 1000;
      if (!Number.isFinite(at)) continue;
      // The candle the entry falls INSIDE: the last one that had already opened by then. Anchoring
      // to the next bar instead would draw the marker after the fact.
      let idx = -1;
      for (let k = 0; k < candles.length; k++) {
        if (toTime(candles[k]) <= at) idx = k;
        else break;
      }
      if (idx < 0) continue;                       // opened before the loaded history starts
      const isLong = p.direction === "long";
      const pnl = p.unrealized_pnl ?? 0;
      // Round to whole dollars, but keep cents on small numbers — "+$1" for a $0.57 position reads
      // as a rounding error rather than a real figure.
      const amount = Math.abs(pnl) >= 10 ? pnl.toFixed(0) : pnl.toFixed(2);
      markers.push({
        time: toTime(candles[idx]),
        // Sit on the side the trade is betting AGAINST, so the arrow points into the move it wants:
        // a long's arrow sits under the bar pointing up, a short's above it pointing down.
        position: isLong ? "belowBar" : "aboveBar",
        // Green/red by live P&L so the bar answers "has price gone my way since I got in?" at a
        // glance — the one question the blue entry LINE can't answer on its own.
        color: pnl > 0 ? "#26a69a" : pnl < 0 ? "#ef5350" : "#3b82f6",
        shape: isLong ? "arrowUp" : "arrowDown",
        // Short by design: price/risk/reward are already in the header and the line labels, so the
        // marker carries only what's unique to it — the bar you entered on, and how it's doing.
        text: `ENTRY ${pnl >= 0 ? "+" : "−"}$${amount.replace("-", "")}`,
      });
    }
    // lightweight-charts requires markers in ascending time order.
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    series.setMarkers(markers);
  }

  function applyBand(candles: Candle[]) {
    const hi = emaHiRef.current;
    const lo = emaLoRef.current;
    if (!hi || !lo) return;
    if (!showBand || !candles.length) {
      hi.setData([]);
      lo.setData([]);
      return;
    }
    const times = candles.map(toTime);
    const eh = ema(candles.map((c) => c.high), 20);
    const el = ema(candles.map((c) => c.low), 20);
    const hiData: LineData[] = [];
    const loData: LineData[] = [];
    for (let i = 0; i < candles.length; i++) {
      if (eh[i] !== undefined) hiData.push({ time: times[i], value: eh[i] as number });
      if (el[i] !== undefined) loData.push({ time: times[i], value: el[i] as number });
    }
    hi.setData(hiData);
    lo.setData(loData);
  }

  // Regression channel = the algorithmic trend line (mid) + dynamic resistance/support (bands), drawn
  // over the last LB bars. Mirrors the backend indicators.regression_channel the engine reads.
  function applyChannel(candles: Candle[]) {
    const mid = chanMidRef.current, up = chanUpRef.current, lo = chanLoRef.current;
    if (!mid || !up || !lo) return;
    const clear = () => { mid.setData([]); up.setData([]); lo.setData([]); };
    if (!showChannel || candles.length < 20) return clear();
    const LB = 60, K = 2;
    const w = candles.slice(-LB);
    const m = w.length;
    const ys = w.map((c) => c.close);
    const meanX = (m - 1) / 2;
    const meanY = ys.reduce((a, b) => a + b, 0) / m;
    let sxx = 0, sxy = 0;
    for (let x = 0; x < m; x++) { sxx += (x - meanX) ** 2; sxy += (x - meanX) * (ys[x] - meanY); }
    if (sxx === 0) return clear();
    const slope = sxy / sxx;
    const intercept = meanY - slope * meanX;
    let ss = 0;
    for (let x = 0; x < m; x++) ss += (ys[x] - (intercept + slope * x)) ** 2;
    const std = Math.sqrt(ss / m);
    const midD: LineData[] = [], upD: LineData[] = [], loD: LineData[] = [];
    for (let x = 0; x < m; x++) {
      const t = toTime(w[x]);
      const v = intercept + slope * x;
      midD.push({ time: t, value: v });
      upD.push({ time: t, value: v + K * std });
      loD.push({ time: t, value: v - K * std });
    }
    mid.setData(midD); up.setData(upD); lo.setData(loD);
  }

  // Multi-timeframe support/resistance — horizontal lines from 1h / 4h / 1d, so a lower-TF chart also
  // shows the STRONGER higher-TF levels. Colour + weight scale with the timeframe (1d = boldest).
  async function applySR() {
    const series = seriesRef.current;
    if (!series) return;
    for (const l of srLinesRef.current) series.removePriceLine(l);
    srLinesRef.current = [];
    if (!["1h", "4h", "1d"].some((tf) => showSR[tf])) return;   // nothing enabled
    let data: Awaited<ReturnType<typeof api.levels>>;
    try {
      data = await api.levels(symbol, assetClass);
    } catch {
      return;
    }
    if (!seriesRef.current) return;
    const STYLE: Record<string, { color: string; width: 1 | 2 | 3 }> = {
      "1h": { color: "#64748b", width: 1 },  // slate — weakest
      "4h": { color: "#f59e0b", width: 2 },  // amber
      "1d": { color: "#ef4444", width: 3 },  // red — strongest
    };
    for (const tf of ["1d", "4h", "1h"]) {   // draw strongest first
      if (!showSR[tf]) continue;
      const st = STYLE[tf];
      for (const lv of data.levels[tf] ?? []) {
        srLinesRef.current.push(
          seriesRef.current.createPriceLine({
            price: lv.price,
            color: st.color,
            lineWidth: st.width,
            lineStyle: LineStyle.Dotted,
            axisLabelVisible: true,
            title: `${tf.toUpperCase()} ${lv.kind === "resistance" ? "R" : "S"}`,
          }),
        );
      }
    }
  }

  useEffect(() => {
    void applySR();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, assetClass, showSR]);

  // Clear the reads when switching pairs (they're pair-specific).
  useEffect(() => {
    setCtx(null);
    setScen(null);
  }, [symbol]);

  const loadContext = async () => {
    setCtxBusy(true);
    try {
      const next = await api.context(symbol, assetClass, timeframe);
      setCtx(next);
      setCtxAt(new Date());
      // If the levels are on the chart, re-plot them from the FRESH read. Leaving the old ones up
      // next to a new reading is the one genuinely dangerous outcome here — you'd be trading a
      // level the analysis no longer thinks is there.
      setCtxLevels((v) => (v ? { res: next.resistance_ladder ?? [], sup: next.support_ladder ?? [] } : v));
    } catch {
      setCtx(null);
    } finally {
      setCtxBusy(false);
    }
  };

  // Re-read automatically when the chart changes underneath the panel. Leaving a 1h reading on
  // screen after switching to 5m is worse than showing nothing: it looks current, and every number
  // in it — RSI, MACD, structure, the levels drawn on the chart — belongs to a different chart.
  const loadCtxRef = useRef(loadContext);
  loadCtxRef.current = loadContext;
  const ctxOpenRef = useRef(false);
  ctxOpenRef.current = ctx != null;
  useEffect(() => {
    if (ctxOpenRef.current) void loadCtxRef.current();
  }, [symbol, timeframe, assetClass]);

  const loadScenarios = async () => {
    setScenBusy(true);
    try {
      setScen(await api.scenarios(symbol, assetClass));
    } catch {
      setScen(null);
    } finally {
      setScenBusy(false);
    }
  };

  // Live quote -> update the last candle.
  useEffect(() => {
    if (!liveQuote || !seriesRef.current || !lastBarRef.current) return;
    const bar = lastBarRef.current;
    const updated: CandlestickData = {
      ...bar, close: liveQuote.price,
      high: Math.max(bar.high, liveQuote.price), low: Math.min(bar.low, liveQuote.price),
    };
    lastBarRef.current = updated;
    seriesRef.current.update(updated);
  }, [liveQuote]);

  // Proposal entry/stop/target overlay lines (dashed). Suppressed once an actual position
  // exists for this symbol — the solid position lines are then the source of truth (avoids
  // duplicate Stop/pos-SL, Entry/pos, Target/pos-TP labels stacking on the axis).
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((l) => series.removePriceLine(l));
    priceLinesRef.current = [];
    const hasPosition = (positions ?? []).some(
      (p) => p.symbol.toUpperCase() === symbol.toUpperCase(),
    );
    if (!proposal || proposal.direction === "no_trade" || hasPosition) return;
    const add = (price: number | null, color: string, title: string) => {
      if (price == null) return;
      priceLinesRef.current.push(
        series.createPriceLine({ price, color, lineWidth: 2, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title }),
      );
    };
    add(proposal.entry, "#3b82f6", "Entry");
    add(proposal.stop_loss, "#ef5350", "Stop");
    add(proposal.take_profit, "#26a69a", "Target");
  }, [proposal, positions, symbol]);

  // AI-scenario S/R overlay — the exact levels the AI cites, plotted as BOLD labelled lines on request
  // (the "Show these levels" button in the scenario card), so the zones in the AI's text are obvious
  // on the chart. Distinct from the thin technical 1H/4H/1D S/R lines.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    scenLinesRef.current.forEach((l) => series.removePriceLine(l));
    scenLinesRef.current = [];
    if (!scenLevels) return;
    const add = (price: number | null | undefined, color: string, title: string) => {
      if (price == null) return;
      scenLinesRef.current.push(
        series.createPriceLine({ price, color, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title }),
      );
    };
    const inv = scenLevels.invalidation;
    const same = (a: number | null, b: number | null) =>
      a != null && b != null && Math.abs(a - b) < Math.abs(a) * 1e-4;
    add(scenLevels.target, "#a78bfa", "🎯 Target");             // violet — the continuation TP
    add(inv, "#ef5350", "⛔ Stop / invalidation");              // red — the stop reference (flips the setup)
    if (!same(scenLevels.resistance, inv)) add(scenLevels.resistance, "#f59e0b", "🟠 Resistance");
    if (!same(scenLevels.support, inv)) add(scenLevels.support, "#26a69a", "🟢 Support");
  }, [scenLevels]);

  // The price map's own S/R ladder, plotted on request. Each label carries the timeframe it came
  // from and how many times it's been tested — a level tested 5 times is a wall you fade, a fresh
  // one breaks easily, and that difference decides whether you trade the break or the rejection.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    ctxLinesRef.current.forEach((l) => series.removePriceLine(l));
    ctxLinesRef.current = [];
    if (!ctxLevels) return;
    const strength = (n: number) => (n >= 4 ? "strong" : n >= 2 ? "moderate" : "fresh");
    const add = (lv: Lv, i: number, up: boolean) => {
      ctxLinesRef.current.push(
        series.createPriceLine({
          price: lv.price,
          color: up ? "#f59e0b" : "#26a69a",
          lineWidth: lv.tests >= 4 ? 3 : 2,           // thicker = more tested = harder wall
          lineStyle: i === 0 ? LineStyle.Solid : LineStyle.Dashed,   // solid = the one in play
          axisLabelVisible: true,
          title: `${up ? "R" : "S"}${i + 1} ${lv.tf.toUpperCase()} · ${strength(lv.tests)} ${lv.tests}x`,
        }),
      );
    };
    ctxLevels.res.forEach((lv, i) => add(lv, i, true));
    ctxLevels.sup.forEach((lv, i) => add(lv, i, false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctxLevels, symbol, timeframe]);

  // Open-position overlay (solid lines) — drawn from live broker positions for THIS symbol,
  // so an open trade's entry/SL/TP stays on the chart regardless of the current proposal.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    if (dragRef.current) return;  // mid-drag: don't rebuild (a positions poll would snap the line back)
    posLinesRef.current.forEach((l) => series.removePriceLine(l));
    posLinesRef.current = [];
    handlesRef.current = handlesRef.current.filter((h) => !h.key.startsWith("pos-"));
    const mine = (positions ?? []).filter(
      (p) => p.symbol.toUpperCase() === symbol.toUpperCase(),
    );
    const add = (price: number | null | undefined, color: string, title: string): IPriceLine | null => {
      if (price == null) return null;
      const line = series.createPriceLine({
        price, color, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title,
      });
      posLinesRef.current.push(line);
      return line;
    };
    for (const p of mine) {
      const arrow = p.direction === "long" ? "▲" : "▼";
      const isLong = p.direction === "long";
      // When the stop has been moved to breakeven (SL ≈ entry) the two labels would stack on
      // the same axis pixel — merge them into one instead of overlapping.
      const be =
        p.stop_loss != null && Math.abs(p.stop_loss - p.entry_price) <= Math.abs(p.entry_price) * 1e-4;
      // Show the $ you'd lose at SL / gain at TP right on the line label (like MT5's on-chart box).
      const slUsd = usdAtLevel(p, p.stop_loss);
      const tpUsd = usdAtLevel(p, p.take_profit);
      add(p.entry_price, "#3b82f6", be ? `${arrow} entry·SL ${slUsd}`.trim() : `${arrow} entry`);
      const slLine = be ? null : add(p.stop_loss, "#ef5350", `SL ${slUsd} · drag`.trim());
      const tpLine = add(p.take_profit, "#26a69a", `TP ${tpUsd} · drag`.trim());
      if (slLine && p.stop_loss != null) {
        handlesRef.current.push({
          key: "pos-sl", label: "SL", refPos: p, line: slLine, price: p.stop_loss,
          validate: (x) => (isLong ? x < p.entry_price : x > p.entry_price),
          commit: (x) => onSetSlTpRef.current?.(x, p.take_profit ?? null),
        });
      }
      if (tpLine && p.take_profit != null) {
        handlesRef.current.push({
          key: "pos-tp", label: "TP", refPos: p, line: tpLine, price: p.take_profit,
          validate: (x) => (isLong ? x > p.entry_price : x < p.entry_price),
          commit: (x) => onSetSlTpRef.current?.(p.stop_loss ?? null, x),
        });
      }
    }
  }, [positions, symbol]);

  // Armed conditional ('wait for the break') overlay — large-dashed amber lines for THIS symbol's
  // armed setups, so the pending trigger/SL/TP are visible on the chart distinct from proposal
  // (dashed) and open-position (solid) lines.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    if (dragRef.current) return;  // mid-drag: don't rebuild (a poll would snap the dragged line back)
    armedLinesRef.current.forEach((l) => series.removePriceLine(l));
    armedLinesRef.current = [];
    handlesRef.current = handlesRef.current.filter((h) => !h.key.startsWith("armed-"));
    const mine = (armed ?? []).filter((a) => a.symbol.toUpperCase() === symbol.toUpperCase());
    const add = (price: number | null, color: string, title: string): IPriceLine | null => {
      if (price == null) return null;
      const line = series.createPriceLine({
        price, color, lineWidth: 1, lineStyle: LineStyle.LargeDashed, axisLabelVisible: true, title,
      });
      armedLinesRef.current.push(line);
      return line;
    };
    for (const a of mine) {
      const isLong = a.direction === "long";
      // BREAK-AND-RETEST: draw the level that has to give way FIRST. Without it the chart shows a
      // buy-limit sitting under resistance with no explanation — which reads as the engine doing the
      // opposite of what it intends. Dotted and grey-blue so it's clearly a PRE-condition, not a
      // price the trade acts on; once broken it turns solid-ish and says so.
      const broken = a.break_confirmed_at != null;
      const brkLine = add(
        a.break_level ?? null,
        broken ? "#64748b" : "#93c5fd",
        broken ? "✓ broke — awaiting retest" : "① must break first",
      );
      if (brkLine) {
        brkLine.applyOptions({ lineStyle: LineStyle.Dotted, lineWidth: 1 });
      }
      const trigLine = add(
        a.trigger_price, "#f59e0b",
        // Name the entry for what it is once a break level exists, so the two amber/blue lines
        // aren't mistaken for each other.
        a.break_level != null ? (broken ? "⚡ RETEST entry · drag" : "② retest entry · drag")
                              : "⚡ Arm · drag",
      );
      const slLine = add(a.stop_loss, "#ef5350", "⚡ SL · drag");
      const tpLine = add(a.take_profit, "#26a69a", "⚡ TP · drag");
      // Trigger: side vs price isn't fixed (break vs limit), so just require it stay positive.
      if (trigLine) {
        handlesRef.current.push({
          key: `armed-${a.id}-trigger`, label: "Trigger", refPos: null, line: trigLine,
          price: a.trigger_price, validate: (x) => x > 0,
          commit: (x) => onSetArmedRef.current?.(a.id, { trigger_price: x }),
        });
      }
      if (slLine && a.stop_loss != null) {
        handlesRef.current.push({
          key: `armed-${a.id}-sl`, label: "SL", refPos: null, line: slLine, price: a.stop_loss,
          validate: (x) => (isLong ? x < a.trigger_price : x > a.trigger_price),
          commit: (x) => onSetArmedRef.current?.(a.id, { stop_loss: x }),
        });
      }
      if (tpLine && a.take_profit != null) {
        handlesRef.current.push({
          key: `armed-${a.id}-tp`, label: "TP", refPos: null, line: tpLine, price: a.take_profit,
          validate: (x) => (isLong ? x > a.trigger_price : x < a.trigger_price),
          commit: (x) => onSetArmedRef.current?.(a.id, { take_profit: x }),
        });
      }
    }
  }, [armed, symbol]);

  // --- pixel ↔ (time, price) -------------------------------------------------------------------
  // The vertical axis is exact: the series converts price↔y directly. The horizontal one is not —
  // lightweight-charts only maps times that ARE bars. So we go through the "logical" axis (a
  // fractional bar index, which accepts values between bars and past the last one) and interpolate
  // against the bar times. That's what buys sub-candle precision: a point lands mid-candle if
  // that's where you drew it, instead of jumping to the nearest bar.
  const logicalOfTime = (t: number): number | null => {
    const ts = barTimesRef.current;
    const n = ts.length;
    if (n < 2) return null;
    if (t <= ts[0]) return (t - ts[0]) / Math.max(1, ts[1] - ts[0]);
    if (t >= ts[n - 1]) return n - 1 + (t - ts[n - 1]) / Math.max(1, ts[n - 1] - ts[n - 2]);
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= t) lo = mid;
      else hi = mid;
    }
    return lo + (t - ts[lo]) / Math.max(1, ts[hi] - ts[lo]);
  };

  const timeOfLogical = (l: number): number | null => {
    const ts = barTimesRef.current;
    const n = ts.length;
    if (n < 2) return null;
    if (l <= 0) return Math.round(ts[0] + l * Math.max(1, ts[1] - ts[0]));
    if (l >= n - 1) return Math.round(ts[n - 1] + (l - (n - 1)) * Math.max(1, ts[n - 1] - ts[n - 2]));
    const i = Math.floor(l);
    return Math.round(ts[i] + (l - i) * Math.max(1, ts[i + 1] - ts[i]));
  };

  const xOfTime = (t: number): number | null => {
    const l = logicalOfTime(t);
    if (l == null) return null;
    const x = chartRef.current?.timeScale().logicalToCoordinate(l as Logical);
    return x == null ? null : Number(x);
  };

  const timeOfX = (x: number): number | null => {
    const l = chartRef.current?.timeScale().coordinateToLogical(x);
    return l == null ? null : timeOfLogical(Number(l));
  };

  // Repaint the overlay. Cheap enough to run on every frame the view changes: a few hundred
  // line segments on a canvas the size of the chart.
  const paintOverlay = () => {
    const canvas = drawCanvasRef.current;
    const chart = chartRef.current;
    const series = seriesRef.current;
    const cont = containerRef.current;
    if (!canvas || !chart || !series || !cont) return;
    const cw = cont.clientWidth;
    const ch = cont.clientHeight;
    if (cw <= 0 || ch <= 0) return;

    // Back the canvas at device resolution — at 1× a diagonal line on a HiDPI screen looks like a
    // staircase, which is most of what reads as "not smooth".
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
      canvas.width = Math.round(cw * dpr);
      canvas.height = Math.round(ch * dpr);
      canvas.style.width = `${cw}px`;
      canvas.style.height = `${ch}px`;
    }
    const g = canvas.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, cw, ch);

    // Keep ink off the price and time axes.
    const plotW = cw - (chart.priceScale("right").width() || 0);
    const plotH = ch - (chart.timeScale().height() || 0);
    g.save();
    g.beginPath();
    g.rect(0, 0, plotW, plotH);
    g.clip();
    g.lineCap = "round";
    g.lineJoin = "round";

    const yOf = (p: number): number | null => {
      const y = series.priceToCoordinate(p);
      return y == null ? null : Number(y);
    };

    const items = draftRef.current ? [...drawingsRef.current, draftRef.current] : drawingsRef.current;
    for (const d of items) {
      if (d.kind === "h") continue;   // drawn as a real price line so it gets an axis label
      g.strokeStyle = d.color;
      g.lineWidth = 2;

      if (d.kind === "f") {
        const pts: { x: number; y: number }[] = [];
        for (const pt of d.pts) {
          const x = xOfTime(pt.t);
          const y = yOf(pt.p);
          if (x != null && y != null) pts.push({ x, y });
        }
        if (pts.length < 2) {
          if (pts.length === 1) {
            g.beginPath();
            g.arc(pts[0].x, pts[0].y, 1.5, 0, Math.PI * 2);
            g.fillStyle = d.color;
            g.fill();
          }
          continue;
        }
        // Curve through the midpoints instead of joining raw samples corner to corner: the same
        // trick every drawing app uses, and the difference between an ink stroke and a zig-zag.
        g.beginPath();
        g.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < pts.length - 1; i++) {
          g.quadraticCurveTo(pts[i].x, pts[i].y, (pts[i].x + pts[i + 1].x) / 2, (pts[i].y + pts[i + 1].y) / 2);
        }
        g.lineTo(pts[pts.length - 1].x, pts[pts.length - 1].y);
        g.stroke();
        continue;
      }

      const x1 = xOfTime(d.t1);
      const x2 = xOfTime(d.t2);
      const y1 = yOf(d.p1);
      const y2 = yOf(d.p2);
      if (x1 == null || x2 == null || y1 == null || y2 == null) continue;
      g.beginPath();
      g.moveTo(x1, y1);
      g.lineTo(x2, y2);
      g.stroke();
      // Dashed projection to the right edge: a trendline that stops in the past can't show WHERE
      // price will meet it. Dashed, so it stays clear which part you actually drew.
      if (x2 > x1 && x2 < plotW) {
        const slope = (y2 - y1) / (x2 - x1);
        g.save();
        g.setLineDash([5, 4]);
        g.globalAlpha = 0.7;
        g.beginPath();
        g.moveTo(x2, y2);
        g.lineTo(plotW, y2 + slope * (plotW - x2));
        g.stroke();
        g.restore();
      }
    }
    g.restore();
  };

  // Horizontal levels stay real price lines: they span the full width and print their price on the
  // axis, which a canvas stroke would have to reimplement.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    drawLinesRef.current.forEach((l) => series.removePriceLine(l));
    drawLinesRef.current = drawings
      .filter((d): d is Extract<Drawing, { kind: "h" }> => d.kind === "h")
      .map((d) =>
        series.createPriceLine({
          price: d.price, color: d.color, lineWidth: 2, lineStyle: LineStyle.Solid,
          axisLabelVisible: true, title: "✏️",
        }),
      );
  }, [drawings, symbol, timeframe, dataV]);

  // Repaint whenever the picture could have moved. There is no single "view changed" event in
  // lightweight-charts — panning, zooming, autoscale and dragging the price axis are all separate
  // — so we watch a cheap signature of the view each frame and redraw only when it differs.
  useEffect(() => {
    let raf = 0;
    let sig = "";
    const tick = () => {
      const chart = chartRef.current;
      const series = seriesRef.current;
      const cont = containerRef.current;
      if (chart && series && cont) {
        const r = chart.timeScale().getVisibleLogicalRange();
        const next = [
          r?.from, r?.to,
          series.coordinateToPrice(0), series.coordinateToPrice(cont.clientHeight),
          cont.clientWidth, cont.clientHeight, dirtyRef.current,
        ].join("|");
        if (next !== sig) {
          sig = next;
          paintOverlay();
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    dirtyRef.current += 1;   // saved set changed — repaint even if the view is still
  }, [drawings, dataV, symbol, timeframe]);

  // Esc drops the tool and any half-drawn stroke.
  useEffect(() => {
    if (drawMode === "off") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      draftRef.current = null;
      dirtyRef.current += 1;
      setDrawMode("off");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawMode]);

  // Drag the SL/TP line directly on the chart to adjust it, committing to the broker on drop. The
  // handlers read live state from refs, so they're attached once. While dragging we turn off chart
  // scroll/scale so the pane doesn't pan, and the positions-poll rebuild is skipped (guard above).
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const HIT = 7; // px tolerance to grab a line

    const yToPrice = (e: MouseEvent): number | null => {
      const series = seriesRef.current;
      if (!series) return null;
      const y = e.clientY - container.getBoundingClientRect().top;
      const p = series.coordinateToPrice(y);
      return p == null ? null : Number(p);
    };

    const handleAt = (e: MouseEvent): DragHandle | null => {
      const series = seriesRef.current;
      if (!series) return null;
      const y = e.clientY - container.getBoundingClientRect().top;
      let best: DragHandle | null = null;
      let bestDist = HIT;
      for (const h of handlesRef.current) {
        const c = series.priceToCoordinate(h.price);
        if (c == null) continue;
        const d = Math.abs(c - y);
        if (d <= bestDist) { bestDist = d; best = h; }
      }
      return best;
    };

    const onMove = (e: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) {
        // While a drawing tool is armed the cursor is a crosshair and lines aren't grabbable —
        // otherwise clicking to place a point next to an SL would drag the SL instead.
        container.style.cursor = drawModeRef.current !== "off" ? "crosshair" : handleAt(e) ? "ns-resize" : "";
        return;
      }
      const price = yToPrice(e);
      if (price == null) return;
      dragPriceRef.current = price;
      drag.line.applyOptions({ price });
      setDragHint({ label: drag.label, price, pos: drag.refPos });
    };

    const onDown = (e: MouseEvent) => {
      if (drawModeRef.current !== "off") return;
      const handle = handleAt(e);
      if (!handle) return;
      dragRef.current = handle;
      dragPriceRef.current = null;
      chartRef.current?.applyOptions({ handleScroll: false, handleScale: false });
      e.preventDefault();
    };

    const onUp = () => {
      const drag = dragRef.current;
      if (!drag) return;
      dragRef.current = null;
      chartRef.current?.applyOptions({ handleScroll: true, handleScale: true });
      container.style.cursor = "";
      const price = dragPriceRef.current;
      setDragHint(null);
      // Reject a wrong-side level; the line snaps back to the stored value on the next poll.
      if (price == null || !drag.validate(price)) return;
      drag.commit(price);
    };

    const onLeave = () => {
      if (!dragRef.current) container.style.cursor = "";
    };

    container.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    container.addEventListener("mouseleave", onLeave);
    return () => {
      container.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      container.removeEventListener("mouseleave", onLeave);
    };
  }, []);

  // --- drawing gestures ------------------------------------------------------------------------
  // Press–drag–release for both tools. The earlier click-then-click trendline gave no feedback
  // between the two clicks; dragging shows the line following the cursor the whole way.
  const atPointer = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const price = seriesRef.current?.coordinateToPrice(y);
    const t = timeOfX(x);
    if (price == null || t == null) return null;
    return { x, y, t, p: Number(price) };
  };

  const onDrawDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (drawMode === "off") return;
    const at = atPointer(e);
    if (!at) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const color = DRAW_COLORS[drawings.length % DRAW_COLORS.length];
    if (drawMode === "h") {
      saveDrawings((prev) => [...prev, { id: newDrawId(), kind: "h", price: at.p, color }]);
      setDrawMode("off");
      return;
    }
    draftRef.current =
      drawMode === "f"
        ? { id: newDrawId(), kind: "f", pts: [{ t: at.t, p: at.p }], color }
        : { id: newDrawId(), kind: "t", t1: at.t, p1: at.p, t2: at.t, p2: at.p, color };
    lastPxRef.current = { x: at.x, y: at.y };
    paintOverlay();
  };

  const onDrawMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = draftRef.current;
    if (!d) return;
    const at = atPointer(e);
    if (!at) return;
    if (d.kind === "f") {
      // Drop samples under 2px apart: the pointer fires far faster than the hand moves, and the
      // extra points are pure jitter that the curve would faithfully reproduce as wobble.
      const last = lastPxRef.current;
      if (last && Math.hypot(at.x - last.x, at.y - last.y) < 2) return;
      lastPxRef.current = { x: at.x, y: at.y };
      d.pts.push({ t: at.t, p: at.p });
    } else if (d.kind === "t") {
      d.t2 = at.t;
      d.p2 = at.p;
    }
    paintOverlay();
  };

  const onDrawUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const d = draftRef.current;
    draftRef.current = null;
    lastPxRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    if (!d) return;
    // A stray click that never became a line is a mis-click, not a drawing.
    const tooSmall =
      (d.kind === "f" && d.pts.length < 2) || (d.kind === "t" && d.t1 === d.t2 && d.p1 === d.p2);
    dirtyRef.current += 1;
    if (tooSmall) {
      paintOverlay();
      return;
    }
    saveDrawings((prev) => [...prev, d]);
    setDrawMode("off");
  };

  const change = legend ? legend.close - legend.open : 0;
  const changePct = legend && legend.open ? (change / legend.open) * 100 : 0;

  const myPos = (positions ?? []).find((p) => p.symbol.toUpperCase() === symbol.toUpperCase());

  // Reset the view to the clean default: the most recent ~110 bars (big candles) with the price
  // axis auto-fitted — undoing any zoom/pan/pinned-scale for clear visualization.
  const recenter = () => {
    const n = candlesRef.current.length;
    const ts = chartRef.current?.timeScale();
    if (ts && n) {
      const VISIBLE = 110;
      ts.setVisibleLogicalRange({ from: Math.max(0, n - VISIBLE), to: n + 6 });
    }
    seriesRef.current?.priceScale().applyOptions({ autoScale: true });
  };

  return (
    <div ref={rootRef} className={`relative${isFull ? " flex min-h-0 flex-1 flex-col" : ""}`}>
      <div className="mb-2 flex flex-wrap items-center gap-1.5 rounded-lg border border-neutral-800 bg-neutral-900/40 p-1.5">
        {EMA_CONFIG.map(({ period, color }) => (
          <button
            key={period}
            onClick={() => setShowEma((s) => ({ ...s, [period]: !s[period] }))}
            className={`rounded-md px-2 py-0.5 text-xs transition ${showEma[period] ? "bg-neutral-700 text-white" : "text-neutral-500 hover:bg-neutral-800/60"}`}
            style={showEma[period] ? { color } : undefined}
          >
            EMA {period}
          </button>
        ))}
        <button
          onClick={() => setShowRsi((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showRsi ? "bg-neutral-700 text-purple-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
        >
          RSI
        </button>
        <button
          onClick={() => setShowMacd((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showMacd ? "bg-neutral-700 text-blue-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
        >
          MACD
        </button>
        <button
          onClick={() => setShowSt((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showSt ? "bg-neutral-700 text-emerald-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
          title="SuperTrend (ATR 10 ×2.7) — green band = uptrend (support), red band = downtrend (resistance)"
        >
          SuperTrend
        </button>
        <button
          onClick={() => setShowPb((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showPb ? "bg-neutral-700 text-amber-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
          title="Pullback score (0-100) on top of SuperTrend: ▲ buy-the-dip in an uptrend / ▼ sell-the-rally in a downtrend. Marks the bar when confidence >= 70."
        >
          Pullback
        </button>
        <button
          onClick={() => setShowStructure((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showStructure ? "bg-neutral-700 text-sky-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
          title="Market structure: labels swing pivots HH / HL (green = bullish structure) and LH / LL (red = bearish). Higher-highs + higher-lows = uptrend; lower-highs + lower-lows = downtrend — the same swing map the engine reads."
        >
          Structure
        </button>
        <button
          onClick={() => setShowChannel((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showChannel ? "bg-neutral-700 text-violet-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
          title="Regression channel: the algorithmic diagonal trend line (mid) + dynamic resistance (upper) / support (lower) bands. Objective, reproducible version of a hand-drawn trend line/channel. Shown for your read; tested and NOT used to gate trades (it hurt a trend-following engine — in a trend, price rides & breaks the upper band)."
        >
          Channel
        </button>
        {(["1h", "4h", "1d"] as const).map((tf) => {
          const onCls = { "1h": "text-slate-300", "4h": "text-amber-300", "1d": "text-red-300" }[tf];
          return (
            <button
              key={tf}
              onClick={() => setShowSR((s) => ({ ...s, [tf]: !s[tf] }))}
              className={`rounded-md px-2 py-0.5 text-xs transition ${showSR[tf] ? `bg-neutral-700 ${onCls}` : "text-neutral-500 hover:bg-neutral-800/60"}`}
              title={`${tf.toUpperCase()} support/resistance — swing levels from the ${tf} chart (${{ "1h": "slate", "4h": "amber", "1d": "red — strongest" }[tf]}). R = resistance, S = support.`}
            >
              {tf.toUpperCase()} S/R
            </button>
          );
        })}
        <button
          onClick={() => setShowBand((v) => !v)}
          className={`rounded-md px-2 py-0.5 text-xs transition ${showBand ? "bg-neutral-700 text-cyan-300" : "text-neutral-500 hover:bg-neutral-800/60"}`}
          title="EMA20 of highs / lows = a band around price. The SuperTrend Strategy enters on a candle close BEYOND this band in the SuperTrend direction."
        >
          EMA20 H/L
        </button>
        <button
          onClick={loadContext}
          disabled={ctxBusy}
          className="ml-auto rounded bg-neutral-800/50 px-2 py-0.5 text-xs text-sky-300 hover:bg-neutral-700 hover:text-white disabled:opacity-50"
          title="Read: plain-language 'where is price on the map (S/R, channel, structure) + do RSI/volume/ATR confirm?' analysis for this pair. Info only — it does NOT change the engine's decision; it's for your Mode-A call."
        >
          {ctxBusy ? "…" : "🗺️ Read"}
        </button>
        {/* Only appears once the map has drawn levels — the way to clear them after you've closed
            the panel to look at the chart. */}
        {ctxLevels && (
          <button
            onClick={() => setCtxLevels(null)}
            className="rounded bg-sky-950/60 px-2 py-0.5 text-xs text-sky-300 hover:bg-neutral-700 hover:text-white"
            title="Hide the price map's support/resistance levels"
          >
            📍 Levels ✕
          </button>
        )}
        <button
          onClick={loadScenarios}
          disabled={scenBusy}
          className="rounded bg-neutral-800/50 px-2 py-0.5 text-xs text-violet-300 hover:bg-neutral-700 hover:text-white disabled:opacity-50"
          title="AI scenarios: the AI reasons out TWO ranked, scored forward scenarios (anchored to the real S/R + structure) and explains why the chosen one is more likely. Info only — it does NOT change the engine's decision."
        >
          {scenBusy ? "…" : "🤖 Scenarios"}
        </button>
        <button
          onClick={recenter}
          className="rounded bg-neutral-800/50 px-2 py-0.5 text-xs text-neutral-300 hover:bg-neutral-700 hover:text-white"
          title="Recenter — reset to the recent bars with the price axis auto-fitted"
        >
          ⊹ Recenter
        </button>

        {/* Hand-drawn lines: arm a tool, click the chart. Saved per pair and kept until deleted. */}
        <span className="ml-1 flex items-center gap-1 border-l border-neutral-800 pl-2">
          {([
            ["f", "🖊 Pen", "Free hand — press and drag to draw any shape, exactly like a pen on paper."],
            ["t", "╱ Trend", "Trend line — press at the start, drag to the end, release. It carries on dashed to the right edge so you can see where price will meet it."],
            ["h", "─ Level", "Level — one click puts a horizontal line across the whole chart, with its price on the axis."],
          ] as const).map(([mode, label, tip]) => (
            <button
              key={mode}
              onClick={() => { draftRef.current = null; dirtyRef.current += 1; setDrawMode((m) => (m === mode ? "off" : mode)); }}
              className={`rounded px-2 py-0.5 text-xs transition ${drawMode === mode ? "bg-sky-600 text-white" : "bg-neutral-800/50 text-neutral-300 hover:bg-neutral-700 hover:text-white"}`}
              title={tip}
            >
              {label}
            </button>
          ))}
          {drawings.length > 0 && (
            <button
              onClick={() => setDrawList((v) => !v)}
              className={`rounded px-2 py-0.5 text-xs transition ${drawList ? "bg-neutral-700 text-white" : "bg-neutral-800/50 text-neutral-300 hover:bg-neutral-700 hover:text-white"}`}
              title={`${drawings.length} saved drawing${drawings.length === 1 ? "" : "s"} on ${symbol} — open to review or delete`}
            >
              📐 {drawings.length}
            </button>
          )}
        </span>
      </div>

      {ctx && (
        <div
          data-panel
          style={ctxDrag.style}
          className="absolute right-2 top-10 z-30 max-h-[580px] w-[22rem] overflow-y-auto rounded-lg border border-neutral-700 bg-neutral-900/95 p-3 text-xs shadow-xl"
        >
          {/* Grab anywhere on this header bar to move the panel; double-click it to snap back. */}
          <div
            {...ctxDrag.handle}
            // Sticky: the panel scrolls, and a grab handle that scrolls out of reach is no handle.
            className="sticky -top-3 z-10 -mx-3 -mt-3 mb-1.5 flex cursor-move touch-none select-none items-center justify-between border-b border-neutral-800 bg-neutral-900/95 px-3 py-2"
            title="Drag to move this panel · double-click to snap it back to the corner"
          >
            {/* The timeframe is part of the title: the same pair reads completely differently on
                5m and 4h, so a reading without its timeframe is ambiguous. */}
            <span className="font-semibold text-sky-300">
              <span className="mr-1 text-neutral-600">⠿</span>🗺️ Price map — {ctx.symbol}
              <span className="ml-1 rounded bg-sky-950/70 px-1.5 py-0.5 text-[10px] text-sky-300">
                {ctx.timeframe}
              </span>
            </span>
            <span className="flex items-center gap-2">
              {ctxDrag.moved && (
                <button
                  onClick={ctxDrag.reset}
                  className="rounded px-1 text-neutral-600 hover:bg-neutral-800 hover:text-neutral-200"
                  title="Snap back to the top-right corner"
                >
                  ⇱
                </button>
              )}
              {/* The reading is a snapshot of a moving market, so it always says WHEN it was taken —
                  a stale read looks identical to a fresh one otherwise. */}
              {ctxAt && (
                <span className="text-[10px] text-neutral-500" title={ctxAt.toLocaleString()}>
                  read {ctxAt.toLocaleTimeString()}
                </span>
              )}
              <button
                onClick={loadContext}
                disabled={ctxBusy}
                className="rounded px-1 text-neutral-500 hover:bg-neutral-800 hover:text-sky-300 disabled:opacity-50"
                title="Re-read the chart now — recomputes every factor and the levels from the latest candles"
              >
                {ctxBusy ? "…" : "↻"}
              </button>
              <button onClick={() => setCtx(null)} className="text-neutral-500 hover:text-neutral-200" title="Close">✕</button>
            </span>
          </div>
          <div className="mb-1 rounded bg-neutral-800/60 px-2 py-1 text-center text-sm font-semibold">
            {ctx.overall_bias}
          </div>
          {/* The count of which way each factor leans — readable in two seconds, before you read
              the rows properly. */}
          {ctx.tally && (
            <div className="mb-2 text-center text-[11px] text-neutral-500">{ctx.tally}</div>
          )}

          {/* Timeframe comparison, above the detail: whether you're with or against the bigger
              picture changes whether the setup below is even worth taking. */}
          {(ctx.tf_compare?.length ?? 0) > 0 && (
            <div className="mb-2 rounded border border-neutral-800 bg-neutral-800/20 p-1.5">
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-neutral-500">
                Timeframe check
              </div>
              {ctx.tf_compare!.map((r) => (
                <div key={r.tf} className="flex items-baseline gap-1.5 py-0.5">
                  <span>{r.signal}</span>
                  <span
                    className={`w-14 shrink-0 font-semibold ${r.is_chart ? "text-sky-300" : "text-neutral-300"}`}
                  >
                    {r.tf}
                    {r.is_chart && <span className="ml-0.5 text-[9px] text-neutral-500">chart</span>}
                  </span>
                  <span
                    className={`w-14 shrink-0 font-medium ${
                      r.verdict === "bullish" ? "text-bull" : r.verdict === "bearish" ? "text-bear" : "text-neutral-500"
                    }`}
                  >
                    {r.verdict}
                  </span>
                  <span className="min-w-0 flex-1 text-[11px] text-neutral-500">{r.note}</span>
                </div>
              ))}
              {ctx.alignment && (
                <div
                  className={`mt-1 border-t border-neutral-800 pt-1 font-medium ${
                    ctx.alignment.startsWith("ALIGNED")
                      ? "text-bull"
                      : ctx.alignment.startsWith("CONFLICTED")
                        ? "text-bear"
                        : "text-amber-300"
                  }`}
                >
                  {ctx.alignment}
                </div>
              )}
            </div>
          )}

          {/* Every factor states its reading AND which side that reading argues for. A number on
              its own ("RSI 72") is trivia; "72 and falling — buyers ran out" is a trade. */}
          <div className="mb-2 space-y-1.5">
            {ctx.scorecard.map((s) => {
              const side = s.implies?.startsWith("supports LONG")
                ? "text-bull"
                : s.implies?.startsWith("supports SHORT")
                  ? "text-bear"
                  : "text-neutral-500";
              return (
                <div key={s.factor} className="rounded bg-neutral-800/30 px-2 py-1">
                  <div className="flex items-baseline gap-1.5">
                    <span>{s.signal}</span>
                    <span className="font-semibold text-neutral-200">{s.factor}</span>
                    <span className="text-neutral-400">{s.note}</span>
                  </div>
                  {s.implies && <div className={`mt-0.5 pl-5 font-medium ${side}`}>→ {s.implies}</div>}
                </div>
              );
            })}
          </div>

          {/* "Trade the break of the nearest level" is only actionable if you can SEE the levels. */}
          {((ctx.resistance_ladder?.length ?? 0) > 0 || (ctx.support_ladder?.length ?? 0) > 0) && (
            <button
              onClick={() =>
                setCtxLevels((v) =>
                  v ? null : { res: ctx.resistance_ladder ?? [], sup: ctx.support_ladder ?? [] },
                )
              }
              className={`mb-2 w-full rounded border py-1 font-medium transition ${
                ctxLevels
                  ? "border-sky-600 bg-sky-950/50 text-sky-200"
                  : "border-neutral-700 text-neutral-300 hover:border-sky-700 hover:text-sky-200"
              }`}
            >
              {ctxLevels ? "📍 Hide these levels" : "📍 Show these levels on the chart"}
            </button>
          )}
          {ctx.scenarios.length > 0 && (
            <div className="mb-2 space-y-1 border-t border-neutral-800 pt-1.5">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500">Scenarios</div>
              {ctx.scenarios.map((s, i) => (
                <div key={i}>
                  <span className="font-semibold text-neutral-200">{s.prob} {s.label}</span>
                  <span className="text-neutral-400"> — {s.text}</span>
                </div>
              ))}
            </div>
          )}
          {ctx.playbook && (
            <div className="mb-2 rounded border border-sky-800/50 bg-sky-950/30 px-2 py-1 text-sky-200">
              <span className="font-semibold">Playbook: </span>{ctx.playbook}
            </div>
          )}
          {ctx.invalidation && <div className="mb-2 text-amber-300">⚠ {ctx.invalidation}</div>}
          <div className="border-t border-neutral-800 pt-1.5 text-[10px] leading-relaxed text-neutral-600">
            {/* The dots mean WHICH SIDE, not good/bad. Without saying so, a red RSI reads as a
                warning against a short when it is in fact the argument for one. */}
            🟢 argues for a LONG · 🔴 argues for a SHORT · 🟡 no directional edge.
            <br />
            Info only — it does NOT change the engine's decision; it's for your Mode‑A call.
          </div>
        </div>
      )}

      {scen && (
        <div className="absolute left-2 top-10 z-30 max-h-[580px] w-[23rem] overflow-y-auto rounded-lg border border-violet-800/60 bg-neutral-900/95 p-3 shadow-xl">
          <button
            onClick={() => setScen(null)}
            className="absolute right-2 top-2 text-neutral-500 hover:text-neutral-200"
            title="Close"
          >
            ✕
          </button>
          <ScenarioCard read={scen} levelsShown={scenLevelsShown}
                        onShowLevels={() => onToggleScenLevels?.(
                          scen ? { support: scen.nearest_support ?? null, resistance: scen.nearest_resistance ?? null,
                                   target: scen.target ?? null, invalidation: scen.invalidation_price ?? null } : null)} />
        </div>
      )}

      {/* Review + delete. Every drawing is listed with what it is and where, so you can remove one
          precisely — a chart you can only clear wholesale is one you stop annotating. */}
      {drawList && drawings.length > 0 && (
        <div className="absolute right-2 top-10 z-40 max-h-[420px] w-[19rem] overflow-y-auto rounded-lg border border-neutral-700 bg-neutral-900/95 p-3 text-xs shadow-xl">
          <div className="mb-2 flex items-center justify-between">
            <span className="font-semibold text-sky-300">📐 My drawings — {symbol}</span>
            <button onClick={() => setDrawList(false)} className="text-neutral-500 hover:text-neutral-200" title="Close">✕</button>
          </div>
          <ul className="space-y-1">
            {drawings.map((d) => (
              <li key={d.id} className="flex items-center gap-2 rounded bg-neutral-800/50 px-2 py-1">
                <span className="h-0.5 w-4 shrink-0 rounded" style={{ backgroundColor: d.color }} />
                <span className="min-w-0 flex-1 truncate text-neutral-300">
                  {d.kind === "h" ? (
                    <>Level <span className="tabular-nums text-neutral-100">{fmtPrice(d.price)}</span></>
                  ) : d.kind === "t" ? (
                    <>
                      Trend{" "}
                      <span className="tabular-nums text-neutral-100">{fmtPrice(d.p1)} → {fmtPrice(d.p2)}</span>
                      <span className="ml-1 text-neutral-500">{new Date(d.t1 * 1000).toLocaleDateString()}</span>
                    </>
                  ) : (
                    <>
                      Drawing{" "}
                      <span className="text-neutral-500">{d.pts.length} pts</span>
                      <span className="ml-1 text-neutral-500">
                        {d.pts.length ? new Date(d.pts[0].t * 1000).toLocaleDateString() : ""}
                      </span>
                    </>
                  )}
                </span>
                <button
                  onClick={() => saveDrawings((prev) => prev.filter((x) => x.id !== d.id))}
                  className="shrink-0 rounded px-1 text-neutral-500 hover:bg-bear/20 hover:text-bear"
                  title="Delete this drawing"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
          <button
            onClick={() => { saveDrawings(() => []); setDrawList(false); }}
            className="mt-2 w-full rounded border border-neutral-700 py-1 text-neutral-400 hover:border-bear/60 hover:text-bear"
          >
            Delete all {drawings.length}
          </button>
          <p className="mt-2 text-[10px] leading-snug text-neutral-500">
            Saved on this device for {symbol}, on every timeframe, until you delete them.
          </p>
        </div>
      )}

      {/* The OHLC readout, the drag hint and the full-screen button are anchored to the CHART, not
          to the component. Anchoring them to the component meant guessing the toolbar's height —
          and the moment the toolbar wrapped to a second row, that row landed on top of the OHLC
          numbers. A wrapping toolbar now just pushes the chart (and everything on it) down. */}
      <div className={`relative${isFull ? " min-h-0 flex-1" : ""}`}>
        {legend && (
          <div className="pointer-events-none absolute left-2 top-2 z-10 flex gap-2 text-xs tabular-nums">
            <span className="text-neutral-400">O<span className="ml-0.5 text-neutral-200">{fmtPrice(legend.open)}</span></span>
            <span className="text-neutral-400">H<span className="ml-0.5 text-neutral-200">{fmtPrice(legend.high)}</span></span>
            <span className="text-neutral-400">L<span className="ml-0.5 text-neutral-200">{fmtPrice(legend.low)}</span></span>
            <span className="text-neutral-400">C<span className="ml-0.5 text-neutral-200">{fmtPrice(legend.close)}</span></span>
            <span className={change >= 0 ? "text-bull" : "text-bear"}>
              {change >= 0 ? "+" : ""}{fmtPrice(change)} ({changePct >= 0 ? "+" : ""}{changePct.toFixed(2)}%)
            </span>
          </div>
        )}

        {dragHint && (
          <div
            className={`pointer-events-none absolute left-1/2 top-2 z-20 -translate-x-1/2 rounded px-2 py-1 text-xs font-semibold text-white shadow ${
              dragHint.label === "SL" ? "bg-bear/90" : dragHint.label === "TP" ? "bg-bull/90" : "bg-amber-500/90"
            }`}
          >
            {dragHint.label} → {fmtPrice(dragHint.price)}{" "}
            {dragHint.pos && usdAtLevel(dragHint.pos, dragHint.price) && (
              <span>({usdAtLevel(dragHint.pos, dragHint.price)})</span>
            )}
          </div>
        )}

        {/* Says exactly which click comes next — a tool that's armed but silent gets forgotten,
            and the next click on the chart then becomes a surprise. */}
        {drawMode !== "off" && (
          <div className="pointer-events-none absolute left-1/2 top-2 z-20 -translate-x-1/2 rounded bg-sky-600/90 px-2 py-1 text-xs font-semibold text-white shadow">
            {drawMode === "h"
              ? "Click the price for your level"
              : drawMode === "f"
                ? "Press and drag to draw"
                : "Press at the start, drag to the end, release"}
            <span className="ml-2 font-normal text-sky-100/80">Esc to cancel</span>
          </div>
        )}

        {/* Sits over the chart's top-right corner (clear of the ~72px price axis) rather than at
            the end of the toolbar, where it was one more chip competing for a row that wraps. */}
        <button
          onClick={toggleFullscreen}
          className="absolute right-[78px] top-1.5 z-20 rounded border border-neutral-700 bg-neutral-900/80 px-2 py-1 text-xs text-neutral-300 shadow backdrop-blur-sm transition hover:border-sky-600 hover:bg-neutral-800 hover:text-white"
          title={isFull ? "Exit full screen (Esc)" : "Full screen — price, RSI and MACD together"}
        >
          {isFull ? "⤡ Exit" : "⛶ Full screen"}
        </button>

        <div ref={containerRef} className={isFull ? "h-full w-full" : "h-[600px] w-full"} />

        {/* The ink layer. Transparent to the mouse unless a tool is armed, so the crosshair, the
            SL/TP drags and panning all behave normally the rest of the time. */}
        <canvas
          ref={drawCanvasRef}
          onPointerDown={onDrawDown}
          onPointerMove={onDrawMove}
          onPointerUp={onDrawUp}
          onPointerCancel={onDrawUp}
          className={`absolute inset-0 z-10 ${drawMode === "off" ? "pointer-events-none" : "cursor-crosshair touch-none"}`}
        />
      </div>
      {(() => {
        const hasPos = !!myPos;
        const hasProp = !!proposal && proposal.direction !== "no_trade" && !hasPos;
        const hasArmed = (armed ?? []).some((a) => a.symbol.toUpperCase() === symbol.toUpperCase());
        if (!hasPos && !hasProp && !hasArmed) return null;
        return (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-neutral-400">
            {hasPos && (
              <span className="inline-flex items-center gap-1"><LineSwatch dash="" /> Open position</span>
            )}
            {hasProp && (
              <span className="inline-flex items-center gap-1"><LineSwatch dash="4 3" /> Proposal</span>
            )}
            {hasArmed && (
              <span className="inline-flex items-center gap-1"><LineSwatch dash="8 4" /> Armed ⚡</span>
            )}
            <span className="text-neutral-700">|</span>
            {(hasPos || hasProp) && <span className="text-blue-400">— Entry</span>}
            {/* The arrow is the only thing on the chart that shows WHEN you got in, so name it. */}
            {hasPos && (
              <span className="inline-flex items-center gap-1 text-blue-400">
                {myPos?.direction === "long" ? "▲" : "▼"} Entry candle
              </span>
            )}
            {hasArmed && <span className="text-amber-400">— Arm trigger</span>}
            {/* Only meaningful when a two-stage arm is on this chart, so don't clutter otherwise. */}
            {(armed ?? []).some(
              (a) => a.symbol.toUpperCase() === symbol.toUpperCase() && a.break_level != null,
            ) && <span className="text-blue-300">┈ Must break first</span>}
            <span className="text-bear">— Stop</span>
            <span className="text-bull">— Target</span>
          </div>
        );
      })()}
      {/* Each pane keeps its dragged height in full screen too, so the price chart (flex-1) simply
          absorbs whatever height the indicators don't claim. */}
      {showRsi && (
        <>
          <PaneResizer height={rsiH} onChange={setRsiH} label="RSI" />
          <div className="relative shrink-0" style={{ height: rsiH }}>
            <span className="pointer-events-none absolute left-2 top-1 z-10 text-xs text-purple-300">RSI 14</span>
            <div ref={rsiContainerRef} className="h-full w-full" />
          </div>
        </>
      )}
      {showMacd && (
        <>
          <PaneResizer height={macdH} onChange={setMacdH} label="MACD" />
          <div className="relative shrink-0" style={{ height: macdH }}>
            <span className="pointer-events-none absolute left-2 top-1 z-10 flex gap-2 text-xs">
              <span className="text-neutral-400">MACD 12 26 9</span>
              <span className="text-blue-300">MACD</span>
              <span className="text-amber-400">signal</span>
            </span>
            <div ref={macdContainerRef} className="h-full w-full" />
          </div>
        </>
      )}
    </div>
  );
}
