// The EMA 10 / 20 ribbon: the space between the two averages, tinted GREEN while the fast one is
// above the slow one and RED while it is below - so the crossing, and which way it went, is visible
// at a glance instead of being two thin lines to tell apart.
//
// A series primitive like orderBlockZones.ts: painted by the chart in the candles' own frame and
// coordinates (so it never lags a pan), and under the candles, which keep their true colours.
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

type Target = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type Scope = Parameters<Parameters<Target["useBitmapCoordinateSpace"]>[0]>[0];

export interface RibbonPoint {
  time: Time;
  fast: number;   // EMA 10
  slow: number;   // EMA 20
}

const UP = "rgba(34,197,94,0.20)";     // fast above slow
const DOWN = "rgba(244,63,94,0.20)";   // fast below slow

export class EmaRibbonPrimitive implements ISeriesPrimitive<Time> {
  private pts: RibbonPoint[] = [];
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private request: (() => void) | null = null;
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor() {
    const fill: ISeriesPrimitivePaneRenderer = { draw: (t) => this.draw(t) };
    this.views = [{ zOrder: () => "bottom", renderer: () => (this.pts.length > 1 ? fill : null) }];
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

  setPoints(pts: RibbonPoint[]) {
    this.pts = pts;
    this.request?.();
  }

  updateAllViews() {
    /* geometry is worked out at draw time */
  }

  paneViews() {
    return this.views;
  }

  private draw(target: Target) {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series) return;
    const ts = chart.timeScale();
    // Screen points of each bar's pair of averages; bars off the time scale are skipped.
    const xy: { x: number; f: number; s: number; d: number }[] = [];
    for (const p of this.pts) {
      const x = ts.timeToCoordinate(p.time);
      const f = series.priceToCoordinate(p.fast);
      const s = series.priceToCoordinate(p.slow);
      if (x == null || f == null || s == null) continue;
      xy.push({ x: Number(x), f: Number(f), s: Number(s), d: p.fast - p.slow });
    }
    if (xy.length < 2) return;
    target.useBitmapCoordinateSpace((sc: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = sc;
      const W = sc.mediaSize.width;
      const poly = (pts: [number, number][], color: string) => {
        g.fillStyle = color;
        g.beginPath();
        pts.forEach(([x, y], i) => (i ? g.lineTo(x * hpr, y * vpr) : g.moveTo(x * hpr, y * vpr)));
        g.closePath();
        g.fill();
      };
      for (let i = 1; i < xy.length; i++) {
        const a = xy[i - 1], b = xy[i];
        if (b.x < 0 || a.x > W) continue;                  // off screen
        if (a.d === 0 && b.d === 0) continue;
        if (Math.sign(a.d) === Math.sign(b.d) || a.d === 0 || b.d === 0) {
          const up = (b.d || a.d) > 0;
          poly([[a.x, a.f], [b.x, b.f], [b.x, b.s], [a.x, a.s]], up ? UP : DOWN);
          continue;
        }
        // The lines cross between these two bars: each side of the crossing point keeps its colour.
        const t = a.d / (a.d - b.d);
        const xi = a.x + t * (b.x - a.x);
        const yi = a.f + t * (b.f - a.f);
        poly([[a.x, a.f], [xi, yi], [a.x, a.s]], a.d > 0 ? UP : DOWN);
        poly([[xi, yi], [b.x, b.f], [b.x, b.s]], b.d > 0 ? UP : DOWN);
      }
    });
  }
}
