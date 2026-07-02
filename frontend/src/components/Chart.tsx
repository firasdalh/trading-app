import { useEffect, useRef, useState } from "react";
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
  LogicalRange,
  SeriesMarker,
  Time,
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";
import { api } from "../api/client";
import { fmtPrice } from "../format";
import type { AssetClass, Candle, PositionView, TradeProposal } from "../types";
import type { LiveQuote } from "../hooks/useQuoteSocket";

export interface ArmedLevel {
  id: number;
  symbol: string;
  direction: string;
  order_type: string;
  trigger_price: number;
  stop_loss: number | null;
  take_profit: number | null;
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
}

const EMA_CONFIG = [
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

export function Chart({ symbol, assetClass, timeframe, proposal, liveQuote, positions, armed, onSetSlTp, onSetArmedLevels }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rsiContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const emaRefs = useRef<Record<number, ISeriesApi<"Line">>>({});
  const stRef = useRef<ISeriesApi<"Line"> | null>(null);  // SuperTrend (ONE line, per-point colour)
  const emaHiRef = useRef<ISeriesApi<"Line"> | null>(null);  // EMA20 of highs (upper band)
  const emaLoRef = useRef<ISeriesApi<"Line"> | null>(null);  // EMA20 of lows (lower band)
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

  const [legend, setLegend] = useState<Legend | null>(null);
  const [showEma, setShowEma] = useState<Record<number, boolean>>({ 20: true, 50: true, 100: false, 200: true });
  const [showRsi, setShowRsi] = useState(true);
  const [showMacd, setShowMacd] = useState(true);
  const [showSt, setShowSt] = useState(true);
  const [showPb, setShowPb] = useState(true);
  const [showBand, setShowBand] = useState(true);

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
        const candleData: CandlestickData[] = series.candles.map((c) => ({
          time: toTime(c), open: c.open, high: c.high, low: c.low, close: c.close,
        }));
        seriesRef.current.setData(candleData);
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
        applyPullback(series.candles);
        applyBand(series.candles);
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

  useEffect(() => {
    applyPullback(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showPb]);

  useEffect(() => {
    applyBand(candlesRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showBand]);

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

  function applyPullback(candles: Candle[]) {
    const series = seriesRef.current;
    if (!series) return;
    if (!showPb || !candles.length) {
      series.setMarkers([]);
      return;
    }
    const markers: SeriesMarker<Time>[] = pullbackSignals(candles).map((s) => ({
      time: toTime(candles[s.i]),
      position: s.bullish ? "belowBar" : "aboveBar",
      color: s.bullish ? "#26a69a" : "#ef5350",
      shape: s.bullish ? "arrowUp" : "arrowDown",
      text: `PB ${s.score}`,
    }));
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
      const trigLine = add(a.trigger_price, "#f59e0b", "⚡ Arm · drag");   // amber trigger
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
        container.style.cursor = handleAt(e) ? "ns-resize" : "";
        return;
      }
      const price = yToPrice(e);
      if (price == null) return;
      dragPriceRef.current = price;
      drag.line.applyOptions({ price });
      setDragHint({ label: drag.label, price, pos: drag.refPos });
    };

    const onDown = (e: MouseEvent) => {
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
    <div className="relative">
      <div className="mb-2 flex items-center gap-2">
        {EMA_CONFIG.map(({ period, color }) => (
          <button
            key={period}
            onClick={() => setShowEma((s) => ({ ...s, [period]: !s[period] }))}
            className={`rounded px-2 py-0.5 text-xs ${showEma[period] ? "bg-neutral-700 text-white" : "bg-neutral-900 text-neutral-500"}`}
            style={showEma[period] ? { color } : undefined}
          >
            EMA {period}
          </button>
        ))}
        <button
          onClick={() => setShowRsi((v) => !v)}
          className={`rounded px-2 py-0.5 text-xs ${showRsi ? "bg-neutral-700 text-purple-300" : "bg-neutral-900 text-neutral-500"}`}
        >
          RSI
        </button>
        <button
          onClick={() => setShowMacd((v) => !v)}
          className={`rounded px-2 py-0.5 text-xs ${showMacd ? "bg-neutral-700 text-blue-300" : "bg-neutral-900 text-neutral-500"}`}
        >
          MACD
        </button>
        <button
          onClick={() => setShowSt((v) => !v)}
          className={`rounded px-2 py-0.5 text-xs ${showSt ? "bg-neutral-700 text-emerald-300" : "bg-neutral-900 text-neutral-500"}`}
          title="SuperTrend (ATR 10 ×2.7) — green band = uptrend (support), red band = downtrend (resistance)"
        >
          SuperTrend
        </button>
        <button
          onClick={() => setShowPb((v) => !v)}
          className={`rounded px-2 py-0.5 text-xs ${showPb ? "bg-neutral-700 text-amber-300" : "bg-neutral-900 text-neutral-500"}`}
          title="Pullback score (0-100) on top of SuperTrend: ▲ buy-the-dip in an uptrend / ▼ sell-the-rally in a downtrend. Marks the bar when confidence >= 70."
        >
          Pullback
        </button>
        <button
          onClick={() => setShowBand((v) => !v)}
          className={`rounded px-2 py-0.5 text-xs ${showBand ? "bg-neutral-700 text-cyan-300" : "bg-neutral-900 text-neutral-500"}`}
          title="EMA20 of highs / lows = a band around price. The SuperTrend Strategy enters on a candle close BEYOND this band in the SuperTrend direction."
        >
          EMA20 H/L
        </button>
        <button
          onClick={recenter}
          className="ml-auto rounded bg-neutral-900 px-2 py-0.5 text-xs text-neutral-300 hover:bg-neutral-700 hover:text-white"
          title="Recenter — reset to the recent bars with the price axis auto-fitted"
        >
          ⊹ Recenter
        </button>
      </div>

      {legend && (
        <div className="pointer-events-none absolute left-2 top-9 z-10 flex gap-2 text-xs tabular-nums">
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
          className={`pointer-events-none absolute left-1/2 top-9 z-20 -translate-x-1/2 rounded px-2 py-1 text-xs font-semibold text-white shadow ${
            dragHint.label === "SL" ? "bg-bear/90" : dragHint.label === "TP" ? "bg-bull/90" : "bg-amber-500/90"
          }`}
        >
          {dragHint.label} → {fmtPrice(dragHint.price)}{" "}
          {dragHint.pos && usdAtLevel(dragHint.pos, dragHint.price) && (
            <span>({usdAtLevel(dragHint.pos, dragHint.price)})</span>
          )}
        </div>
      )}
      <div ref={containerRef} className="h-[600px] w-full" />
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
            {hasArmed && <span className="text-amber-400">— Arm trigger</span>}
            <span className="text-bear">— Stop</span>
            <span className="text-bull">— Target</span>
          </div>
        );
      })()}
      {showRsi && (
        <div className="relative mt-1">
          <span className="pointer-events-none absolute left-2 top-1 z-10 text-xs text-purple-300">RSI 14</span>
          <div ref={rsiContainerRef} className="h-[120px] w-full" />
        </div>
      )}
      {showMacd && (
        <div className="relative mt-1">
          <span className="pointer-events-none absolute left-2 top-1 z-10 flex gap-2 text-xs">
            <span className="text-neutral-400">MACD 12 26 9</span>
            <span className="text-blue-300">MACD</span>
            <span className="text-amber-400">signal</span>
          </span>
          <div ref={macdContainerRef} className="h-[120px] w-full" />
        </div>
      )}
    </div>
  );
}
