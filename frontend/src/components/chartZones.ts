// TradingView-style risk / reward boxes for the main chart: a red box from entry to stop and a green
// box from entry to target, starting at the bar the trade began and running to the right edge.
//
// Why a series primitive and not the canvas under the chart: a primitive is painted BY the chart, in
// the same frame and with the same coordinates as the candles. The background canvas has to chase
// the chart with a requestAnimationFrame poll, so on a fast pan or a drag the box would lag a frame
// behind the price lines it is supposed to sit between. Here it cannot.
//
// Geometry is worked out at DRAW time rather than in updateAllViews(). Dragging an SL/TP price line
// only triggers a light repaint, which redraws without calling updateAllViews — a cached box would
// stay where the line used to be while the line itself followed the mouse. Reading the live level on
// every paint is a dozen arithmetic operations for one or two zones, so there is nothing to cache.
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

type Target = Parameters<ISeriesPrimitivePaneRenderer["draw"]>[0];
type Scope = Parameters<Parameters<Target["useBitmapCoordinateSpace"]>[0]>[0];

export interface TradeZone {
  key: string;
  // "position" = a live trade (solid border, stronger fill, live P&L).
  // "plan" = the proposed trade (dashed border, lighter fill), so a plan never reads as a fill.
  kind: "position" | "plan";
  direction: "long" | "short";
  entry: number;
  stop: number | null;
  target: number | null;
  // The stop the trade was OPENED with (GET /positions/live `initial_stop_loss`). R is measured
  // against it, as everywhere else on the desk: measured against the live stop, widening the stop
  // made the loss read as fewer R. null = no record of it (a trade opened in the terminal): the live
  // stop is the basis. NOT SET (undefined) on a position = the caller has not passed it, and the
  // entry tag then prints no R at all rather than one that contradicts the strip above the chart.
  initialStop?: number | null;
  // Drag-handle keys for the stop / target price lines. While one is being dragged, the zone reads
  // that line's in-progress price instead of the broker's, so the box follows the mouse.
  stopKey?: string;
  targetKey?: string;
  // Logical bar index the box starts at (the entry candle; -1 = before the loaded history, which
  // clamps to the left edge). null: a plan starts at the last bar; a position whose open time is
  // unknown starts FALLBACK_BARS back from it.
  startIndex: number | null;
  // Account-currency P&L per 1.0 of price, signed so (level − entry) × this = P&L at that level.
  // null when it can't be derived (a plan, or a position still sitting exactly at its entry).
  usdPerPrice: number | null;
  pnl: number | null;          // the broker's floating P&L — shown as-is, never recomputed
  qty: number | null;
  // "#1", "#2"… when several trades are open on this pair: prefixed to every label of the zone, so a
  // TP or SL tag that had to move to stay readable still says which trade it belongs to.
  tag?: string;
}

export interface ZoneSources {
  // Current price of a drag-handle's line (null when no such handle exists).
  liveLevel: (key: string) => number | null;
  // The newest bar on the chart (including a provisional one) and its live close.
  liveBar: () => { index: number; close: number } | null;
}

const GREEN = "38,166,154";   // the chart's own TP line colour (#26a69a), so the box and its edge match
const RED = "239,83,80";      // the SL line colour (#ef5350)
const FALLBACK_BARS = 12;
// A box that starts at its entry candle is only a sliver while the trade is young (and a plan's,
// starting at the last bar, is never wider than the empty right-offset gap). Below this width a
// position's zone gets a faded LEAD-IN to its left so the red/green bands still read at a glance; the
// hard vertical edge and the darker "travelled" band still start at the real entry bar, so WHEN the
// trade began stays exact. A plan's box just starts this far from the right edge.
const minWidth = (paneW: number) => Math.min(paneW, Math.max(240, paneW * 0.3));
const LABEL_H = 17;
const LABEL_GAP = 4;
// Kept clear at the right end of the pane when a label has to slide right: the chart prints each
// price line's own title there ("SL −$57 · drag", "▼ entry"), on the same rows as our labels.
const RIGHT_RESERVE = 150;
const FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif";

const usd = (v: number) => fmtUsd(v, { sign: true }).replace("-", "−");
const rTxt = (r: number) => `${r >= 0 ? "" : "−"}${Math.abs(r).toFixed(Math.abs(r) >= 10 ? 0 : 1)}R`;

