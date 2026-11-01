import { useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import {
  ColorType,
  createChart,
  LineStyle,
  type AutoscaleInfo,
  type BaselineData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type MouseEventParams,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { api } from "../api/client";
import type { AssetClass, Candle, OrderFlow, OrderFlowBar } from "../types";

/**
 * THE ORDER-FLOW PANE: who pushed the price, buyers or sellers, candle by candle and over the last N.
 *
 * Exness quotes carry no traded volume and no buyer or seller, and the bid and ask move together. So
 * the backend (GET /api/market/order-flow) sorts every tick by the tick rule: one that lifted the bid
 * is a BUY tick, one that dropped it a SELL tick. "Ticks" counts them (the volume each side had);
 * "Move" adds up how far each side pushed the price.
 *
 *   ━ line, top band       - buyers' share over the last N candles; the dotted line is 50%
 *   ▮ light green up / red down, lower band - each side of the candle
 *   ▮ solid                                  - the side that won it, by how much
 *
 * The "next 5" chip checks, on the candles loaded, whether a strong share was followed by price going
 * the same way five candles later. It is a look at the data on screen, not a backtest.
 */

const WINDOWS = [5, 10, 20, 50, 100];
const AHEAD = 5;              // the follow-through check looks this many candles on
const POLL_MS = 2000;         // the newest bars are re-read this often while the pane is open
const MIN_HALF = 5;           // the share line's scale spans at least 45-55%, so a small wobble stays small
// Two bands under the legend, so the line never runs through the bars: the share on top, the bars below.
const SHARE_MARGINS = { top: 0.22, bottom: 0.5 };
const BAR_MARGINS = { top: 0.56, bottom: 0.04 };
const BUY_SOFT = "rgba(38,166,154,0.28)";
const SELL_SOFT = "rgba(239,83,80,0.28)";
const BUY_NET = "rgba(38,166,154,0.95)";
const SELL_NET = "rgba(239,83,80,0.95)";

const METHOD =
  "Estimated from the price ticks. Exness sends no traded volume and no buyer or seller, and its bid and ask " +
  "move together, so each tick that lifted the price counts as buying and each that dropped it as selling " +
  "(the tick rule). Ticks: how many times each side moved the price. Move: how far each side moved it. " +
  "Line (top): buyers' share over the last N candles, the dotted line is 50%. Bars (below): light = each side " +
  "of the candle, solid = the side that won it.";

type Mode = "ticks" | "move";
type Point = { time: UTCTimestamp; row: OrderFlowBar | null; close: number };
type Test = { hiCut: number; loCut: number; upN: number; upH: number; dnN: number; dnH: number; baseN: number; baseUp: number };

interface Props {
  symbol: string;
  assetClass: AssetClass;
  timeframe: string;
  mainChart: MutableRefObject<IChartApi | null>;   // the price chart: the panes scroll and hover together
  candles: MutableRefObject<Candle[]>;
  lastBarTime: () => number | null;                 // the newest bar on the price chart, one the live feed opened included
  dataV: number;                                    // bumps when the candles reload
  reloadTick: number;                               // bumps when a bar opens
  toTime: (c: Candle) => UTCTimestamp;
  toDisp: (utcSec: number) => number;
  timeAxis: boolean;
  secondsVisible: boolean;
  storeKey: (k: string) => string;
}

function useStored<T>(key: string, initial: T) {
  const [v, setV] = useState<T>(() => {
    try {
      const s = localStorage.getItem(key);
      return s === null ? initial : (JSON.parse(s) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(v));
    } catch {
      /* storage unavailable: the setting just isn't kept */
    }
  }, [key, v]);
  return [v, setV] as const;
}

const utcOf = (c: Candle) =>
  Math.floor(Date.parse(/[Zz]|[+-]\d{2}:?\d{2}$/.test(c.ts) ? c.ts : `${c.ts}Z`) / 1000);

// One point per candle of the price chart, so both time axes hold the same bars (the panes scroll
// together by bar index). A newer bar the live feed has already drawn up there is added at the end.
function alignPoints(candles: Candle[], rows: Map<number, OrderFlowBar>, toTime: Props["toTime"],
                     toDisp: Props["toDisp"], lastBar: number | null): Point[] {
  const pts: Point[] = candles.map((c) => ({ time: toTime(c), row: rows.get(utcOf(c)) ?? null, close: c.close }));
  const lastUtc = candles.length ? utcOf(candles[candles.length - 1]) : -Infinity;
  if (lastBar != null) {
    for (const u of [...rows.keys()].filter((x) => x > lastUtc).sort((x, y) => x - y)) {
      const time = toDisp(u) as UTCTimestamp;
      if (time > lastBar) break;
      pts.push({ time, row: rows.get(u) ?? null, close: NaN });
    }
  }
  return pts;
}

const sides = (r: OrderFlowBar, mode: Mode): [number, number] => (mode === "ticks" ? [r[1], r[2]] : [r[4], r[5]]);

// Buyers' share of the last n candles, at each candle: null until half the window has flow.
function rollingShare(pts: Point[], mode: Mode, n: number): (number | null)[] {
  const out: (number | null)[] = [];
  let a = 0, b = 0, have = 0;
  for (let i = 0; i < pts.length; i++) {
    const r = pts[i].row;
    if (r) {
      const [x, y] = sides(r, mode);
      a += x; b += y; have++;
    }
    const old = i >= n ? pts[i - n].row : null;
    if (old) {
      const [x, y] = sides(old, mode);
      a -= x; b -= y; have--;
    }
    out.push(have >= Math.ceil(n / 2) && a + b > 0 ? (100 * a) / (a + b) : null);
  }
  return out;
}

// Was a strong share followed by price going the same way AHEAD candles later? "Strong" is the top and
// bottom fifth of the shares seen here, since how far the share strays from 50% depends on the
// timeframe and N. The newest candle is still forming and is left out.
function followThrough(pts: Point[], share: (number | null)[]): Test | null {
  const idx: number[] = [];
  for (let i = 0; i + AHEAD < pts.length - 1; i++) {
    if (share[i] != null && Number.isFinite(pts[i].close) && Number.isFinite(pts[i + AHEAD].close)) idx.push(i);
  }
  if (idx.length < 40) return null;
  const sorted = idx.map((i) => share[i] as number).sort((x, y) => x - y);
  const hiCut = sorted[Math.floor(0.8 * (sorted.length - 1))];
  const loCut = sorted[Math.ceil(0.2 * (sorted.length - 1))];
  const t: Test = { hiCut, loCut, upN: 0, upH: 0, dnN: 0, dnH: 0, baseN: 0, baseUp: 0 };
  for (const i of idx) {
    const s = share[i] as number;
    const fwd = pts[i + AHEAD].close - pts[i].close;
    t.baseN++;
    if (fwd > 0) t.baseUp++;
    if (s >= hiCut && s > 50) {
      t.upN++;
      if (fwd > 0) t.upH++;
    } else if (s <= loCut && s < 50) {
      t.dnN++;
      if (fwd < 0) t.dnH++;
    }
  }
  return t;
}

const pct = (h: number, n: number) => (n ? Math.round((100 * h) / n) : null);

export function OrderFlowPane({ symbol, assetClass, timeframe, mainChart, candles, lastBarTime, dataV, reloadTick,
                                toTime, toDisp, timeAxis, secondsVisible, storeKey }: Props) {
  const [mode, setMode] = useStored<Mode>(storeKey("chart.flowMode"), "ticks");
  const [n, setN] = useStored<number>(storeKey("chart.flowN"), 20);
  const [flowV, setFlowV] = useState(0);
  const [meta, setMeta] = useState<OrderFlow | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [main, setMain] = useState<IChartApi | null>(null);

  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const buyRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const sellRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const netRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const shareRef = useRef<ISeriesApi<"Baseline"> | null>(null);
  const rowsRef = useRef(new Map<number, OrderFlowBar>());
  const keyRef = useRef("");                 // which chart rowsRef holds
  const fmtRef = useRef<(v: number) => string>((v) => String(v));
  const overPane = useRef(false);            // only a drag or zoom ON this pane moves the price chart
  const halfRef = useRef(MIN_HALF);          // the share band's half-height in %, for its axis labels

  const digits = meta?.digits ?? 2;
  const fmtAmount = (v: number) =>
    mode === "ticks" ? Math.round(v).toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: digits });
  fmtRef.current = fmtAmount;

  // The pane's own chart, made once.
  useEffect(() => {
    if (!boxRef.current) return;
    const chart = createChart(boxRef.current, {
      autoSize: true,
      // The TradingView attribution stays on the price chart (see the RSI pane in Chart.tsx).
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a3a3a3", attributionLogo: false },
      grid: { vertLines: { color: "#1f1f1f" }, horzLines: { visible: false } },
      timeScale: { timeVisible: true, secondsVisible, borderColor: "#404040", visible: timeAxis },
      // The axis is the share's (in %). The bars have no labels: their numbers are in the legend.
      rightPriceScale: { borderColor: "#404040", minimumWidth: 72, scaleMargins: SHARE_MARGINS },
      crosshair: { mode: 1 },
    });
    // Both sides on one overlay scale centred on 0; a value reads without its sign (a sell bar points down).
    const centred = (orig: () => AutoscaleInfo | null): AutoscaleInfo | null => {
      const r = orig();
      if (!r) return r;
      const m = Math.max(Math.abs(r.priceRange.minValue), Math.abs(r.priceRange.maxValue)) || 1;
      return { ...r, priceRange: { minValue: -m, maxValue: m } };
    };
    const hist = {
      priceScaleId: "bars", priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: centred,
      priceFormat: { type: "custom" as const, minMove: 1e-8, formatter: (p: number) => fmtRef.current(Math.abs(p)) },
    };
    buyRef.current = chart.addHistogramSeries({ ...hist, color: BUY_SOFT });
    sellRef.current = chart.addHistogramSeries({ ...hist, color: SELL_SOFT });
    netRef.current = chart.addHistogramSeries(hist);
    netRef.current.createPriceLine({ price: 0, color: "#525252", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
    chart.priceScale("bars").applyOptions({ scaleMargins: BAR_MARGINS });
    // The axis spans the whole pane, so its labels beside the bars band are left blank.
    shareRef.current = chart.addBaselineSeries({
      baseValue: { type: "price", price: 50 }, lineWidth: 2,
      priceFormat: {
        type: "custom", minMove: 0.1,
        formatter: (p: number) => (Math.abs(p - 50) <= halfRef.current + 1e-6 ? `${+p.toFixed(1)}%` : ""),
      },
      topLineColor: "#34d399", topFillColor1: "rgba(52,211,153,0.12)", topFillColor2: "rgba(52,211,153,0.02)",
      bottomLineColor: "#fb7185", bottomFillColor1: "rgba(251,113,133,0.02)", bottomFillColor2: "rgba(251,113,133,0.12)",
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
      autoscaleInfoProvider: (orig: () => AutoscaleInfo | null): AutoscaleInfo | null => {
        const r = orig();
        if (!r) return r;
        const d = Math.max(MIN_HALF, Math.abs(r.priceRange.minValue - 50), Math.abs(r.priceRange.maxValue - 50));
        halfRef.current = d;
        return { ...r, priceRange: { minValue: 50 - d, maxValue: 50 + d } };
      },
    });
    shareRef.current.createPriceLine({ price: 50, color: "#525252", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: "" });
    chart.subscribeCrosshairMove((p: MouseEventParams) => setHover(p.time != null ? (p.time as number) : null));
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      buyRef.current = sellRef.current = netRef.current = null;
      shareRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.applyOptions({ timeScale: { visible: timeAxis, secondsVisible } });
  }, [timeAxis, secondsVisible]);

  // The price chart is made in the parent's effect, which runs after this one on the first mount: pick
  // it up once the candles have loaded.
  useEffect(() => {
    if (mainChart.current !== main) setMain(mainChart.current);
  }, [dataV, main, mainChart]);

  // Scroll and zoom together, and show the candle under the price chart's crosshair here too.
  useEffect(() => {
    const pane = chartRef.current;
    if (!main || !pane) return;
    let syncing = false;
    const toPane = (r: LogicalRange | null) => {
      if (syncing || !r) return;
      syncing = true;
      pane.timeScale().setVisibleLogicalRange(r);
      syncing = false;
    };
    const toMain = (r: LogicalRange | null) => {
      if (syncing || !r || !overPane.current) return;
      syncing = true;
      main.timeScale().setVisibleLogicalRange(r);
      syncing = false;
    };
    const onMain = (p: MouseEventParams) => setHover(p.time != null ? (p.time as number) : null);
    main.timeScale().subscribeVisibleLogicalRangeChange(toPane);
    pane.timeScale().subscribeVisibleLogicalRangeChange(toMain);
    main.subscribeCrosshairMove(onMain);
    toPane(main.timeScale().getVisibleLogicalRange());
    return () => {
      try {
        main.timeScale().unsubscribeVisibleLogicalRangeChange(toPane);
        main.unsubscribeCrosshairMove(onMain);
      } catch {
        /* the price chart is already gone */
      }
      pane.timeScale().unsubscribeVisibleLogicalRangeChange(toMain);
    };
  }, [main]);

  // The whole series when the chart changes or a bar opens. Another pair's bars are dropped at once,
  // so they never sit under this pair's candles.
  useEffect(() => {
    const key = `${symbol}|${timeframe}`;
    if (keyRef.current !== key) {
      rowsRef.current = new Map();
      keyRef.current = "";
      setMeta(null);
      setFlowV((v) => v + 1);
    }
    let dead = false;
    api
      .orderFlow(symbol, assetClass, timeframe, 400)
      .then((f) => {
        if (dead) return;
        rowsRef.current = new Map(f.bars.map((b) => [b[0], b]));
        keyRef.current = key;
        setMeta(f);
        setErr(null);
        setFlowV((v) => v + 1);
      })
      .catch((e: unknown) => {
        if (!dead && keyRef.current !== key) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => { dead = true; };
  }, [symbol, assetClass, timeframe, reloadTick]);

  // Then the newest bars, every POLL_MS (not while the tab is hidden).
  useEffect(() => {
    const key = `${symbol}|${timeframe}`;
    let dead = false;
    let busy = false;
    const id = window.setInterval(() => {
      if (busy || document.visibilityState === "hidden" || keyRef.current !== key) return;
      busy = true;
      api
        .orderFlow(symbol, assetClass, timeframe, 3)
        .then((f) => {
          if (dead || keyRef.current !== key) return;
          for (const b of f.bars) rowsRef.current.set(b[0], b);
          setFlowV((v) => v + 1);
        })
        .catch(() => { /* one missed read; the next catches up */ })
        .finally(() => { busy = false; });
    }, POLL_MS);
    return () => { dead = true; window.clearInterval(id); };
  }, [symbol, assetClass, timeframe]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const pts = useMemo(() => alignPoints(candles.current, rowsRef.current, toTime, toDisp, lastBarTime()), [flowV, dataV]);
  const share = useMemo(() => rollingShare(pts, mode, n), [pts, mode, n]);
  const test = useMemo(() => followThrough(pts, share), [pts, share]);

  useEffect(() => {
    const buy = buyRef.current, sell = sellRef.current, net = netRef.current, line = shareRef.current;
    if (!buy || !sell || !net || !line) return;
    const b: (HistogramData | WhitespaceData)[] = [];
    const s: (HistogramData | WhitespaceData)[] = [];
    const d: (HistogramData | WhitespaceData)[] = [];
    const l: (BaselineData | WhitespaceData)[] = [];
    pts.forEach((p, i) => {
      if (p.row) {
        const [x, y] = sides(p.row, mode);
        b.push({ time: p.time, value: x });
        s.push({ time: p.time, value: -y });
        d.push({ time: p.time, value: x - y, color: x >= y ? BUY_NET : SELL_NET });
      } else {
        b.push({ time: p.time });
        s.push({ time: p.time });
        d.push({ time: p.time });
      }
      const v = share[i];
      l.push(v != null ? { time: p.time, value: v } : { time: p.time });
    });
    buy.setData(b);
    sell.setData(s);
    net.setData(d);
    line.setData(l);
    const r = main?.timeScale().getVisibleLogicalRange();
    if (r) chartRef.current?.timeScale().setVisibleLogicalRange(r);
  }, [pts, share, mode, main]);

  // The legend reads the hovered candle, or the newest one.
  const at = (() => {
    if (!pts.length) return -1;
    if (hover != null) {
      const i = pts.findIndex((p) => (p.time as number) === hover);
      if (i >= 0) return i;
    }
    return pts.length - 1;
  })();
  const win = (() => {
    if (at < 0) return null;
    let a = 0, b = 0;
    for (let i = Math.max(0, at - n + 1); i <= at; i++) {
      const r = pts[i].row;
      if (r) {
        const [x, y] = sides(r, mode);
        a += x; b += y;
      }
    }
    return a + b > 0 ? { a, b, share: share[at] ?? (100 * a) / (a + b) } : null;
  })();
  const bar = at >= 0 ? pts[at].row : null;
  const unit = mode === "ticks" ? "ticks" : "of price move";

  const testView = (() => {
    if (!test) return null;
    const up = pct(test.upH, test.upN);
    const dn = pct(test.dnH, test.dnN);
    if (up == null || dn == null) return null;
    const tone = up >= 55 && dn >= 55 ? "follows" : up <= 45 && dn <= 45 ? "fades" : "no edge";
    const cls = tone === "follows" ? "bg-emerald-500/15 text-emerald-300" : tone === "fades" ? "bg-violet-500/15 text-violet-300" : "bg-neutral-800 text-neutral-400";
    const tip =
      `On the candles loaded: when buyers had one of their strongest shares over ${n} candles (${test.hiCut.toFixed(1)}% or more), ` +
      `price was higher ${AHEAD} candles later ${test.upH} times in ${test.upN} (${up}%). When sellers had one of theirs ` +
      `(buyers ${test.loCut.toFixed(1)}% or less), it was lower ${test.dnH} times in ${test.dnN} (${dn}%). Any candle: higher ` +
      `${pct(test.baseUp, test.baseN)}% of the time. Above 50% = the pressure carried on; below = price turned back. ` +
      `"Strongest" is the top and bottom fifth of the shares seen here. Neighbouring readings overlap and the sample is small, ` +
      `so this swings from day to day: a look at the data on screen, not a backtest.`;
    return { up, dn, tone, cls, tip };
  })();

  return (
    <div
      className="relative h-full w-full"
      onPointerEnter={() => { overPane.current = true; }}
      onPointerLeave={() => { overPane.current = false; setHover(null); }}
    >
      <div className="pointer-events-none absolute inset-x-2 top-1 z-10 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
        <span className="font-semibold text-sky-300">Order flow</span>
        <span className="pointer-events-auto inline-flex overflow-hidden rounded border border-neutral-700" role="group" aria-label="Measure">
          {(["ticks", "move"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              aria-pressed={mode === m}
              title={m === "ticks" ? "Count the ticks each side made: the volume each side had" : "Add up how far each side moved the price"}
              className={`px-1.5 leading-4 ${mode === m ? "bg-sky-500/80 font-semibold text-neutral-950" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
            >
              {m === "ticks" ? "Ticks" : "Move"}
            </button>
          ))}
        </span>
        <label className="pointer-events-auto inline-flex items-center gap-1 text-neutral-400">
          last
          <select
            value={n}
            onChange={(e) => setN(Number(e.target.value))}
            className="rounded bg-neutral-800 px-0.5 text-neutral-100"
            aria-label="Candles in the window"
          >
            {WINDOWS.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        {win && (
          <span
            className="pointer-events-auto inline-flex items-center gap-1"
            title={`${fmtAmount(win.a)} ${unit} by buyers vs ${fmtAmount(win.b)} by sellers over the last ${n} candles${hover != null ? " up to the one under the cursor" : ""}`}
          >
            <span className="font-semibold text-bull">Buyers {win.share.toFixed(1)}%</span>
            <span className="relative h-1.5 w-20 overflow-hidden rounded-full bg-rose-500/70">
              <span className="absolute inset-y-0 left-0 bg-emerald-400" style={{ width: `${win.share}%` }} />
              <span className="absolute inset-y-0 left-1/2 w-px bg-neutral-950" />
            </span>
            <span className="font-semibold text-bear">{(100 - win.share).toFixed(1)}% Sellers</span>
          </span>
        )}
        {bar && (
          <span className="text-neutral-400" title={`This candle: ${fmtAmount(sides(bar, mode)[0])} ${unit} by buyers, ${fmtAmount(sides(bar, mode)[1])} by sellers${mode === "ticks" ? `, ${bar[3]} unchanged` : ""}`}>
            candle <span className="text-bull">▲{fmtAmount(sides(bar, mode)[0])}</span>{" "}
            <span className="text-bear">▼{fmtAmount(sides(bar, mode)[1])}</span>
          </span>
        )}
        {testView && (
          <span className={`pointer-events-auto cursor-help rounded px-1 ${testView.cls}`} title={testView.tip}>
            next {AHEAD}: ▲{testView.up}% ▼{testView.dn}% · {testView.tone}
          </span>
        )}
        {meta?.truncated && <span className="text-neutral-500" title="The tick history read for this pane reaches back one day">· last 24 h</span>}
        <span className="pointer-events-auto cursor-help text-neutral-500" title={METHOD}>ⓘ estimate</span>
      </div>
      {err && !meta && (
        <div className="absolute inset-0 z-10 flex items-center justify-center px-4 text-center text-xs text-amber-300/80">
          Order flow unavailable: {err}
        </div>
      )}
      <div ref={boxRef} className="h-full w-full" />
    </div>
  );
}
