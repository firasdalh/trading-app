// AUTO FIBONACCI (asked 2026-11-07): one switch in the Indicators menu finds the trend on the chart's
// own timeframe, picks the swing that matters and draws the Fibonacci on it - on any timeframe, 5S to 1D.
//
// How the swing is found - the way a trader draws it by hand:
//  1. A zig-zag marks the real swings: price has to turn by 3 x ATR(14) before a high or a low counts as
//     a swing (the zig-zag TradingView's auto Fib uses: 3 x ATR(10)). Smaller wiggles are noise.
//  2. The trend is the swing structure: higher highs AND higher lows = up, lower highs AND lower lows =
//     down. The Fibonacci then goes on the trend's LATEST FINISHED impulse: in an uptrend from the swing
//     low that started the last push up to its high (1 at the low, 0 at the high), so the levels under
//     the high are where a pullback usually stalls; in a downtrend from the swing high that started the
//     last drop to its low.
//  FINISHED swings only (fixed 2026-11-07): the zig-zag's last point is the running extreme - price has
//     not turned away from it yet - and the Fibonacci used to end there, so every new high moved the 0
//     AND the targets: a target price was running to kept running away. Now both ends are finished
//     swings: the levels and the targets stay where they are while price runs to them, and the drawing
//     moves only once a new swing is finished (price turned 3 x ATR off it).
//  3. When highs and lows disagree:
//     - a higher high AND a lower low (the structure broke): the newer of the two breaks is the trend,
//       measured on the move that made it;
//     - a lower high AND a higher low (a squeeze): the biggest swing price is still trading inside,
//       starting no further back than the swing before the squeeze - the impulse the market is
//       digesting - so the levels frame the whole range, not one wiggle in it.
// The extensions 1.272 and 1.618 sit beyond the end, in the trend's direction: where the next push
// usually aims. Once price has gone past the 1 the leg is BROKEN: its targets are dead, and 1.272 / 1.618
// beyond the 1 (the BREAK zone, rose) show where such a break usually runs instead.
//
// The ZONES are what to look at (asked the same day): each its own colour, named on the chart, and lit up
// while price is in it - GOLD the golden zone 0.5-0.618 (where most pullbacks turn), TEAL the deep zone
// 0.618-0.786 (the last stop before the swing fails at the RED 1), BLUE the shallow zone 0.236-0.382 (a
// strong trend's pullback), VIOLET the target zone ext 1.272-1.618.
//
// Drawn as a series primitive (like chartZones.ts): painted by the chart in the candles' own frame, so it
// never lags a pan, and never part of the autoscale - the price axis keeps fitting the candles.
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
import type { Candle } from "../types";

type Target = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type Scope = Parameters<Parameters<Target["useBitmapCoordinateSpace"]>[0]>[0];

export interface FibSwing {
  i: number;          // bar index in the candles (= the series' logical index)
  price: number;
  high: boolean;      // a swing high (else a swing low)
}

export interface AutoFib {
  trend: "up" | "down";
  from: FibSwing;     // where the leg starts: level 1
  to: FibSwing;       // where it ends: level 0. Both ends are FINISHED swings, so the leg never moves under price
  broken: boolean;    // price has gone past the 1 since: this move failed (the break targets apply)
  // Why this leg: "trend" = HH + HL / LH + LL, the latest push; "break" = a new high AND a new low, the
  // newer break; "range" = a squeeze, the biggest swing price is still inside; "leg" = too few swings
  // to read a structure yet, the bigger of the last two legs.
  why: "trend" | "break" | "range" | "leg";
}

export const FIB_ATR_MULT = 3;
const ATR_N = 14;
const MIN_BARS = 30;