type Label = { text: string; x: number; y: number; w: number; fg: string; bg: string; border?: string };
type Box = {
  z: TradeZone;
  xL: number;            // where the (faded) lead-in starts; == x0 when the box is wide enough
  x0: number; xR: number; xNow: number | null;
  yE: number; yS: number | null; yT: number | null; yNow: number | null;
  stop: number | null; target: number | null;
  stopLocked: boolean;   // stop sits on the PROFIT side of entry — trailed, so it locks a gain
};

export class TradeZonesPrimitive implements ISeriesPrimitive<Time> {
  private zones: TradeZone[] = [];
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private request: (() => void) | null = null;
  // Same array every call: the library caches pane views by reference.
  private readonly views: readonly ISeriesPrimitivePaneView[];

  constructor(private readonly src: ZoneSources) {
    // Fills sit UNDER the candles (tinting on top would recolour the candles, which must stay true);
    // the labels sit on top so a candle never hides the number you are looking for.
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

  setZones(zones: TradeZone[]) {
    this.zones = zones;
    this.request?.();
  }

  updateAllViews() {
    /* geometry is computed at draw time — see the header comment */
  }

  paneViews() {
    return this.views;
  }

  // --- geometry (media pixels) ------------------------------------------------------------------
  private boxes(paneW: number): Box[] {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series) return [];
    const ts = chart.timeScale();
    const live = this.src.liveBar();
    const y = (p: number | null): number | null => {
      if (p == null) return null;
      const c = series.priceToCoordinate(p);
      return c == null ? null : Number(c);
    };
    const x = (l: number): number | null => {
      const c = ts.logicalToCoordinate(l as Logical);
      return c == null ? null : Number(c);
    };
    const out: Box[] = [];
    for (const z of this.zones) {
      const yE = y(z.entry);
      if (yE == null) continue;
      const stop = (z.stopKey ? this.src.liveLevel(z.stopKey) : null) ?? z.stop;
      const target = (z.targetKey ? this.src.liveLevel(z.targetKey) : null) ?? z.target;
      let startL: number | null = z.startIndex;
      if (startL == null) {
        if (!live) continue;
        startL = z.kind === "plan" ? live.index : live.index - FALLBACK_BARS;
      }
      const xs = x(startL);
      if (xs == null) continue;
      let x0 = Math.max(0, xs);
      if (x0 >= paneW - 2) continue;   // the trade starts right of the view (scrolled back in time)
      const xL = Math.max(0, Math.min(x0, paneW - minWidth(paneW)));
      // A plan has no start time to be faithful to, so its box is simply drawn at full width — a
      // fade there would only wash out the one thing it exists to show.
      if (z.kind === "plan") x0 = xL;
      const sign = z.direction === "long" ? 1 : -1;
      const stopLocked = stop != null && (stop - z.entry) * sign > 0;
      const xl = live ? x(live.index) : null;
      out.push({
        z, xL, x0, xR: paneW,
        xNow: xl == null ? null : Math.min(paneW, Math.max(x0, xl)),
        yE, yS: y(stop), yT: y(target),
        yNow: z.kind === "position" && live ? y(live.close) : null,
        stop, target, stopLocked,
      });
    }
    return out;
  }

