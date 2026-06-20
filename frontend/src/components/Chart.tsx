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
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";
import { api } from "../api/client";
import { fmtPrice } from "../format";
import type { AssetClass, Candle, PositionView, TradeProposal } from "../types";
import type { LiveQuote } from "../hooks/useQuoteSocket";

export interface ArmedLevel {
  symbol: string;
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
}

const EMA_CONFIG = [
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

export function Chart({ symbol, assetClass, timeframe, proposal, liveQuote, positions, armed }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rsiContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const emaRefs = useRef<Record<number, ISeriesApi<"Line">>>({});
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

  const [legend, setLegend] = useState<Legend | null>(null);
  const [showEma, setShowEma] = useState<Record<number, boolean>>({ 50: true, 100: false, 200: true });
  const [showRsi, setShowRsi] = useState(true);
  const [showMacd, setShowMacd] = useState(true);

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
        volumeRef.current.setData([]);
        for (const { period } of EMA_CONFIG) emaRefs.current[period]?.setData([]);
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
    posLinesRef.current.forEach((l) => series.removePriceLine(l));
    posLinesRef.current = [];
    const mine = (positions ?? []).filter(
      (p) => p.symbol.toUpperCase() === symbol.toUpperCase(),
    );
    const add = (price: number | null | undefined, color: string, title: string) => {
      if (price == null) return;
      posLinesRef.current.push(
        series.createPriceLine({ price, color, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title }),
      );
    };
    for (const p of mine) {
      const arrow = p.direction === "long" ? "▲" : "▼";
      // When the stop has been moved to breakeven (SL ≈ entry) the two labels would stack on
      // the same axis pixel — merge them into one instead of overlapping.
      const be =
        p.stop_loss != null && Math.abs(p.stop_loss - p.entry_price) <= Math.abs(p.entry_price) * 1e-4;
      // Show the $ you'd lose at SL / gain at TP right on the line label (like MT5's on-chart box).
      const slUsd = usdAtLevel(p, p.stop_loss);
      const tpUsd = usdAtLevel(p, p.take_profit);
      add(p.entry_price, "#3b82f6", be ? `${arrow} entry·SL ${slUsd}`.trim() : `${arrow} entry`);
      if (!be) add(p.stop_loss, "#ef5350", `SL ${slUsd}`.trim());
      add(p.take_profit, "#26a69a", `TP ${tpUsd}`.trim());
    }
  }, [positions, symbol]);

  // Armed conditional ('wait for the break') overlay — large-dashed amber lines for THIS symbol's
  // armed setups, so the pending trigger/SL/TP are visible on the chart distinct from proposal
  // (dashed) and open-position (solid) lines.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    armedLinesRef.current.forEach((l) => series.removePriceLine(l));
    armedLinesRef.current = [];
    const mine = (armed ?? []).filter((a) => a.symbol.toUpperCase() === symbol.toUpperCase());
    const add = (price: number | null, color: string, title: string) => {
      if (price == null) return;
      armedLinesRef.current.push(
        series.createPriceLine({ price, color, lineWidth: 1, lineStyle: LineStyle.LargeDashed,
                                 axisLabelVisible: true, title }),
      );
    };
    for (const a of mine) {
      add(a.trigger_price, "#f59e0b", "⚡ Arm");   // amber trigger
      add(a.stop_loss, "#ef5350", "⚡ SL");
      add(a.take_profit, "#26a69a", "⚡ TP");
    }
  }, [armed, symbol]);

  const change = legend ? legend.close - legend.open : 0;
  const changePct = legend && legend.open ? (change / legend.open) * 100 : 0;

  const myPos = (positions ?? []).find((p) => p.symbol.toUpperCase() === symbol.toUpperCase());

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