// The retracements (0 = the end of the leg, 1 = its start), then 1.272 / 1.618 either beyond the 0 (the
// targets, `ext`) or - once the leg is broken - beyond the 1. `pri` decides which tag stays when two would
// overlap (lower wins). Each level takes its zone's colour; 1 (red) is where the swing fails.
export interface FibLevel { r: number; color: string; pri: number; ext?: boolean; key?: boolean }
const RET_LEVELS: readonly FibLevel[] = [
  { r: 0, color: "#e5e5e5", pri: 1 },
  { r: 0.236, color: "#38bdf8", pri: 6 },
  { r: 0.382, color: "#38bdf8", pri: 4 },
  { r: 0.5, color: "#fbbf24", pri: 3 },
  { r: 0.618, color: "#fbbf24", pri: 0, key: true },     // the golden ratio: the level most pullbacks are watched at
  { r: 0.786, color: "#2dd4bf", pri: 5 },
  { r: 1, color: "#f87171", pri: 2 },
];
const TARGET_LEVELS: readonly FibLevel[] = [
  { r: 1.272, color: "#a78bfa", pri: 7, ext: true },
  { r: 1.618, color: "#a78bfa", pri: 8, ext: true },
];
const BREAK_LEVELS: readonly FibLevel[] = [
  { r: 1.272, color: "#fb7185", pri: 7 },
  { r: 1.618, color: "#fb7185", pri: 8 },
];
export function fibLevels(f: { broken: boolean }): readonly FibLevel[] {
  return [...RET_LEVELS, ...(f.broken ? BREAK_LEVELS : TARGET_LEVELS)];
}

// The zones that matter, each in its own colour (a / b: the levels that bound it; `alpha`: its fill).
export interface FibZone { label: string; a: number; b: number; color: string; alpha: number; ext?: boolean }
const PULLBACK_ZONES: readonly FibZone[] = [
  { label: "Shallow zone", a: 0.236, b: 0.382, color: "#38bdf8", alpha: 0.12 },
  { label: "Golden zone", a: 0.5, b: 0.618, color: "#fbbf24", alpha: 0.22 },
  { label: "Deep zone", a: 0.618, b: 0.786, color: "#2dd4bf", alpha: 0.15 },
];
const TARGET_ZONE: FibZone = { label: "Target zone", a: 1.272, b: 1.618, color: "#a78bfa", alpha: 0.14, ext: true };
const BREAK_ZONE: FibZone = { label: "Break zone", a: 1.272, b: 1.618, color: "#fb7185", alpha: 0.14 };
export function fibZones(f: { broken: boolean }): readonly FibZone[] {
  return [...PULLBACK_ZONES, f.broken ? BREAK_ZONE : TARGET_ZONE];
}

/** A zone as a span of fibPosition (an extension r lies at 1 - r: past the 0; a break level past the 1). */
export function zoneSpan(z: FibZone): [number, number] {
  return z.ext ? [1 - z.b, 1 - z.a] : [z.a, z.b];
}

/** The zone a fibPosition is in (the golden zone wins at 0.618), or null. */
export function fibZoneAt(pos: number, broken = false): FibZone | null {
  return fibZones({ broken }).find((z) => {
    const [lo, hi] = zoneSpan(z);
    return pos >= lo && pos <= hi;
  }) ?? null;
}

/** Price of a level on a leg: a retracement r measured back from the end, an extension beyond it. */
export function fibPrice(fib: { from: FibSwing; to: FibSwing }, r: number, ext = false): number {
  const range = fib.to.price - fib.from.price;
  return ext ? fib.from.price + range * r : fib.to.price - range * r;
}

/** Wilder ATR per bar (the bar's own true range until N bars exist). */
function atrSeries(c: Candle[], n = ATR_N): number[] {
  const out: number[] = [];
  let a = 0;
  for (let i = 0; i < c.length; i++) {
    const tr = i === 0 ? c[i].high - c[i].low
      : Math.max(c[i].high - c[i].low, Math.abs(c[i].high - c[i - 1].close), Math.abs(c[i].low - c[i - 1].close));
    a = i < n ? (a * i + tr) / (i + 1) : (a * (n - 1) + tr) / n;
    out.push(a);
  }
  return out;
}

/** The zig-zag: alternating swing highs and lows, each turn at least `mult` x ATR. The LAST one is the
 *  running extreme (price has not turned away from it far enough yet). */