  private drawFills(target: Target) {
    target.useBitmapCoordinateSpace((s: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = s;
      const lw = Math.max(1, Math.floor(vpr));
      const off = lw % 2 ? 0.5 : 0;   // keeps a 1px line on one pixel row instead of smeared over two
      for (const b of this.boxes(s.mediaSize.width)) {
        const plan = b.z.kind === "plan";
        const lead = Math.round(b.xL * hpr);
        const left = Math.round(b.x0 * hpr);
        const right = Math.round(b.xR * hpr);
        const dash = plan ? [5 * hpr, 4 * hpr] : [];
        // A colour that fades in from nothing at the lead-in's start to full strength at the entry bar.
        const paint = (rgb: string, a: number): string | CanvasGradient => {
          if (left - lead < 2) return `rgba(${rgb},${a})`;
          const gr = g.createLinearGradient(lead, 0, right, 0);
          gr.addColorStop(0, `rgba(${rgb},0)`);
          gr.addColorStop((left - lead) / Math.max(1, right - lead), `rgba(${rgb},${a})`);
          gr.addColorStop(1, `rgba(${rgb},${a})`);
          return gr;
        };
        const hline = (yMedia: number, color: string | CanvasGradient) => {
          const yy = Math.round(yMedia * vpr) + off;
          g.strokeStyle = color;
          g.lineWidth = lw;
          g.setLineDash(dash);
          g.beginPath();
          g.moveTo(lead, yy);
          g.lineTo(right, yy);
          g.stroke();
          g.setLineDash([]);
        };
        const band = (yEdge: number, rgb: string, fillA: number) => {
          const top = Math.round(Math.min(b.yE, yEdge) * vpr);
          const bot = Math.round(Math.max(b.yE, yEdge) * vpr);
          if (bot - top < 1) return;
          g.fillStyle = paint(rgb, fillA);
          g.fillRect(lead, top, right - lead, bot - top);
          // A slightly stronger 1px edge on the SL/TP side and at the entry bar, like TradingView's
          // position tool. The entry side gets its own neutral line below.
          hline(yEdge, paint(rgb, plan ? 0.6 : 0.75));
          g.strokeStyle = `rgba(${rgb},${plan ? 0.5 : 0.6})`;
          g.lineWidth = Math.max(1, Math.floor(hpr));
          g.setLineDash(dash);
          g.beginPath();
          g.moveTo(left + off, top);
          g.lineTo(left + off, bot);
          g.stroke();
          g.setLineDash([]);
        };
        if (b.yT != null) band(b.yT, GREEN, plan ? 0.12 : 0.22);
        if (b.yS != null) {
          // A trailed stop is a LOCKED gain, not a risk — painting it red would say the opposite of
          // what it means. A green band entry→stop marks what can no longer be given back.
          band(b.yS, b.stopLocked ? GREEN : RED, plan ? 0.12 : b.stopLocked ? 0.16 : 0.22);
        }

        // Where price has travelled since entry: TradingView darkens entry→current price over the
        // bars the trade has been open, green when it is working and red when it isn't. It turns
        // "how far along am I?" from reading numbers into one glance.
        if (!plan && b.yNow != null && b.xNow != null && b.xNow > b.x0) {
          const sign = b.z.direction === "long" ? 1 : -1;
          const winning = (b.yE - b.yNow) * sign > 0;   // y grows downward
          const top = Math.round(Math.min(b.yE, b.yNow) * vpr);
          const bot = Math.round(Math.max(b.yE, b.yNow) * vpr);
          if (bot > top) {
            g.fillStyle = `rgba(${winning ? GREEN : RED},0.18)`;
            g.fillRect(left, top, Math.round(b.xNow * hpr) - left, bot - top);
          }
        }

        // Thin entry line across the box — the shared edge of the two zones.
        hline(b.yE, paint(plan ? "147,197,253" : "229,229,229", plan ? 0.7 : 0.55));
      }
    });
  }

