import { useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import {
  ColorType,
  createChart,
  LineStyle,
  LineType,
  type AutoscaleInfo,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type LogicalRange,
  type MouseEventParams,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import {
  crowdRead, FLOW_CUT, fmtCoins, fmtIn, fmtUsdShort, funding as readFunding, fundingRead, liqByBar, liqKey, liqSum, oiAt,
  oiHistory, oiNow, oiPeriod, okxHistory, okxLatest, perpOf, takerFlow, type Flow, type Funding, type Liq, type OiPoint,
  type Side,
} from "../cryptoFeeds";
import { TF_SECONDS } from "../timeframes";
import type { Candle } from "../types";

/**
 * THE CRYPTO PULSE PANE (2026-11-21, asked for manual BTC trading): what the futures crowd is doing, under a
 * crypto chart. Exness's BTC follows the big perpetual-futures markets, and there the forced closes and the
 * open interest show who is trapped and who is piling in - nothing in our own quotes says that.
 *
 *   ▮ green up   - SHORTS liquidated on that candle (forced buying), in dollars
 *   ▮ red down   - LONGS liquidated (forced selling); solid = one of the biggest on screen. Heights are the
 *                  square root of the dollars, so small candles still show; the legend has the real amounts
 *   ━ amber line - open interest in coins (positions open on Binance's perpetual)
 *
 * The legend reads the last 15 min or 1 h: open interest against the price (who is coming in or getting out),
 * the liquidations, funding (who is paying to stay in), and the aggressive flow of the last 5 minutes (who is
 * pressing the market buttons; the learner watches it on the strategy's crypto trades). Free public feeds read by the browser
 * (cryptoFeeds.ts); display only, untested on our trades.
 */

const WINDOWS = [15, 60] as const;
const HIST_MS = 6 * 3600_000;    // liquidations loaded back this far (OKX keeps more; the 1m chart needs about this)
const HIST_PAGES = 60;           // ...or this many pages of 100, whichever comes first
const KEEP_MS = 12 * 3600_000;   // older ones are dropped while the pane stays open
const LIQ_POLL_MS = 3000;
const OI_POLL_MS = 15_000;
const FUND_POLL_MS = 30_000;
const FLOW_POLL_MS = 15_000;
const OI_MARGINS = { top: 0.22, bottom: 0.5 };
const BAR_MARGINS = { top: 0.56, bottom: 0.04 };
const SHORT_SOFT = "rgba(38,166,154,0.45)";
const LONG_SOFT = "rgba(239,83,80,0.45)";
const SHORT_BIG = "rgba(38,166,154,1)";
const LONG_BIG = "rgba(239,83,80,1)";

const METHOD =
  "Free public feeds, read by this browser (no key, nothing about the account is sent). Liquidations: OKX's " +
  "USDT perpetual - one of the biggest exchanges, so the whole market's total is several times this, but the " +
  "shape is the same. Open interest and funding: Binance's USDT perpetual. Their prices sit a few dollars off " +
  "Exness's. Display only: none of this is tested on our trades and nothing here feeds the strategy.";

type Point = { time: UTCTimestamp; side: Side | null; oi: number | null };

interface Props {
  symbol: string;
  timeframe: string;
  mainChart: MutableRefObject<IChartApi | null>;
  candles: MutableRefObject<Candle[]>;
  lastBarTime: () => number | null;
  dataV: number;
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

// One point per candle of the price chart, so both time axes hold the same bars (the panes scroll together by
// bar index); a newer bar the live feed has already drawn up there is added at the end.
function alignPoints(candles: Candle[], rows: Map<number, Side>, oi: OiPoint[], tfSec: number,
                     toTime: Props["toTime"], toDisp: Props["toDisp"], lastBar: number | null): Point[] {
  const oiOf = (u: number) => {
    if (!oi.length || (u + tfSec) * 1000 < oi[0].ts) return null;
    return oiAt(oi, Math.min(Date.now(), (u + tfSec) * 1000))?.coins ?? null;
  };
  const pts: Point[] = candles.map((c) => {
    const u = utcOf(c);
    return { time: toTime(c), side: rows.get(u) ?? null, oi: oiOf(u) };
  });
  const lastUtc = candles.length ? utcOf(candles[candles.length - 1]) : -Infinity;
  if (lastBar != null) {
    const nowBar = Math.floor(Date.now() / 1000 / tfSec) * tfSec;
    for (let u = lastUtc + tfSec; u <= nowBar; u += tfSec) {
      const time = toDisp(u) as UTCTimestamp;
      if (time > lastBar) break;
      pts.push({ time, side: rows.get(u) ?? null, oi: oiOf(u) });
    }
  }
  return pts;
}

// The close at or before ``ms`` on the price chart, for the price side of the window's read.
function closeAt(candles: Candle[], ms: number): number | null {
  for (let i = candles.length - 1; i >= 0; i--) if (utcOf(candles[i]) * 1000 <= ms) return candles[i].close;
  return null;
}

const pctChange = (a: number | null | undefined, b: number | null | undefined) =>
  a != null && b != null && a > 0 ? ((b - a) / a) * 100 : null;
const signed = (v: number, d = 2) => `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`;

export function CryptoPulsePane({ symbol, timeframe, mainChart, candles, lastBarTime, dataV, toTime, toDisp,
                                  timeAxis, secondsVisible, storeKey }: Props) {
  const perp = useMemo(() => perpOf(symbol), [symbol]);
  const tfSec = TF_SECONDS[timeframe] ?? 60;
  const period = oiPeriod(tfSec);
  const [win, setWin] = useStored<number>(storeKey("chart.pulseWin"), 15);
  const [v, setV] = useState(0);                       // bumps when a feed brings something new
  const [fund, setFund] = useState<Funding | null>(null);
  const [flow, setFlow] = useState<Flow | null>(null);
  const [liqErr, setLiqErr] = useState<string | null>(null);
  const [oiErr, setOiErr] = useState<string | null>(null);
  const [liqReady, setLiqReady] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const [main, setMain] = useState<IChartApi | null>(null);

  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const shortRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const longRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const oiRef = useRef<ISeriesApi<"Line"> | null>(null);
  const liqsRef = useRef(new Map<string, Liq>());
  const oiPtsRef = useRef<OiPoint[]>([]);
  const oiRangeRef = useRef({ lo: -Infinity, hi: Infinity });   // the line's visible range, for its axis labels
  const overPane = useRef(false);

  // The pane's own chart, made once.
  useEffect(() => {
    if (!boxRef.current) return;
    const chart = createChart(boxRef.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a3a3a3", attributionLogo: false },
      grid: { vertLines: { color: "#1f1f1f" }, horzLines: { visible: false } },
      timeScale: { timeVisible: true, secondsVisible, borderColor: "#404040", visible: timeAxis },
      // The axis is the open interest's (in coins). The bars have no labels: their dollars are in the legend.
      rightPriceScale: { borderColor: "#404040", minimumWidth: 72, scaleMargins: OI_MARGINS },
      crosshair: { mode: 1 },
    });
    const centred = (orig: () => AutoscaleInfo | null): AutoscaleInfo | null => {
      const r = orig();
      if (!r) return r;
      const m = Math.max(Math.abs(r.priceRange.minValue), Math.abs(r.priceRange.maxValue)) || 1;
      return { ...r, priceRange: { minValue: -m, maxValue: m } };
    };
    // Bar heights are the square root of the dollars (see barH), so the label squares them back.
    const hist = {
      priceScaleId: "bars", priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: centred,
      priceFormat: { type: "custom" as const, minMove: 0.01, formatter: (p: number) => fmtUsdShort(p * p) },
    };
    shortRef.current = chart.addHistogramSeries({ ...hist, color: SHORT_SOFT });
    longRef.current = chart.addHistogramSeries({ ...hist, color: LONG_SOFT });
    longRef.current.createPriceLine({ price: 0, color: "#525252", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
    chart.priceScale("bars").applyOptions({ scaleMargins: BAR_MARGINS });
    // The axis spans the whole pane, so its labels beside the bars band (below the line's range) are left blank;
    // a narrow range gets one more digit, or every label would read the same.
    oiRef.current = chart.addLineSeries({
      color: "#fbbf24", lineWidth: 2, lineType: LineType.WithSteps, priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: {
        type: "custom", minMove: 1,
        formatter: (p: number) => {
          const { lo, hi } = oiRangeRef.current;
          if (p < lo - (hi - lo) * 0.05 || p > hi + (hi - lo) * 0.05) return "";
          return hi - lo < 2000 && p >= 1000 && p < 1e6 ? `${(p / 1000).toFixed(2)}k` : fmtCoins(p);
        },
      },
      autoscaleInfoProvider: (orig: () => AutoscaleInfo | null): AutoscaleInfo | null => {
        const r = orig();
        if (r) oiRangeRef.current = { lo: r.priceRange.minValue, hi: r.priceRange.maxValue };
        return r;
      },
    });
    chart.subscribeCrosshairMove((p: MouseEventParams) => setHover(p.time != null ? (p.time as number) : null));
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      shortRef.current = longRef.current = oiRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.applyOptions({ timeScale: { visible: timeAxis, secondsVisible } });
  }, [timeAxis, secondsVisible]);

  // The price chart is made in the parent's effect, which runs after this one on the first mount.
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

  // Liquidations: the last hours once, then the newest every few seconds (not while the tab is hidden).
  useEffect(() => {
    liqsRef.current = new Map();
    setLiqReady(false);
    setLiqErr(null);
    setV((x) => x + 1);
    if (!perp) return;
    const ctl = new AbortController();
    let busy = true;
    let seen = 0;
    const add = (ls: Liq[]) => {
      const m = liqsRef.current;
      let added = 0;
      for (const l of ls) {
        const k = liqKey(l);
        if (!m.has(k)) {
          m.set(k, l);
          added++;
        }
        if (l.ts > seen) seen = l.ts;
      }
      const cut = Date.now() - KEEP_MS;
      for (const [k, l] of m) if (l.ts < cut) m.delete(k);
      if (added) setV((x) => x + 1);
    };
    okxHistory(perp, Date.now() - HIST_MS, HIST_PAGES, ctl.signal)
      .then((ls) => {
        add(ls);
        setLiqReady(true);
      })
      .catch((e: unknown) => { if (!ctl.signal.aborted) setLiqErr(e instanceof Error ? e.message : String(e)); })
      .finally(() => { busy = false; });
    const id = window.setInterval(() => {
      if (busy || document.visibilityState === "hidden") return;
      busy = true;
      okxLatest(perp, seen, ctl.signal)
        .then((ls) => {
          add(ls);
          setLiqErr(null);
          setLiqReady(true);
        })
        .catch(() => { /* one missed read; the next catches up */ })
        .finally(() => { busy = false; });
    }, LIQ_POLL_MS);
    return () => { ctl.abort(); window.clearInterval(id); };
  }, [perp]);

  // Open interest: the history at the chart's step (5m at the least), then a live reading every 15 s.
  useEffect(() => {
    oiPtsRef.current = [];
    setOiErr(null);
    setV((x) => x + 1);
    if (!perp) return;
    const ctl = new AbortController();
    let busy = true;
    oiHistory(perp, period, ctl.signal)
      .then((pts) => {
        const lastTs = pts.length ? pts[pts.length - 1].ts : 0;
        const live = oiPtsRef.current.filter((p) => p.ts > lastTs);
        oiPtsRef.current = [...pts, ...live];
        setV((x) => x + 1);
      })
      .catch((e: unknown) => { if (!ctl.signal.aborted) setOiErr(e instanceof Error ? e.message : String(e)); })
      .finally(() => { busy = false; });
    const tick = () => {
      if (busy || document.visibilityState === "hidden") return;
      busy = true;
      oiNow(perp, ctl.signal)
        .then((p) => {
          const pts = oiPtsRef.current;
          if (p.coins > 0 && (!pts.length || p.ts > pts[pts.length - 1].ts)) {
            pts.push(p);
            setV((x) => x + 1);
          }
        })
        .catch(() => { /* next time */ })
        .finally(() => { busy = false; });
    };
    const id = window.setInterval(tick, OI_POLL_MS);
    return () => { ctl.abort(); window.clearInterval(id); };
  }, [perp, period]);

  // Funding: who pays whom at the next settlement, every 30 s.
  useEffect(() => {
    setFund(null);
    if (!perp) return;
    const ctl = new AbortController();
    const read = () => {
      if (document.visibilityState === "hidden") return;
      readFunding(perp, ctl.signal).then(setFund).catch(() => { /* next time */ });
    };
    read();
    const id = window.setInterval(read, FUND_POLL_MS);
    return () => { ctl.abort(); window.clearInterval(id); };
  }, [perp]);

  // Aggressive flow: the taker-buy share of the last 5 finished minutes, every 15 s.
  useEffect(() => {
    setFlow(null);
    if (!perp) return;
    const ctl = new AbortController();
    const read = () => {
      if (document.visibilityState === "hidden") return;
      takerFlow(perp, undefined, ctl.signal).then(setFlow).catch(() => { /* next time */ });
    };
    read();
    const id = window.setInterval(read, FLOW_POLL_MS);
    return () => { ctl.abort(); window.clearInterval(id); };
  }, [perp]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const liqs = useMemo(() => [...liqsRef.current.values()].sort((a, b) => a.ts - b.ts), [v]);
  const rows = useMemo(() => liqByBar(liqs, tfSec), [liqs, tfSec]);
  const pts = useMemo(
    () => alignPoints(candles.current, rows, oiPtsRef.current, tfSec, toTime, toDisp, lastBarTime()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, v, dataV, tfSec],
  );
  // "Big" = the top tenth of the candles that had any, on this chart.
  const bigCut = useMemo(() => {
    const xs = pts.flatMap((p) => (p.side ? [p.side.long, p.side.short] : [])).filter((x) => x > 0).sort((a, b) => a - b);
    return xs.length >= 10 ? xs[Math.floor(0.9 * (xs.length - 1))] : Infinity;
  }, [pts]);

  useEffect(() => {
    const sh = shortRef.current, lo = longRef.current, line = oiRef.current;
    if (!sh || !lo || !line) return;
    const s: (HistogramData | WhitespaceData)[] = [];
    const l: (HistogramData | WhitespaceData)[] = [];
    const o: (LineData | WhitespaceData)[] = [];
    // Heights are the square root of the dollars: one $2M wave would otherwise flatten every $20k candle to nothing.
    const barH = (usd: number) => Math.sqrt(usd);
    for (const p of pts) {
      if (p.side && p.side.short > 0) s.push({ time: p.time, value: barH(p.side.short), color: p.side.short >= bigCut ? SHORT_BIG : SHORT_SOFT });
      else s.push({ time: p.time });
      if (p.side && p.side.long > 0) l.push({ time: p.time, value: -barH(p.side.long), color: p.side.long >= bigCut ? LONG_BIG : LONG_SOFT });
      else l.push({ time: p.time });
      o.push(p.oi != null ? { time: p.time, value: p.oi } : { time: p.time });
    }
    sh.setData(s);
    lo.setData(l);
    line.setData(o);
    const r = main?.timeScale().getVisibleLogicalRange();
    if (r) chartRef.current?.timeScale().setVisibleLogicalRange(r);
  }, [pts, bigCut, main]);

  // The window's read, at the newest moment.
  const now = Date.now();
  const winMs = win * 60_000;
  const oiPts = oiPtsRef.current;
  const oiLast = oiPts.length ? oiPts[oiPts.length - 1] : null;
  const oiPct = pctChange(oiAt(oiPts, now - winMs)?.coins, oiLast?.coins);
  const pxPct = pctChange(closeAt(candles.current, now - winMs), candles.current.length ? candles.current[candles.current.length - 1].close : null);
  const crowd = crowdRead(oiPct, pxPct, win);
  const winLiq = liqSum(liqs, now - winMs, now);
  const fr = fund && Number.isFinite(fund.rate) ? fundingRead(fund.rate) : null;

  // The candle under the cursor (or none).
  const hovered = hover != null ? pts.find((p) => (p.time as number) === hover) ?? null : null;

  const toneCls = (t: "bull" | "bear" | "flat") =>
    t === "bull" ? "bg-emerald-500/15 text-emerald-300" : t === "bear" ? "bg-rose-500/15 text-rose-300" : "bg-neutral-800 text-neutral-300";

  if (!perp) return null;
  return (
    <div
      className="relative h-full w-full"
      onPointerEnter={() => { overPane.current = true; }}
      onPointerLeave={() => { overPane.current = false; setHover(null); }}
    >
      <div className="pointer-events-none absolute inset-x-2 top-1 z-10 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
        <span className="font-semibold text-amber-300">Crypto pulse · {perp.base}</span>
        <span className="pointer-events-auto inline-flex overflow-hidden rounded border border-neutral-700" role="group" aria-label="Window">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setWin(w)}
              aria-pressed={win === w}
              title={`Read the last ${w === 60 ? "hour" : `${w} minutes`}`}
              className={`px-1.5 leading-4 ${win === w ? "bg-amber-400/90 font-semibold text-neutral-950" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
            >
              {w === 60 ? "1h" : `${w}m`}
            </button>
          ))}
        </span>
        {oiLast && (
          <span
            className="pointer-events-auto cursor-help text-neutral-300"
            title={`Open interest: ${Math.round(oiLast.coins).toLocaleString()} ${perp.base} in open positions on Binance's ${perp.binance} perpetual${fund?.mark ? ` (about ${fmtUsdShort(oiLast.coins * fund.mark)})` : ""}. Counted in coins, so a price move alone does not move it.`}
          >
            <span className="text-amber-300">OI</span> {fmtCoins(oiLast.coins)}
            {oiPct != null && <span className={oiPct >= 0 ? "text-bull" : "text-bear"}> {signed(oiPct)}</span>}
          </span>
        )}
        {crowd && (
          <span className={`pointer-events-auto cursor-help rounded px-1 ${toneCls(crowd.tone)}`}
                title={`${crowd.tip} Last ${win === 60 ? "hour" : `${win} min`}: open interest ${oiPct != null ? signed(oiPct) : "?"}, price ${pxPct != null ? signed(pxPct) : "?"}.`}>
            {crowd.text}
          </span>
        )}
        {liqReady && (
          <span className="pointer-events-auto cursor-help"
                title={`Liquidated on OKX in the last ${win === 60 ? "hour" : `${win} min`}: ${winLiq.n} forced closes. Longs liquidated = forced selling (pushes the price down); shorts liquidated = forced buying (pushes it up). A big wave often marks the end of a move rather than the start.`}>
            <span className="text-neutral-400">liq.</span> <span className="text-bear">longs {fmtUsdShort(winLiq.long)}</span>{" "}
            <span className="text-bull">shorts {fmtUsdShort(winLiq.short)}</span>
          </span>
        )}
        {flow && (() => {
          const b = Math.round(flow.buy * 1000) / 10;
          const tone = flow.buy - 0.5 > FLOW_CUT ? "text-bull" : flow.buy - 0.5 < -FLOW_CUT ? "text-bear" : "text-neutral-300";
          const who = flow.buy - 0.5 > FLOW_CUT ? "buyers pressing" : flow.buy - 0.5 < -FLOW_CUT ? "sellers pressing" : "balanced";
          return (
            <span className="pointer-events-auto cursor-help"
                  title={`Aggressive flow: of the volume traded in the last ${flow.bars} finished minutes on Binance's ${perp.binance} perpetual, ${b.toFixed(1)}% was market BUYING (people pressing buy at the market) and ${(100 - b).toFixed(1)}% market selling. Over ${(50 + FLOW_CUT * 100).toFixed(2)}% = buyers pressing (with a buy, against a sell); under ${(50 - FLOW_CUT * 100).toFixed(2)}% = sellers pressing. In the 2026-11-21 study (6 months of entries like ours), entries with the 5-minute flow against them did a little worse - a hint, not proven, so the learner watches it on the strategy's crypto trades.`}>
              <span className="text-neutral-400">flow</span>{" "}
              <span className={tone}>buyers {b.toFixed(1)}%</span>
              <span className="text-neutral-500"> · {who}</span>
            </span>
          );
        })()}
        {fund && fr && (
          <span className="pointer-events-auto cursor-help"
                title={`Funding on Binance's ${perp.binance}: ${(fund.rate * 100).toFixed(4)}% every ${fund.hours} h, next at ${new Date(fund.next).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })} on your clock. Positive = longs pay shorts; about +0.01% is the usual rate. Well above it, the longs are crowded; below zero, the shorts are.`}>
            <span className="text-neutral-400">funding</span>{" "}
            <span className={fr.tone === "warn" ? "text-amber-300" : "text-neutral-300"}>{(fund.rate * 100).toFixed(4)}%</span>
            {fr.tone === "warn" && <span className="text-amber-300"> · {fr.text}</span>}
            <span className="text-neutral-500"> · in {fmtIn(fund.next - now)}</span>
          </span>
        )}
        {hovered && (
          <span className="text-neutral-400">
            candle{" "}
            {hovered.side ? (
              <>
                {hovered.side.long > 0 && <span className="text-bear">longs {fmtUsdShort(hovered.side.long)} </span>}
                {hovered.side.short > 0 && <span className="text-bull">shorts {fmtUsdShort(hovered.side.short)}</span>}
              </>
            ) : (
              "no liquidations"
            )}
            {hovered.oi != null && <> · OI {fmtCoins(hovered.oi)}</>}
          </span>
        )}
        <span className="pointer-events-auto cursor-help text-neutral-500" title={METHOD}>ⓘ OKX · Binance</span>
      </div>
      {(liqErr || oiErr) && !liqReady && !oiLast && (
        <div className="absolute inset-0 z-10 flex items-center justify-center px-4 text-center text-xs text-amber-300/80">
          Crypto feeds unavailable: {liqErr ?? oiErr}
        </div>
      )}
      <div ref={boxRef} className="h-full w-full" />
    </div>
  );
}