export function zigzag(c: Candle[], mult = FIB_ATR_MULT): FibSwing[] {
  if (c.length < 2) return [];
  const atr = atrSeries(c);
  const out: FibSwing[] = [];
  let dir = 0;                                     // 1: rising (hunting the high), -1: falling, 0: not yet known
  let hi: FibSwing = { i: 0, price: c[0].high, high: true };
  let lo: FibSwing = { i: 0, price: c[0].low, high: false };
  for (let i = 1; i < c.length; i++) {
    const th = mult * atr[i];
    const b = c[i];
    if (dir === 0) {
      if (b.high > hi.price) hi = { i, price: b.high, high: true };
      if (b.low < lo.price) lo = { i, price: b.low, high: false };
      if (th > 0 && hi.price - lo.price >= th) {
        // The first swing is whichever extreme came first; the move away from it sets the direction.
        if (lo.i < hi.i) { out.push(lo); dir = 1; } else { out.push(hi); dir = -1; }
      }
    } else if (dir === 1) {
      if (b.high > hi.price) hi = { i, price: b.high, high: true };
      else if (th > 0 && hi.price - b.low >= th) {
        out.push(hi);
        dir = -1;
        lo = { i, price: b.low, high: false };
      }
    } else {
      if (b.low < lo.price) lo = { i, price: b.low, high: false };
      else if (th > 0 && b.high - lo.price >= th) {
        out.push(lo);
        dir = 1;
        hi = { i, price: b.high, high: true };
      }
    }
  }
  if (dir === 1) out.push(hi);
  else if (dir === -1) out.push(lo);
  return out;
}

const legSize = (a: FibSwing, b: FibSwing) => Math.abs(b.price - a.price);

/** The biggest swing price is still INSIDE, starting at one of `starts`: from a swing that is still the
 *  lowest low (highest high) of everything after it, to the highest high (lowest low) since. */
function containingLeg(c: Candle[], starts: FibSwing[]): { from: FibSwing; to: FibSwing } | null {
  const n = c.length;
  // Highest high / lowest low from bar i to the end, and the FIRST bar that made it.
  const hi: number[] = new Array(n), hiI: number[] = new Array(n);
  const lo: number[] = new Array(n), loI: number[] = new Array(n);
  for (let i = n - 1; i >= 0; i--) {
    const up = i === n - 1 || c[i].high >= hi[i + 1];
    hi[i] = up ? c[i].high : hi[i + 1];
    hiI[i] = up ? i : hiI[i + 1];
    const dn = i === n - 1 || c[i].low <= lo[i + 1];
    lo[i] = dn ? c[i].low : lo[i + 1];
    loI[i] = dn ? i : loI[i + 1];
  }
  let best: { from: FibSwing; to: FibSwing; size: number } | null = null;
  for (const a of starts) {
    const k = a.i + 1;
    if (k >= n) continue;
    if (a.high ? hi[k] > a.price : lo[k] < a.price) continue;     // price has been beyond it since
    const to: FibSwing = a.high ? { i: loI[k], price: lo[k], high: false } : { i: hiI[k], price: hi[k], high: true };
    const size = legSize(a, to);
    if (size > 0 && (!best || size > best.size)) best = { from: a, to, size };
  }
  return best;
}

/** Has any bar after the leg's end gone past its START (the 1)? Then the move failed. */
function brokenAfter(c: Candle[], a: FibSwing, b: FibSwing): boolean {
  for (let i = b.i + 1; i < c.length; i++) {
    if (b.high ? c[i].low < a.price : c[i].high > a.price) return true;
  }
  return false;
}