  private drawLabels(target: Target) {
    const series = this.series;
    if (!series) return;
    const fmt = series.priceFormatter();
    target.useBitmapCoordinateSpace((s: Scope) => {
      const { context: g, horizontalPixelRatio: hpr, verticalPixelRatio: vpr } = s;
      const W = s.mediaSize.width;
      const H = s.mediaSize.height;
      g.font = `600 ${Math.round(11 * vpr)}px ${FONT}`;
      g.textBaseline = "middle";
      const measure = (t: string) => g.measureText(t).width / hpr + 12;
      const live = this.src.liveBar();
      // 22px top margin keeps a label for an off-screen level clear of the OHLC legend row.
      const clampY = (yy: number) => Math.max(22, Math.min(H - LABEL_H - 2, yy));

      // Each zone first works out where its own labels WANT to be; they are then placed for all zones
      // together. Two trades on one pair (a scale-in, a re-entry) stack their boxes, and their tags —
      // each anchored at its box's left edge, often at nearly the same price — used to land on top
      // of each other, so neither could be read.
      type Spec = { entry: Label; chip: Label | null; edges: Label[] };
      const specs: Spec[] = [];
      for (const b of this.boxes(W)) {
        const z = b.z;
        const plan = z.kind === "plan";
        const sign = z.direction === "long" ? 1 : -1;
        const pre = z.tag ? `${z.tag} ` : "";
        // Stop at (or within a hair of) entry = breakeven: no risk left, so no R either — dividing
        // by a near-zero distance would print a meaningless 500R.
        const be = b.stop != null && Math.abs(b.stop - z.entry) <= Math.abs(z.entry) * 1e-4;
        // What the trade risks NOW (live stop; follows a drag) — the R:R on the TP tag.
        const riskNow = b.stop != null && !b.stopLocked && !be ? Math.abs(z.entry - b.stop) : null;
        // What it risked at ENTRY — the unit R is counted in. It survives a trail to breakeven, so a
        // trailed winner still reads "+1.8R"; with no record of the entry stop, the live one.
        const init = z.initialStop;
        const riskEntry =
          init != null && (z.entry - init) * sign > Math.abs(z.entry) * 1e-6 ? Math.abs(z.entry - init) : riskNow;
        const atLevel = (lv: number) => (z.usdPerPrice != null ? usd((lv - z.entry) * z.usdPerPrice) : null);
        // At the LEFT end of the band: the chart already prints each line's title ("SL −$57 · drag")
        // against the price axis on the right, and a label there would sit on top of it.
        const xAt = (w: number) => Math.max(2, Math.min(b.xL + 6, W - w - 4));

        // Entry tag, centred on the entry line: what the trade is and how it is doing right now.
        const parts: string[] = [];
        if (plan) parts.push(`${sign > 0 ? "▲" : "▼"} PLAN ${z.direction.toUpperCase()}`);
        else {
          parts.push(`${sign > 0 ? "▲" : "▼"} ${z.direction.toUpperCase()}${z.qty != null ? ` ${z.qty}` : ""}`);
          if (z.pnl != null) parts.push(usd(z.pnl));
          if (riskEntry && live && z.initialStop !== undefined) {
            parts.push(rTxt(((live.close - z.entry) * sign) / riskEntry));
          }
          if (b.stop != null && be) parts.push("SL at breakeven");
        }
        const entryTxt = pre + parts.join(" · ");
        const ew = measure(entryTxt);
        const pnlRgb = (z.pnl ?? 0) > 0 ? GREEN : (z.pnl ?? 0) < 0 ? RED : "59,130,246";
        const entryLbl: Label = {
          text: entryTxt, x: xAt(ew), y: clampY(b.yE - LABEL_H / 2), w: ew,
          fg: plan ? "#bfdbfe" : "#ffffff",
          bg: plan ? "rgba(23,23,23,0.88)" : `rgba(${pnlRgb},0.92)`,
          border: plan ? "rgba(147,197,253,0.7)" : undefined,
        };
        // An unprotected live trade gets its own amber warning chip rather than a word inside the
        // P&L tag: on a green (winning) tag "no stop" would read as fine, and it is the opposite.
        // It rides just right of the entry tag, wherever that ends up (x is set when placed).
        let chip: Label | null = null;
        if (!plan && b.stop == null) {
          const txt = "⚠ NO STOP";
          chip = { text: txt, x: 0, y: entryLbl.y, w: measure(txt), fg: "#1c1917", bg: "rgba(245,158,11,0.95)" };
        }

        // SL / TP labels sit just inside the box against its far edge — away from the entry line,
        // where the candles of the open trade usually are. If the box is too thin to hold one
        // without touching the entry tag, it moves just outside the edge instead.
        const edges: Label[] = [];
        const edgeLabel = (yEdge: number | null, text: string, rgb: string) => {
          if (yEdge == null) return;
          const w = measure(text);
          const up = yEdge < b.yE;          // the box extends upward from entry
          const inside = up ? yEdge + 5 : yEdge - 5 - LABEL_H;
          const outside = up ? yEdge - 4 - LABEL_H : yEdge + 4;
          const hitsEntry = (yy: number) => yy < entryLbl.y + LABEL_H + 2 && yy + LABEL_H + 2 > entryLbl.y;
          const yy = clampY(hitsEntry(inside) ? outside : inside);
          edges.push({
            text, x: xAt(w), y: yy, w,
            fg: plan ? `rgb(${rgb})` : "#ffffff",
            bg: plan ? "rgba(23,23,23,0.88)" : `rgba(${rgb},0.9)`,
            border: plan ? `rgba(${rgb},0.8)` : undefined,
          });
        };
        // The $ at each level is already in the price line's own title on the right ("SL −$78 ·
        // drag"); what only these tags can add is the ratio between the two.
        if (b.target != null) {
          const bits = [`${pre}TP ${fmt.format(b.target)}`];
          const d = atLevel(b.target);
          if (d) bits.push(d);
          if (riskNow) bits.push(`R:R ${(Math.abs(b.target - z.entry) / riskNow).toFixed(2)}`);
          edgeLabel(b.yT, bits.join(" · "), GREEN);
        }
        if (b.stop != null && !be) {
          const bits = [`${pre}SL ${fmt.format(b.stop)}`];
          const d = atLevel(b.stop);
          if (d) bits.push(b.stopLocked ? `${d} locked` : d);
          else if (b.stopLocked) bits.push("profit locked");
          // The stop is −1R by definition — unless it has MOVED since entry. Then its distance in
          // entry-R is the news: "−2.2R" = hitting it now costs 2.2× what the trade was sized to lose.
          if (riskNow && riskEntry && riskEntry !== riskNow && Math.abs(riskNow / riskEntry - 1) > 0.05) {
            bits.push(rTxt(-riskNow / riskEntry));
          }
          edgeLabel(b.yS, bits.join(" · "), b.stopLocked ? GREEN : RED);
        }
        specs.push({ entry: entryLbl, chip, edges });
      }

      // Placement. Entry tags go first, so the trade itself never moves to make room for a TP/SL
      // tag. A label that would cover one already placed slides SIDEWAYS along its own row — right
      // while there is room short of RIGHT_RESERVE, else left — because the chart's price line runs
      // the full width, so anywhere on that row it still sits on the level it names. Only when the
      // row is full does it step to the nearest free row above or below. With a single trade
      // nothing collides and every label stays exactly where it always was.
      const placed: Label[] = [];
      const overlaps = (a: Label, o: Label) =>
        a.x < o.x + o.w + LABEL_GAP && a.x + a.w + LABEL_GAP > o.x
        && a.y < o.y + LABEL_H + 1 && a.y + LABEL_H + 1 > o.y;
      const slide = (l: Label, dir: 1 | -1): Label | null => {
        let c = l;
        for (let i = 0; i < 12; i++) {
          const hit = placed.find((p) => overlaps(c, p));
          if (!hit) return c;
          const x = dir > 0 ? hit.x + hit.w + LABEL_GAP : hit.x - c.w - LABEL_GAP;
          if (dir > 0 ? x > W - RIGHT_RESERVE - c.w : x < 2) return null;
          c = { ...c, x };
        }
        return null;
      };
      const place = (l: Label): Label => {
        let c = slide(l, 1) ?? slide(l, -1);
        for (const k of [1, -1, 2, -2, 3, -3]) {
          if (c) break;
          const row = { ...l, y: clampY(l.y + k * (LABEL_H + 3)) };
          if (!placed.some((p) => overlaps(row, p))) c = row;
        }
        c ??= l;   // nowhere free: draw it where it belongs rather than drop it
        placed.push(c);
        return c;
      };
      for (const sp of specs) {
        const e = place(sp.entry);
        if (sp.chip) place({ ...sp.chip, x: Math.min(e.x + e.w + LABEL_GAP, W - sp.chip.w - 4), y: e.y });
      }
      for (const sp of specs) sp.edges.forEach(place);

      for (const l of placed) {
        const x = Math.round(l.x * hpr);
        const yy = Math.round(l.y * vpr);
        const w = Math.round(l.w * hpr);
        const h = Math.round(LABEL_H * vpr);
        g.fillStyle = l.bg;
        g.beginPath();
        roundRect(g, x, yy, w, h, 3 * hpr);
        g.fill();
        if (l.border) {
          g.strokeStyle = l.border;
          g.lineWidth = Math.max(1, Math.floor(hpr));
          g.stroke();
        }
        g.fillStyle = l.fg;
        g.fillText(l.text, x + 6 * hpr, yy + h / 2 + 0.5);
      }
    });
  }
}

// ctx.roundRect is missing from older Chromium/Safari builds; a hand-rolled path works everywhere.
export function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const rr = Math.min(r, w / 2, h / 2);
  g.moveTo(x + rr, y);
  g.lineTo(x + w - rr, y);
  g.quadraticCurveTo(x + w, y, x + w, y + rr);
  g.lineTo(x + w, y + h - rr);
  g.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  g.lineTo(x + rr, y + h);
  g.quadraticCurveTo(x, y + h, x, y + h - rr);
  g.lineTo(x, y + rr);
  g.quadraticCurveTo(x, y, x + rr, y);
  g.closePath();
}
