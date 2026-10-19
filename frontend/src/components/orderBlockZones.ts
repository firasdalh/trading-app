// Order-block zones for the main chart: the area a move departed from, drawn from its origin candle
// out to the right edge, the way a desk draws supply and demand.
//
// A sibling of chartZones.ts and deliberately built the same way — a series primitive, painted BY
// the chart in the same frame and coordinate space as the candles, so a fast pan cannot leave the
// boxes a frame behind the price they belong to.
//
// COLOUR, and why it is not green/red: the SL/TP boxes next door already own green and red, and
// they mean something entirely different (your money, relative to your entry). Order blocks are a
// statement about the market, not about your trade, so they get their own pair — cyan for demand,
// amber for supply — and a lower alpha, so they read as background structure rather than as levels
// competing with your stop.
import type {
  IChartApiBase,
  ISeriesApi,
  ISeriesPrimitive,
  ISeriesPrimitivePaneRenderer,
  ISeriesPrimitivePaneView,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import { roundRect } from "./chartZones";

type Target = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type Scope = Parameters<Parameters<Target["useBitmapCoordinateSpace"]>[0]>[0];

export interface ObZone {
  key: string;
  kind: "bullish" | "bearish";
  top: number;
  bottom: number;
  /** The origin candle — where the zone is drawn from (seconds since epoch). */
  originTime: Time;
  /** 0..100 from the backend; drives the fill's opacity so the eye ranks them without reading. */
  strength: number;
  /** Price has returned into it at least once — drawn hollow, because the first test is the clean one. */
  mitigated: boolean;
  touches: number;
  fvg: boolean;
  displacementAtr: number;
}

const CYAN = "34,211,238";    // demand (bullish block)
const AMBER = "251,146,60";   // supply (bearish block)

interface Box {
  z: ObZone;
  x0: number;
  yTop: number;
  yBot: number;
}


export class OrderBlockPrimitive implements ISeriesPrimitive<Time> {
  private zones: ObZone[] = [];
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private request: (() => void) | null = null;
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor() {
    // Fills under the candles — tinting over them would recolour the candles, and a candle's colour
    // is the one thing on this chart that must stay literally true. Labels on top.
    const fills: ISeriesPrimitivePaneRenderer = { draw: (t) => this.drawFills(t) };
    const labels: ISeriesPrimitivePaneRenderer = { draw: (t) => this.drawLabels(t) };
    this.views = [
      { zOrder: () => "bottom", renderer: () => (this.zones.length ? fills : null) },
      { zOrder: () => "top", renderer: () => (this.zones.length ? labels : null) },
    ];
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

  setZones(zones: ObZone[]) {
    this.zones = zones;
    this.request?.();
  }

  updateAllViews() {
    /* geometry is computed at draw time, as in chartZones.ts */
  }

  paneViews() {
    return this.views;
  }

  private boxes(paneW: number): Box[] {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series) return [];
    const ts = chart.timeScale();
    const out: Box[] = [];
    for (const z of this.zones) {
      const yTop = series.priceToCoordinate(z.top);
      const yBot = series.priceToCoordinate(z.bottom);
      if (yTop == null || yBot == null) continue;
      // A block whose origin scrolled off the left still matters — price can be sitting in it — so
      // it clamps to the left edge rather than vanishing.
      const xc = ts.timeToCoordinate(z.originTime);
      const x0 = xc == null ? 0 : Math.max(0, Number(xc));
      if (x0 >= paneW - 2) continue;        // origin is to the RIGHT of the view
      out.push({ z, x0, yTop: Number(yTop), yBot: Number(yBot) });
    }
    return out;
  }

  private drawFills(target: Target) {
    target.useBitmapCoordinateSpace((s: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = s;
      for (const b of this.boxes(s.mediaSize.width)) {
        const rgb = b.z.kind === "bullish" ? CYAN : AMBER;
        // Opacity carries the strength score, so the strongest block is the one the eye lands on
        // without reading a single number. A mitigated zone is drawn hollow: it has already been
        // traded into, and the first return is the one worth taking.
        const a = (b.z.mitigated ? 0.05 : 0.10) + (b.z.strength / 100) * (b.z.mitigated ? 0.05 : 0.12);
        const left = Math.round(b.x0 * hpr);
        const right = Math.round(s.mediaSize.width * hpr);
        const top = Math.round(Math.min(b.yTop, b.yBot) * vpr);
        const bot = Math.round(Math.max(b.yTop, b.yBot) * vpr);
        g.fillStyle = `rgba(${rgb},${a})`;
        g.fillRect(left, top, right - left, Math.max(1, bot - top));

        g.save();
        g.strokeStyle = `rgba(${rgb},${b.z.mitigated ? 0.45 : 0.85})`;
        g.lineWidth = Math.max(1, Math.floor(vpr));
        g.setLineDash(b.z.mitigated ? [4 * hpr, 4 * hpr] : []);
        g.beginPath();
        g.moveTo(left, top + 0.5);
        g.lineTo(right, top + 0.5);
        g.moveTo(left, bot - 0.5);
        g.lineTo(right, bot - 0.5);
        g.stroke();
        // The origin edge, drawn solid even on a mitigated block: it is the candle the move left
        // from, and it is what you measure a retest against.
        g.setLineDash([]);
        g.beginPath();
        g.moveTo(left + 0.5, top);
        g.lineTo(left + 0.5, bot);
        g.stroke();
        g.restore();
      }
    });
  }

  private drawLabels(target: Target) {
    // Labels sit on a dark pill with a coloured border, the same treatment as the SL/TP tags next
    // door. The first version wrote bare coloured text straight onto the chart, where it collided with
    // candles, moving averages and the engine's own "PB" markers - readable in a screenshot only if you
    // already knew what it said, which is no use on a tool meant for your eye.
    target.useBitmapCoordinateSpace((s: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = s;
      const fontPx = Math.max(10, Math.round(10.5 * vpr));
      const H = Math.round(16 * vpr);                 // pill height
      const PAD = 6 * hpr;
      g.save();
      g.font = `600 ${fontPx}px system-ui, -apple-system, 'Segoe UI', sans-serif`;
      g.textBaseline = "middle";
      const placed: { x: number; y: number; w: number }[] = [];
      for (const b of this.boxes(s.mediaSize.width)) {
        const rgb = b.z.kind === "bullish" ? CYAN : AMBER;
        const bits = [b.z.kind === "bullish" ? "Demand" : "Supply", `${b.z.strength}`];
        if (b.z.fvg) bits.push("FVG");
        if (b.z.touches > 0) bits.push(`${b.z.touches}x`);
        const text = bits.join(" · ");
        const w = Math.round(g.measureText(text).width + PAD * 2);
        const top = Math.round(Math.min(b.yTop, b.yBot) * vpr);
        const bot = Math.round(Math.max(b.yTop, b.yBot) * vpr);
        const x = Math.round(b.x0 * hpr) + Math.round(4 * hpr);
        // Centred in the zone when it is tall enough; otherwise sitting ON its top edge. The first
        // version skipped the label on thin zones, which on a zoomed-out chart is most of them -
        // leaving coloured bands with nothing to say what they were.
        let y = bot - top >= H + 2 * vpr ? Math.round((top + bot) / 2 - H / 2) : top - H - Math.round(2 * vpr);
        // Overlapping zones would stack their pills on one row; step each clash down a row.
        for (let guard = 0; guard < 8; guard++) {
          const clash = placed.some((q) => x < q.x + q.w && q.x < x + w && Math.abs(q.y - y) < H);
          if (!clash) break;
          y += H + Math.round(2 * vpr);
        }
        placed.push({ x, y, w });
        g.fillStyle = "rgba(12,12,14,0.86)";
        g.beginPath();
        roundRect(g, x, y, w, H, 3 * hpr);
        g.fill();
        g.strokeStyle = `rgba(${rgb},${b.z.mitigated ? 0.55 : 0.9})`;
        g.lineWidth = Math.max(1, Math.floor(hpr));
        g.stroke();
        g.fillStyle = `rgba(${rgb},${b.z.mitigated ? 0.75 : 1})`;
        g.fillText(text, x + PAD, y + H / 2 + 0.5);
      }
      g.restore();
    });
  }
}