/** The trend and the FINISHED swing to draw the Fibonacci on, or null when the chart holds none yet. */
export function autoFib(c: Candle[], mult = FIB_ATR_MULT): AutoFib | null {
  if (c.length < MIN_BARS) return null;
  // The zig-zag's last point is the running extreme, not a swing yet: left out, so the leg - and its
  // targets - cannot move while price runs to them.
  const p = zigzag(c, mult).slice(0, -1);
  const n = p.length;
  if (n < 2) return null;
  const last = p[n - 1];
  const leg = (a: FibSwing, b: FibSwing, why: AutoFib["why"]): AutoFib => ({
    trend: b.high ? "up" : "down", from: a, to: b, why, broken: brokenAfter(c, a, b),
  });
  if (n === 2) return leg(p[0], p[1], "leg");
  if (n >= 4) {
    const highs = p.filter((s) => s.high);
    const lows = p.filter((s) => !s.high);
    const [h0, h1] = highs.slice(-2);
    const [l0, l1] = lows.slice(-2);
    if (h0 && h1 && l0 && l1) {
      const up = h1.price > h0.price && l1.price > l0.price;
      const down = h1.price < h0.price && l1.price < l0.price;
      // With the trend: the last finished push - or, after the pullback that followed it finished too,
      // still that push (price is now running back to its 0 and targets, which must hold still).
      if (up) return last.high ? leg(p[n - 2], last, "trend") : leg(p[n - 3], p[n - 2], "trend");
      if (down) return !last.high ? leg(p[n - 2], last, "trend") : leg(p[n - 3], p[n - 2], "trend");
      if (h1.price < h0.price && l1.price > l0.price) {
        // The squeeze is the last four swings; the one before them started the impulse it digests.
        // Measured over the finished swings only (up to the last one), like everything else.
        const box = containingLeg(c.slice(0, last.i + 1), p.slice(Math.max(0, n - 5), n - 1));
        if (box) return leg(box.from, box.to, "range");
      }
      // A higher high AND a lower low: the last leg made the newer break, so it is always the bigger.
      return legSize(p[n - 2], last) >= legSize(p[n - 3], p[n - 2])
        ? leg(p[n - 2], last, "break")
        : leg(p[n - 3], p[n - 2], "break");
    }
  }
  // Too few swings for a structure: the bigger of the last two legs.
  return legSize(p[n - 2], last) >= legSize(p[n - 3], p[n - 2])
    ? leg(p[n - 2], last, "leg")
    : leg(p[n - 3], p[n - 2], "leg");
}

/** The leg as it stands NOW: the same two swings (they never move), but a live bar that has gone past the
 *  1 since the candles were read breaks it at once. */
export function liveFib(fib: AutoFib, live: { index: number; high: number; low: number } | null): AutoFib {
  if (fib.broken || !live || live.index <= fib.to.i) return fib;
  const past = fib.trend === "up" ? live.low < fib.from.price : live.high > fib.from.price;
  return past ? { ...fib, broken: true } : fib;
}

/** Where `price` sits on the leg, as a retracement (0 = at the end, 1 = back at the start). */
export function fibPosition(fib: AutoFib, price: number): number | null {
  const range = fib.to.price - fib.from.price;
  return range ? (fib.to.price - price) / range : null;
}

/** The tag's short status after "📐 Fib ▲ up": where price is on the leg, and the zone it is in. */
export function fibStatus(f: AutoFib, pos: number | null): { text: string; zone: FibZone | null } {
  if (pos == null) return { text: "", zone: null };
  const zone = fibZoneAt(pos, f.broken);
  if (f.broken) {
    return {
      text: zone ? " · broken" : pos > 1.618 ? " · broken · past 1.618" : pos > 1 ? " · broken · past 1" : ` · broken · back at ${pos.toFixed(2)}`,
      zone,
    };
  }
  if (zone) return { text: "", zone };
  if (pos < -0.618) return { text: " · past 1.618 · targets hit", zone: null };
  if (pos < 0) return { text: " · past 0 → next 1.272", zone: null };
  if (pos > 1) return { text: " · past 1", zone: null };
  return { text: ` · price at ${pos.toFixed(2)}`, zone: null };
}

export interface FibSources {
  // The newest bar INCLUDING the provisional one the quote handler opens, with its live high / low / close.
  liveBar: () => { index: number; high: number; low: number; close: number } | null;
}

