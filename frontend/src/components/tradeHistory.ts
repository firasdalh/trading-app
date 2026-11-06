// MetaTrader's "show trade history" on the main chart: where each past trade on the pair was opened and
// where it was closed (asked for 2026-11-03, "like the chart of the MT5"). As MT5 draws it, every DEAL is
// an arrow with its tip on the deal's price - a buy deal blue and pointing up, a sell deal red and
// pointing down - so a long opens on a blue arrow and closes on a red one, and a short the other way
// round. A dotted line joins a trade's entry to each of its exits (a partial close is an exit of its
// own), blue for a buy and red for a sell. One thing MT5 keeps for its tooltip is printed at the exit
// here: the result, green or red - on a scalping chart, which trades worked is the first thing you look
// for. Hovering an arrow (Chart.tsx) gives the rest.
//
// A series primitive, like the SL/TP boxes (chartZones.ts): painted by the chart in the same frame as
// the candles, so nothing trails behind a pan, with the geometry worked out at draw time.
import type {
  IChartApiBase,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitivePaneRenderer,
  ISeriesPrimitivePaneView,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import { fmtUsd } from "../format";
import { roundRect } from "./chartZones";

type Target = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type Scope = Parameters<Parameters<Target["useBitmapCoordinateSpace"]>[0]>[0];

export interface HistPoint {
  index: number;      // logical bar index; one before the loaded history is simply off the left edge
  price: number;
}

export interface HistTrade {
  key: string;
  direction: "long" | "short";
  entry: HistPoint;
  exits: (HistPoint & { pnl: number | null })[];
  pnl: number | null;   // the whole trade's (every exit + costs): the tag at its last exit once it is closed
  open: boolean;        // part of it is still open: its last exit shows only what that exit booked
}

export interface HistHit {
  key: string;
  x: number;            // pane pixels, where the arrow is drawn
  y: number;
}

const BUY = "30,144,255";       // MT5's buy colour (dodger blue)
const SELL = "255,59,59";       // and its sell red
const WIN = "38,166,154";       // the chart's own green / red (#26a69a / #ef5350)
const LOSS = "239,83,80";
const ARROW_H = 11;             // tip to base, pane pixels
const ARROW_W = 6;              // half the base
const HIT_R = 10;               // how near an arrow the pointer must be to hover it
const TAG_H = 14;
const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif";

const usd = (v: number) => fmtUsd(v, { sign: true }).replace("-", "−");

export class TradeHistoryPrimitive implements ISeriesPrimitive<Time> {
  private trades: HistTrade[] = [];
  private hover: string | null = null;
  private hits: HistHit[] = [];           // every arrow as last drawn, for hit()
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private request: (() => void) | null = null;
  // Same array every call: the library caches pane views by reference.
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor() {
    // On top of the candles, as MT5 draws its deals: an arrow under a candle body would be lost.
    const renderer: ISeriesPrimitivePaneRenderer = { draw: (t) => this.draw(t) };
    this.views = [{ zOrder: () => "top", renderer: () => (this.trades.length ? renderer : null) }];
  }

  attached(p: SeriesAttachedParameter<Time, SeriesType>) {
    this.chart = p.chart;
    this.series = p.series;
    this.request = p.requestUpdate;
  }

  detached() {
    this.chart = null;
    this.series = null;
    this.request = null;
  }

  setTrades(trades: HistTrade[]) {
    this.trades = trades;
    if (!trades.length) this.hits = [];
    this.request?.();
  }

  /** The trade under the pointer is drawn stronger (its line solid, its arrows bigger). */
  setHover(key: string | null) {
    if (key === this.hover) return;
    this.hover = key;
    this.request?.();
  }

  /** The trade whose arrow is nearest (x, y), in pane pixels, as last drawn - within HIT_R. */
  hit(x: number, y: number): HistHit | null {
    let best: HistHit | null = null;
    let bestD = HIT_R * HIT_R;
    for (const h of this.hits) {
      const d = (h.x - x) ** 2 + (h.y - y) ** 2;
      if (d <= bestD) {
        bestD = d;
        best = h;
      }
    }
    return best;
  }

  updateAllViews() {
    /* geometry is computed at draw time - see the header comment */
  }

  paneViews() {
    return this.views;
  }

  private draw(target: Target) {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series) return;
    const ts = chart.timeScale();
    const tagsOn = ts.options().barSpacing >= 3;     // zoomed far out the tags would only be a smear
    target.useBitmapCoordinateSpace((s: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = s;
      const W = s.mediaSize.width;
      const H = s.mediaSize.height;
      const X = (i: number): number | null => {
        const c = ts.logicalToCoordinate(i as Logical);
        return c == null ? null : Number(c);
      };
      const Y = (p: number): number | null => {
        const c = series.priceToCoordinate(p);
        return c == null ? null : Number(c);
      };

      type Pt = { x: number; y: number; up: boolean; rgb: string };
      type Drawn = { t: HistTrade; entry: Pt; exits: (Pt & { pnl: number | null; last: boolean })[] };
      const drawn: Drawn[] = [];
      for (const t of this.trades) {
        const ex = X(t.entry.index);
        const ey = Y(t.entry.price);
        if (ex == null || ey == null) continue;
        const long = t.direction === "long";
        const exits: Drawn["exits"] = [];
        t.exits.forEach((e, i) => {
          const x = X(e.index);
          const y = Y(e.price);
          if (x == null || y == null) return;
          // The closing deal is the other side's: a long closes on a SELL deal, a short on a BUY one.
          exits.push({ x, y, up: !long, rgb: long ? SELL : BUY, pnl: e.pnl, last: i === t.exits.length - 1 });
        });
        if (!exits.length) continue;
        const xs = [ex, ...exits.map((e) => e.x)];
        if (Math.max(...xs) < -20 || Math.min(...xs) > W + 20) continue;   // nowhere near the view
        drawn.push({ t, entry: { x: ex, y: ey, up: long, rgb: long ? BUY : SELL }, exits });
      }
      // The hovered trade last, so it sits on top of any trade it crosses.
      drawn.sort((a, b) => Number(a.t.key === this.hover) - Number(b.t.key === this.hover));

      const lw = Math.max(1, Math.round(1.6 * hpr));
      for (const d of drawn) {
        const hot = d.t.key === this.hover;
        g.strokeStyle = `rgba(${d.t.direction === "long" ? BUY : SELL},${hot ? 1 : 0.9})`;
        g.lineWidth = hot ? lw + Math.round(hpr) : lw;
        g.setLineDash(hot ? [] : [4 * hpr, 3 * hpr]);
        for (const e of d.exits) {
          g.beginPath();
          g.moveTo(d.entry.x * hpr, d.entry.y * vpr);
          g.lineTo(e.x * hpr, e.y * vpr);
          g.stroke();
        }
      }
      g.setLineDash([]);

      const hits: HistHit[] = [];
      const arrow = (p: Pt, hot: boolean, key: string) => {
        const h = hot ? ARROW_H * 1.4 : ARROW_H;
        const w = hot ? ARROW_W * 1.4 : ARROW_W;
        // The tip is ON the price: a buy's arrow sits under it pointing up, a sell's above pointing down.
        const base = p.up ? p.y + h : p.y - h;
        g.beginPath();
        g.moveTo(p.x * hpr, p.y * vpr);
        g.lineTo((p.x - w) * hpr, base * vpr);
        g.lineTo((p.x + w) * hpr, base * vpr);
        g.closePath();
        g.fillStyle = `rgb(${p.rgb})`;
        g.fill();
        // A light rim: the arrow reads over a candle of its own colour and over the EMA lines alike.
        g.strokeStyle = "rgba(245,245,245,0.9)";
        g.lineWidth = Math.max(1, Math.round(hpr));
        g.stroke();
        if (p.x >= -HIT_R && p.x <= W + HIT_R) hits.push({ key, x: p.x, y: (p.y + base) / 2 });
      };
      for (const d of drawn) {
        const hot = d.t.key === this.hover;
        arrow(d.entry, hot, d.t.key);
        for (const e of d.exits) arrow(e, hot, d.t.key);
      }
      this.hits = hits;

      // Result tags beside the exits, the newest trades' first: where two would overlap, the older one
      // gives way (its result is a hover away), rather than both turning into an unreadable stack.
      if (!tagsOn) return;
      g.font = `600 ${Math.round(10 * vpr)}px ${FONT}`;
      g.textBaseline = "middle";
      const placed: { x: number; y: number; w: number }[] = [];
      for (let k = drawn.length - 1; k >= 0; k--) {
        const d = drawn[k];
        const hot = d.t.key === this.hover;
        for (const e of d.exits) {
          const v = e.last && !d.t.open ? d.t.pnl : e.pnl;
          if (v == null) continue;
          const text = usd(v);
          const w = g.measureText(text).width / hpr + 8;
          const x = e.x + ARROW_W + 4;
          const y = (e.up ? e.y + ARROW_H / 2 : e.y - ARROW_H / 2) - TAG_H / 2;
          if (x + w < 0 || x > W || y + TAG_H < 0 || y > H) continue;
          if (!hot && placed.some((o) => x < o.x + o.w + 2 && x + w + 2 > o.x && y < o.y + TAG_H && y + TAG_H > o.y)) {
            continue;
          }
          placed.push({ x, y, w });
          g.fillStyle = "rgba(10,10,10,0.78)";
          g.beginPath();
          roundRect(g, Math.round(x * hpr), Math.round(y * vpr), Math.round(w * hpr), Math.round(TAG_H * vpr), 3 * hpr);
          g.fill();
          g.fillStyle = `rgb(${v > 0 ? WIN : v < 0 ? LOSS : "163,163,163"})`;
          g.fillText(text, Math.round((x + 4) * hpr), Math.round((y + TAG_H / 2) * vpr) + 0.5);
        }
      }
    });
  }
}