const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif";
const rgba = (hex: string, a: number) => {
  const v = parseInt(hex.slice(1), 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${a})`;
};
const rTxt = (r: number) => (r === 0 || r === 1 ? String(r) : r.toFixed(3).replace(/0+$/, ""));

function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

export class AutoFibPrimitive implements ISeriesPrimitive<Time> {
  private fib: AutoFib | null = null;
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private request: (() => void) | null = null;
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(private readonly src: FibSources) {
    // The zones sit UNDER the candles (they keep their true colours); lines and tags on top.
    const fills: ISeriesPrimitivePaneRenderer = { draw: (t) => this.draw(t, false) };
    const lines: ISeriesPrimitivePaneRenderer = { draw: (t) => this.draw(t, true) };
    this.views = [
      { zOrder: () => "bottom", renderer: () => (this.fib ? fills : null) },
      { zOrder: () => "top", renderer: () => (this.fib ? lines : null) },
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

  setFib(fib: AutoFib | null) {
    this.fib = fib;
    this.request?.();
  }

  updateAllViews() {
    /* geometry is worked out at draw time, so a live push past the swing moves the levels at once */
  }

  paneViews() {
    return this.views;
  }

  private draw(target: Target, top: boolean) {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series || !this.fib) return;
    const live = this.src.liveBar();
    const fib = liveFib(this.fib, live);
    const pos = live ? fibPosition(fib, live.close) : null;
    const ts = chart.timeScale();
    const xOf = (i: number) => {
      const c = ts.logicalToCoordinate(i as Logical);
      return c == null ? null : Number(c);
    };
    const yOf = (p: number) => {
      const c = series.priceToCoordinate(p);
      return c == null ? null : Number(c);
    };
    const xA = xOf(fib.from.i);
    const xB = xOf(fib.to.i);
    if (xA == null || xB == null) return;
    const zones = fibZones(fib).map((z) => {
      const ya = yOf(fibPrice(fib, z.a, z.ext));
      const yb = yOf(fibPrice(fib, z.b, z.ext));
      const [lo, hi] = zoneSpan(z);
      return { ...z, ya, yb, here: pos != null && pos >= lo && pos <= hi };
    });
    target.useBitmapCoordinateSpace((sc: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = sc;
      const W = sc.mediaSize.width;
      const H = sc.mediaSize.height;
      const x0 = Math.max(0, xA);
      if (x0 >= W - 4) return;

      if (!top) {
        // The zones: each in its own colour, strongest at the swing and fading to the right. The zone
        // price is in right now is lit up.
        for (const z of zones) {
          if (z.ya == null || z.yb == null) continue;
          const y1 = Math.min(z.ya, z.yb);
          const h = Math.abs(z.yb - z.ya);
          if (y1 > H || y1 + h < 0 || h < 0.5) continue;
          // A broken leg's pullback zones are history: faded, so its break zone is what reads.
          const a = z.here ? Math.min(0.42, z.alpha * 2) : fib.broken && z.label !== "Break zone" ? z.alpha * 0.5 : z.alpha;
          const grad = g.createLinearGradient(x0 * hpr, 0, W * hpr, 0);
          grad.addColorStop(0, rgba(z.color, a));
          grad.addColorStop(1, rgba(z.color, a * 0.35));
          g.fillStyle = grad;
          g.fillRect(x0 * hpr, y1 * vpr, (W - x0) * hpr, h * vpr);
        }
        return;
      }

      // The leg: a dashed line from the start swing to the end swing, a dot on each.
      const yA = yOf(fib.from.price);
      const yB = yOf(fib.to.price);
      if (yA != null && yB != null) {
        g.save();
        g.strokeStyle = "rgba(229,229,229,0.5)";
        g.lineWidth = Math.max(1, hpr);
        g.setLineDash([5 * hpr, 4 * hpr]);
        g.beginPath();
        g.moveTo(xA * hpr, yA * vpr);
        g.lineTo(xB * hpr, yB * vpr);
        g.stroke();
        g.restore();
        for (const [x, y] of [[xA, yA], [xB, yB]] as const) {
          g.fillStyle = "rgba(10,10,10,0.9)";
          g.beginPath();
          g.arc(x * hpr, y * vpr, 4.5 * hpr, 0, Math.PI * 2);
          g.fill();
          g.fillStyle = "rgba(229,229,229,0.95)";
          g.beginPath();
          g.arc(x * hpr, y * vpr, 3 * hpr, 0, Math.PI * 2);
          g.fill();
        }
      }

      // The levels, from the start of the leg to the right edge. 0.618 thick; 0 and 1 a little heavier;
      // the extensions dashed.
      const rows = fibLevels(fib).map((lv) => ({ ...lv, price: fibPrice(fib, lv.r, lv.ext), y: yOf(fibPrice(fib, lv.r, lv.ext)) }));
      const tags: { text: string; y: number; color: string; pri: number }[] = [];
      for (const r of rows) {
        if (r.y == null || r.y < -20 || r.y > H + 20) continue;
        const yy = Math.round(r.y * vpr) + 0.5;
        g.strokeStyle = rgba(r.color, r.key || r.r === 0 || r.r === 1 ? 0.95 : 0.6);
        g.lineWidth = (r.key ? 2 : r.r === 0 || r.r === 1 ? 1.5 : 1) * vpr;
        g.setLineDash(r.ext ? [6 * hpr, 4 * hpr] : []);
        g.beginPath();
        g.moveTo(x0 * hpr, yy);
        g.lineTo(W * hpr, yy);
        g.stroke();
        tags.push({ text: `${r.ext ? "ext " : ""}${rTxt(r.r)} (${series.priceFormatter().format(r.price)})`, y: r.y, color: r.color, pri: r.pri });
      }
      g.setLineDash([]);

      // A colour bar down the left edge of each zone, tying its tag to its band.
      for (const z of zones) {
        if (z.ya == null || z.yb == null) continue;
        const y1 = Math.max(-2, Math.min(z.ya, z.yb));
        const y2 = Math.min(H + 2, Math.max(z.ya, z.yb));
        if (y2 - y1 < 1) continue;
        g.fillStyle = rgba(z.color, 0.95);
        g.fillRect(x0 * hpr, y1 * vpr, 3 * hpr, (y2 - y1) * vpr);
      }

      // Level tags left of the leg's start when there is room (TradingView's place), else just inside it.
      // Zoomed out, the levels sit closer than a tag is tall: the more important tag keeps its place and
      // one that would overlap it is left out (its line is still drawn).
      g.font = `${10 * vpr}px ${FONT}`;
      g.textBaseline = "middle";
      const GAP = 14;
      const placed: typeof tags = [];
      for (const l of [...tags].sort((a, b) => a.pri - b.pri)) {
        if (placed.every((q) => Math.abs(q.y - l.y) >= GAP)) placed.push(l);
      }
      const pad = 4;
      let insideW = 0;                     // how far tags placed INSIDE the leg reach, for the zone names
      for (const l of placed) {
        const w = g.measureText(l.text).width / hpr;
        const outside = x0 - w - 2 * pad - 4 >= 0 && xA >= 0;
        const lx = outside ? x0 - w - 2 * pad - 4 : x0 + 6;
        if (!outside) insideW = Math.max(insideW, w + 2 * pad + 6);
        g.fillStyle = "rgba(10,10,10,0.82)";
        roundRect(g, lx * hpr, (l.y - 7) * vpr, (w + 2 * pad) * hpr, 14 * vpr, 3 * hpr);
        g.fill();
        g.fillStyle = l.color;
        g.fillText(l.text, (lx + pad) * hpr, l.y * vpr);
      }

      // The zone names, inside each zone just right of its colour bar: a solid pill in the zone's colour.
      // The zone price is in now says so.
      g.font = `bold ${10 * vpr}px ${FONT}`;
      for (const z of zones) {
        if (z.ya == null || z.yb == null) continue;
        const h = Math.abs(z.yb - z.ya);
        const yc = (z.ya + z.yb) / 2;
        if (h < 15 || yc < 8 || yc > H - 8) continue;      // too thin to name, or off screen
        const text = `${z.label.toUpperCase()}  ${z.ext ? "ext " : ""}${rTxt(z.a)}–${rTxt(z.b)}${z.here ? "  ● PRICE HERE" : ""}`;
        const w = g.measureText(text).width / hpr;
        const lx = x0 + 8 + insideW;
        if (lx + w + 2 * pad > W) continue;
        g.fillStyle = rgba(z.color, z.here ? 1 : 0.88);
        roundRect(g, lx * hpr, (yc - 7.5) * vpr, (w + 2 * pad) * hpr, 15 * vpr, 7.5 * Math.min(hpr, vpr));
        g.fill();
        g.fillStyle = "#0a0a0a";
        g.fillText(text, (lx + pad) * hpr, yc * vpr);
      }
    });
  }
}

/** The toolbar tag's hover text: why this leg, its ends, where price is on it and how to read it. */
export function fibExplain(f: AutoFib, pos: number | null, fmt: (v: number) => string): string {
  const up = f.trend === "up";
  const why = {
    trend: up ? "Uptrend: higher highs and higher lows. Measured on the latest finished push up"
              : "Downtrend: lower highs and lower lows. Measured on the latest finished drop",
    break: up ? "The structure broke UP: a higher high after a lower low. Measured on the move that broke it"
              : "The structure broke DOWN: a lower low after a higher high. Measured on the move that broke it",
    range: `A squeeze (lower highs and higher lows) after a move ${up ? "up" : "down"}. Measured on that move - the range price is still inside`,
    leg: `Only a few swings on this chart yet. Measured on the bigger recent move, ${up ? "up" : "down"}`,
  }[f.why];
  const ends = `, from the swing ${up ? "low" : "high"} ${fmt(f.from.price)} (1) to the ${up ? "high" : "low"} ${fmt(f.to.price)} (0).`
    + " Both are finished swings: the levels and the targets stay where they are while price runs to them, and the"
    + ` Fibonacci moves only once a new swing is finished (price turns ${FIB_ATR_MULT} x ATR(14) off it).`;
  if (f.broken) {
    const b1 = fmt(fibPrice(f, 1.272)), b2 = fmt(fibPrice(f, 1.618));
    return `${why}${ends} BROKEN: price has gone past the 1 since, so this ${up ? "up" : "down"}-move failed and its targets are off.`
      + ` ROSE = the break zone, 1.272 (${b1}) to 1.618 (${b2}) ${up ? "below the low" : "above the high"}: where such a break often runs.`
      + (pos != null ? ` Price is at ${pos.toFixed(2)} of the leg.` : "");
  }
  const zone = pos != null ? fibZoneAt(pos) : null;
  const t1 = fmt(fibPrice(f, 1.272, true)), t2 = fmt(fibPrice(f, 1.618, true));
  const where = pos == null ? ""
    : zone ? ` Price is in the ${zone.label.toLowerCase()} (${pos.toFixed(2)}).`
    : pos < -0.618 ? ` Price is past 1.618: both targets are hit.`
    : pos < 0 ? ` Price is past the 0, heading for the targets: 1.272 at ${t1}, then 1.618 at ${t2}.`
    : ` Price is at ${pos.toFixed(2)} of the leg.`;
  const use = up
    ? ` GOLD = the golden zone (0.5-0.618), where most pullbacks in an uptrend are bought. TEAL = the deep zone (0.618-0.786), the last stop before the swing fails at the red 1. BLUE = shallow (0.236-0.382), the pullback of a strong trend. VIOLET = the targets above the high: 1.272 at ${t1}, 1.618 at ${t2}.`
    : ` GOLD = the golden zone (0.5-0.618), where most bounces in a downtrend are sold. TEAL = the deep zone (0.618-0.786), the last stop before the swing fails at the red 1. BLUE = shallow (0.236-0.382), the bounce of a strong trend. VIOLET = the targets below the low: 1.272 at ${t1}, 1.618 at ${t2}.`;
  return `${why}${ends}${where}${use}`;
}
