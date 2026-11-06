import { api } from "./api/client";
import { atrCalc, ema, macdCalc, rsiCalc, stochCalc, vwapCalc } from "./components/Chart";
import { fmtUsd } from "./format";
import { usePolling } from "./hooks/usePolling";
import { TF_SECONDS } from "./timeframes";
import type { AssetClass, Candle, PositionView, RiskState, ScalpSpec } from "./types";

// --- Scalp mode --------------------------------------------------------------------------------
// A fast, manual way to trade the 5s-10m charts. Two things hold whatever you pick:
//   - every scalp order carries a broker-side stop AND target, sized before it is sent, so a click
//     cannot open an unprotected position;
//   - the order still goes through the deterministic Risk Manager (/api/proposals/manual), so
//     RISK.md's ceiling and gates are re-checked on the backend, not trusted from here.
// "Leverage" here is the trade's size against your equity (notional / equity). Exness's ACCOUNT
// leverage (1:600 ...) only decides how much margin a position holds; it is set in the Exness
// Personal Area, and the app can read it but not change it.

export type ScalpLevel = "low" | "medium" | "high";

// Per preset: the share of equity a stopped-out trade loses, the cap on the trade's leverage however
// tight the stop, and the day's loss after which the scalp buttons lock until 00:00 UTC. All inside
// RISK.md's 3% per-trade ceiling, which the backend enforces regardless.
export const SCALP_PRESETS: Record<ScalpLevel, { label: string; risk: number; maxLev: number; dailyStop: number }> = {
  low: { label: "Low", risk: 0.005, maxLev: 10, dailyStop: 0.02 },
  medium: { label: "Medium", risk: 0.01, maxLev: 25, dailyStop: 0.04 },
  high: { label: "High", risk: 0.02, maxLev: 50, dailyStop: 0.06 },
};

export const STOP_ATR_CHOICES = [1, 1.5, 2];
export const TARGET_R_CHOICES = [1, 1.5, 2];
// Free margin a scalp may take: a position holding all of it is stopped out by the spread alone.
const MARGIN_USE = 0.9;
// Room over the spread gate's minimum stop - the spread can widen between this read and the order.
const GATE_SLACK = 1.1;

export function floorStep(x: number, step: number): number {
  if (!(step > 0)) return x;
  const dp = (String(step).split(".")[1] ?? "").length;
  return Number((Math.floor(x / step + 1e-9) * step).toFixed(dp));
}

// The closest stop the order can carry: the broker's minimum distance, and the Risk Manager's spread
// gate - it measures the spread against mid-to-stop, half a spread shorter than the fill side, and
// refuses the trade when the spread is over its share of that.
export function minScalpStop(spec: ScalpSpec, spreadGate: number | null): number {
  const floor = spec.spread + spec.stops_level;
  return spreadGate && spreadGate > 0 ? Math.max(floor, (spec.spread / spreadGate + spec.spread / 2) * GATE_SLACK) : floor;
}

// The stop distance the ATR choice gives, before any size is known (a pre-order is staged with it).
export function scalpStop(spec: ScalpSpec, atr: number | null, stopAtr: number, spreadGate: number | null): number | null {
  return atr && atr > 0 ? Math.max(stopAtr * atr, minScalpStop(spec, spreadGate)) : null;
}

export interface ScalpPlan {
  lots: number;
  leverage: number;       // the trade's notional / equity at `lots`
  maxLeverage: number;    // the slider's top: the preset's cap, its risk at this stop, free margin
  notional: number;
  margin: number | null;
  stopDist: number;       // price distance from the fill side to the stop
  minStop: number;        // the closest stop the broker and the spread gate accept
  widened: boolean;       // the ATR stop was pushed out to clear the spread gate / broker minimum
  riskUsd: number;
  riskPct: number;
  rewardUsd: number;
  rr: number;             // target distance / stop distance
  blocked: string | null; // why BUY / SELL are off
}

// The most a scalp may risk, per trade: the Risk Manager's scalp-mode ceiling (RISK.md override 2026-11-02,
// SCALP_RISK_CEILING in app/risk/service.py). Each preset's own risk is adjustable up to it.
export const SCALP_MAX_RISK = 0.06;

export function planScalp({ spec, atr, stopAtr, targetR, level, leverage, spreadGate, stopDist: userStop, targetDist, risk }: {
  spec: ScalpSpec;
  atr: number | null;
  stopAtr: number;
  targetR: number;
  level: ScalpLevel;
  leverage: number | null;     // what the slider asks for; null = the most the preset allows
  spreadGate: number | null;   // the Risk Manager's max spread / stop (0.15); null when the gate is off
  // A pre-order's own levels, as distances from the fill side. Without them the stop is the ATR
  // multiple and the target that many R.
  stopDist?: number | null;
  targetDist?: number | null;
  risk?: number | null;        // this preset's risk as set on the ticket (fraction); null = the preset's own
}): ScalpPlan {
  const preset = SCALP_PRESETS[level];
  const riskFrac = Math.min(SCALP_MAX_RISK, risk ?? preset.risk);
  const equity = spec.equity ?? 0;
  const mid = (spec.bid + spec.ask) / 2;
  const vpu = spec.value_per_unit ?? 0;
  const perLot = vpu * mid;                     // one lot's notional, in the account currency
  const none: ScalpPlan = {
    lots: 0, leverage: 0, maxLeverage: 0, notional: 0, margin: null, stopDist: 0, minStop: 0, widened: false,
    riskUsd: 0, riskPct: 0, rewardUsd: 0, rr: 0, blocked: null,
  };
  if (!(equity > 0)) return { ...none, blocked: "Account equity is $0 - deposit to scalp." };
  if (!(perLot > 0)) return { ...none, blocked: `The broker sent no contract data for ${spec.symbol}.` };
  const own = userStop != null;
  if (!own && !(atr && atr > 0)) return { ...none, blocked: "Reading the candles for the stop..." };
  if (own && !(userStop > 0)) return { ...none, blocked: "Price has crossed your stop - drag it back past the market or press Esc." };

  // The stop: yours, or the ATR multiple pushed out where the broker or the spread gate needs more
  // room (scalpStop). A stop of yours inside that minimum is shown, priced, and not sendable.
  const floor = minScalpStop(spec, spreadGate);
  const atrStop = own ? userStop : stopAtr * (atr as number);
  const stopDist = own ? userStop : Math.max(atrStop, floor);
  const tight = own && stopDist < floor;

  // Risk = leverage x stop distance / price, so at this stop the preset's risk caps the leverage.
  const levRisk = (riskFrac * mid) / stopDist;
  const levMargin = spec.margin_per_lot && spec.margin_free != null
    ? ((MARGIN_USE * spec.margin_free) / spec.margin_per_lot) * perLot / equity
    : Infinity;
  const maxLeverage = Math.max(0, Math.min(preset.maxLev, levRisk, levMargin));
  const want = Math.min(leverage ?? maxLeverage, maxLeverage);

  let lots = floorStep((want * equity) / perLot, spec.lot_step);
  if (spec.lot_max) lots = Math.min(lots, spec.lot_max);
  let blocked: string | null = null;
  if (lots < spec.lot_min) {
    // Can't trade smaller than the broker's minimum: take it when it still fits, say which limit
    // it breaks when it doesn't.
    const minLev = (spec.lot_min * perLot) / equity;
    if (minLev <= maxLeverage + 1e-9) lots = spec.lot_min;
    else if (minLev > levMargin) blocked = `Not enough free margin for the smallest lot (${spec.lot_min}).`;
    else if (minLev > preset.maxLev) blocked = `The smallest lot (${spec.lot_min}) is ${minLev.toFixed(1)}x your equity - over ${preset.label}'s ${preset.maxLev}x.`;
    else blocked = `The smallest lot (${spec.lot_min}) loses ${((minLev * stopDist) / mid * 100).toFixed(1)}% at this stop - over ${preset.label}'s ${+(riskFrac * 100).toFixed(2)}%. A higher risk or a tighter stop fits it.`;
  }
  const notional = lots * perLot;
  const riskUsd = lots * vpu * stopDist;
  const reach = targetDist ?? targetR * stopDist;
  if (!blocked && tight) {
    blocked = `Stop too close for the spread: the Risk Manager needs it at least ${floor.toFixed(spec.digits)} from your fill.`;
  }
  if (!blocked && targetDist != null && !(targetDist > 0)) {
    blocked = "Price has crossed your target - drag it back past the market or press Esc.";
  }
  return {
    lots, leverage: notional / equity, maxLeverage, notional,
    margin: spec.margin_per_lot ? lots * spec.margin_per_lot : null,
    stopDist, minStop: floor, widened: !own && stopDist > atrStop * (1 + 1e-9),
    riskUsd, riskPct: riskUsd / equity, rewardUsd: lots * vpu * Math.max(0, reach), rr: Math.max(0, reach) / stopDist,
    blocked: blocked ?? (lots > 0 ? null : "The size rounds down to zero lots."),
  };
}

// A staged order. Each level is either PINNED at a price - you dragged it there, under a swing low
// say, and it stays put while the market moves - or travels with the price as staged: the stop a
// distance from the fill (the ATR choice), the target a multiple of the stop (the R choice), so an
// untouched target keeps its R when you move the stop. Pinned, a stop staged at the spread minimum
// would slip inside it on the first tick against you and the order could not be sent.
export interface PreOrder {
  dir: "long" | "short";
  sl: { price: number } | { dist: number };
  tp: { price: number } | { r: number };
}

// A pre-order's levels as prices (in the symbol's digits; a travelling stop rounded AWAY from the
// fill, so rounding never brings it inside the distance asked for) and as distances from the side
// it would fill on right now.
export function resolvePreOrder(spec: ScalpSpec, p: PreOrder) {
  const entry = p.dir === "long" ? spec.ask : spec.bid;
  const sign = p.dir === "long" ? 1 : -1;
  const k = 10 ** spec.digits;
  const near = (v: number) => Math.round(v * k) / k;
  const sl = "price" in p.sl ? near(p.sl.price)
    : (sign > 0 ? Math.floor : Math.ceil)((entry - sign * p.sl.dist) * k + sign * 1e-6) / k;
  const stopDist = (entry - sl) * sign;
  const tp = "price" in p.tp ? near(p.tp.price) : near(entry + sign * p.tp.r * stopDist);
  return { entry, sl, tp, stopDist, targetDist: (tp - entry) * sign };
}

// What the chart draws for a staged order, worked out by the ticket.
export interface PreOrderView {
  dir: "long" | "short";
  entry: number;          // the fill side now: the ask for a buy, the bid for a sell
  sl: number;
  tp: number;
  lots: number;
  leverage: number;
  slUsd: number;          // the loss at the stop (negative)
  tpUsd: number;          // the gain at the target
  rr: number;
  bid: number;
  ask: number;
  why: string | null;     // why Enter would not send it; null = ready
  // The dollars at a level while it is being dragged, sized as the drop would size it.
  hint: (kind: "sl" | "tp", price: number) => string;
}

// The on-chart banner saying what the next key does - and, in a trade, its money (`big`).
export interface ScalpHud { tone: "long" | "short" | "close" | "idle" | "win" | "loss"; title: string; detail: string; big?: string }

// A long fills at the ask and is stopped on the bid; a short the other way round. Measured from the
// fill side, the loss at the stop is stopDist either way, the spread included.
export function scalpLevels(spec: ScalpSpec, dir: "long" | "short", stopDist: number, targetR: number) {
  const entry = dir === "long" ? spec.ask : spec.bid;
  const sign = dir === "long" ? 1 : -1;
  const round = (p: number) => Number(p.toFixed(spec.digits));
  return { entry, stop: round(entry - sign * stopDist), target: round(entry + sign * targetR * stopDist) };
}

export type ReadTone = "bull" | "bear" | "flat" | "warn";
// A reading's mini gauge: its value on a scale, the scale's marked levels, and whether it fills from
// the middle (a signed reading, -hi..+hi) or from the left.
export interface ReadMeter { lo: number; hi: number; v: number; marks?: number[]; centre?: boolean }
export interface ScalpReading { label: string; value: string; tone: ReadTone; tip: string; meter?: ReadMeter }

const signed = (x: number, dp = 1) => `${x >= 0 ? "+" : ""}${x.toFixed(dp)}`;

// Readings of the stop timeframe's candles. Each is a fact about price, not a call: none of them was
// tested as an entry signal on this book, and the momentum / regime / price-action reads that were
// tested failed (analysis/ai_reads). Colour says which side the reading leans to, nothing more.
export function scalpReadings(candles: Candle[], spread: number | null, tf: string, rsiPeriod = 7): { atr: number | null; items: ScalpReading[] } {
  const n = candles.length;
  const atr = atrCalc(candles, 14);
  if (n < 30 || !atr) return { atr, items: [] };
  const closes = candles.map((c) => c.close);
  const close = closes[n - 1];
  const items: ScalpReading[] = [];

  const e10 = ema(closes, 10);
  const e20 = ema(closes, 20);
  const f = e10[n - 1] as number, s = e20[n - 1] as number, f3 = e10[n - 4] as number;
  const up = f > s && f >= f3, down = f < s && f <= f3;
  items.push({
    label: "Trend", value: up ? "▲ up" : down ? "▼ down" : "↔ mixed", tone: up ? "bull" : down ? "bear" : "flat",
    tip: `EMA 10 against EMA 20 on ${tf}, and which way EMA 10 has moved over 3 bars.`,
  });

  const vw = vwapCalc(candles)[n - 1];
  if (vw != null) {
    const d = (close - vw) / atr;
    items.push({
      label: "VWAP", value: `${signed(d)} ATR`, tone: d > 0.25 ? "bull" : d < -0.25 ? "bear" : "flat",
      tip: "Price against the session VWAP (reset at 00:00 UTC), in ATRs. Far from it is stretched; near it is the level price keeps returning to.",
      meter: { lo: -3, hi: 3, v: d, centre: true },
    });
  }

  const r = rsiCalc(closes, rsiPeriod)[n - 1];
  if (r != null) {
    items.push({
      label: `RSI ${rsiPeriod}`, value: `${r.toFixed(0)}${r >= 80 ? " hot" : r <= 20 ? " cold" : ""}`,
      tone: r >= 80 || r <= 20 ? "warn" : r > 55 ? "bull" : r < 45 ? "bear" : "flat",
      tip: `RSI ${rsiPeriod} on ${tf} (the chart's period). Over 80 or under 20 the move is stretched - a late entry there buys the top or sells the bottom of a burst.`,
      meter: { lo: 0, hi: 100, v: r, marks: [20, 80] },
    });
  }

  const st = stochCalc(candles, 5, 3, 3);
  const k = st.k[n - 1], d = st.d[n - 1];
  if (k != null && d != null) {
    items.push({
      label: "Stoch", value: `${k.toFixed(0)}${k > d ? "↑" : k < d ? "↓" : ""}`,
      tone: k >= 80 || k <= 20 ? "warn" : k > d ? "bull" : k < d ? "bear" : "flat",
      tip: `Stochastic 5,3,3 on ${tf}: where the close sits in the last 5 bars' range (%K), and whether it is above its 3-bar average (↑) or below (↓). It turns before the RSI - quicker and noisier. Over 80 / under 20 the burst is stretched.`,
      meter: { lo: 0, hi: 100, v: k, marks: [20, 80] },
    });
  }

  const mom = (close - closes[n - 6]) / atr;
  items.push({
    label: "Move 5", value: `${signed(mom)} ATR`, tone: mom > 0.5 ? "bull" : mom < -0.5 ? "bear" : "flat",
    tip: `How far price travelled over the last 5 ${tf} bars, in ATRs.`,
    meter: { lo: -3, hi: 3, v: mom, centre: true },
  });

  // Where the close sits in the last 20 bars' range: at an edge, a break opens room and a rejection
  // turns it back - either way the edge is the level.
  const win = candles.slice(n - 20);
  const hi = Math.max(...win.map((c) => c.high)), lo = Math.min(...win.map((c) => c.low));
  if (hi > lo) {
    const pos = ((close - lo) / (hi - lo)) * 100;
    items.push({
      label: "Range 20", value: pos >= 85 ? "at high" : pos <= 15 ? "at low" : `${pos.toFixed(0)}%`,
      tone: pos >= 85 ? "bull" : pos <= 15 ? "bear" : "flat",
      tip: `Where price sits in the last 20 ${tf} bars' range: the high is ${((hi - close) / atr).toFixed(1)} ATR above, the low ${((close - lo) / atr).toFixed(1)} ATR below. At an edge a break opens room, and a rejection there turns price back.`,
      meter: { lo: 0, hi: 100, v: pos },
    });
  }

  // Bollinger width now against its own recent average: a squeeze is a market coiling.
  const widths: number[] = [];
  for (let i = Math.max(19, n - 100); i < n; i++) {
    const w = closes.slice(i - 19, i + 1);
    const mean = w.reduce((a, b) => a + b, 0) / 20;
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - mean) ** 2, 0) / 20);
    if (mean > 0) widths.push((4 * sd) / mean);
  }
  const avgW = widths.reduce((a, b) => a + b, 0) / (widths.length || 1);
  if (widths.length >= 20 && avgW > 0) {
    const x = widths[widths.length - 1] / avgW;
    items.push({
      label: "Squeeze", value: `${x.toFixed(1)}×${x < 0.7 ? " tight" : x > 1.6 ? " wide" : ""}`, tone: x < 0.7 ? "warn" : "flat",
      tip: `Bollinger band width (20, 2) on ${tf} against its average over the last ${widths.length} bars. Tight bands are a coiled market - the break out of one tends to run; wide bands mean the move is already extended.`,
      meter: { lo: 0, hi: 2.5, v: x, marks: [0.7, 1.6] },
    });
  }

  // The forming bar's volume is partial, so the last CLOSED bar is measured against the 20 before it.
  const prior = candles.slice(n - 22, n - 2).map((c) => c.volume);
  const avg = prior.reduce((a, b) => a + b, 0) / (prior.length || 1);
  if (avg > 0) {
    const x = candles[n - 2].volume / avg;
    items.push({
      label: "Activity", value: `${x.toFixed(1)}×`, tone: x >= 1.5 ? "warn" : "flat",
      tip: "Ticks in the last closed bar against the 20-bar average (MT5 gives tick counts, not traded volume). Busy bars move further; a breakout on quiet ticks is the one that fails.",
      meter: { lo: 0, hi: 3, v: x, marks: [1.5] },
    });
  }

  if (spread != null && spread > 0) {
    const pct = (spread / atr) * 100;
    items.push({
      label: "Spread", value: `${pct.toFixed(0)}% ATR`, tone: pct > 25 ? "warn" : "flat",
      tip: "The spread against one bar's average range. You pay it on every scalp, win or lose; above about a quarter of the ATR it takes most of a small target.",
      meter: { lo: 0, hi: 60, v: pct, marks: [25] },
    });
  }
  return { atr, items };
}

// --- In a trade: hold or close? -----------------------------------------------------------------
// The open trade on the charted pair, live: its money, where price sits between its stop and its
// target, the best and worst it has shown, and a checklist judged AGAINST YOUR SIDE (green always
// means "this supports holding"). The checklist is the usual scalping rules of thumb - momentum,
// trend, VWAP, a stretched RSI, profit given back, time without progress. None was backtested on
// this book, so it is a list of what to look at, not a signal; the stop and the target already close
// the trade on their own.

export type CueTone = "ok" | "warn" | "exit" | "flat";
// `short` names a warning in the headline ("WATCH · lost the VWAP"); `say` is what to do about it.
export interface TradeCue { key: string; label: string; value: string; tone: CueTone; tip: string; short?: string; say?: string }
export type VerdictTone = "hold" | "watch" | "exit" | "danger" | "target";

export interface TradeWatch {
  dir: "long" | "short";
  lots: number;
  entry: number;
  price: number;              // where it would close now: the bid for a long, the ask for a short
  pnl: number;
  r: number | null;           // the P&L in R of the risk taken at entry
  sl: number | null;
  tp: number | null;
  slUsd: number | null;       // the P&L if the stop is hit (negative while it is on the losing side)
  tpUsd: number | null;
  // Positions on the stop (0) to target (1) scale: price now, the entry, the best and worst prices.
  at: number | null;
  entryAt: number | null;
  peakAt: number | null;
  troughAt: number | null;
  peak: number;               // the best P&L this trade has shown
  trough: number;             // the worst
  secs: number | null;        // time in the trade
  cues: TradeCue[];
  verdict: { tone: VerdictTone; title: string; detail: string };
}

const tsMs = (s: string | null | undefined): number | null => {
  if (!s) return null;
  const t = Date.parse(/[Zz]|[+-]\d{2}:?\d{2}$/.test(s) ? s : `${s}Z`);
  return Number.isFinite(t) ? t : null;
};

export function fmtDur(secs: number | null): string {
  if (secs == null || !(secs >= 0)) return "—";
  const s = Math.floor(secs);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

export const fmtR = (r: number) => `${r >= 0 ? "+" : ""}${r.toFixed(2)}R`;

export function tradeWatch({ pos, spec, candles, atr, tf, seen, now = Date.now(), rsiPeriod = 7 }: {
  pos: PositionView;
  spec: ScalpSpec | null;
  candles: Candle[] | null;           // the ticket's bars (the stop timeframe's)
  atr: number | null;
  tf: string;
  seen?: { peak: number; trough: number };   // the best / worst P&L already seen by this tab
  now?: number;
  rsiPeriod?: number;                 // the scalp chart's RSI period
}): TradeWatch {
  const long = pos.direction === "long";
  const sign = long ? 1 : -1;
  const entry = pos.entry_price;
  const price = spec ? (long ? spec.bid : spec.ask) : pos.last_price ?? entry;
  // Dollars per unit of price in the trade's favour: from the live contract value, or - without it -
  // from the broker's own P&L at its last price.
  const perUnit = spec?.value_per_unit
    ? spec.value_per_unit * pos.qty
    : pos.last_price != null && pos.last_price !== entry ? pos.unrealized_pnl / (sign * (pos.last_price - entry)) : null;
  const usd = (px: number) => (perUnit != null && Number.isFinite(perUnit) ? sign * (px - entry) * perUnit : null);
  const pnl = usd(price) ?? pos.unrealized_pnl;

  // R against the stop the trade OPENED with: a stop moved since can't change what was risked.
  const stop0 = pos.initial_stop_loss ?? pos.stop_loss;
  const risk0 = stop0 != null ? sign * (entry - stop0) : null;
  const r = risk0 != null && risk0 > 0 ? (sign * (price - entry)) / risk0 : null;
  const sl = pos.stop_loss, tp = pos.take_profit;

  // The best and worst it has been: the quotes this tab saw, and the bars opened since the fill
  // (bid bars: a long closes on the bid, a short pays the spread on top).
  let peak = Math.max(seen?.peak ?? pnl, pnl);
  let trough = Math.min(seen?.trough ?? pnl, pnl);
  const opened = tsMs(pos.opened_at);
  if (candles && opened != null && perUnit != null) {
    const sp = spec?.spread ?? 0;
    for (const c of candles) {
      const t = tsMs(c.ts);
      if (t == null || t < opened) continue;
      const best = usd(long ? c.high : c.low + sp), worst = usd(long ? c.low : c.high + sp);
      if (best != null) peak = Math.max(peak, best);
      if (worst != null) trough = Math.min(trough, worst);
    }
  }

  const span = sl != null && tp != null && tp !== sl ? tp - sl : null;
  const scale = (px: number) => (span != null && sl != null ? (px - sl) / span : null);
  const priceAt = (v: number) => (perUnit ? entry + (sign * v) / perUnit : null);
  const pk = priceAt(peak), tr = priceAt(trough);
  const at = scale(price), entryAt = scale(entry);
  const secs = opened != null ? Math.max(0, (now - opened) / 1000) : null;

  const cues: TradeCue[] = [];
  const n = candles?.length ?? 0;
  if (candles && n >= 30 && atr && atr > 0) {
    const closes = candles.map((c) => c.close);
    const mine = (x: number) => sign * x;   // a move in the trade's favour is positive

    const { hist } = macdCalc(closes, 3, 10, 16);
    const h = hist[n - 1], hp = hist[n - 2];
    if (h != null && hp != null) {
      const withYou = mine(h) > 0;
      const building = Math.abs(h) > Math.abs(hp);
      cues.push({
        key: "momentum", label: "Momentum",
        value: withYou ? (building ? "building" : "fading") : "against you",
        tone: withYou ? (building ? "ok" : "flat") : "warn",
        tip: withYou
          ? building
            ? `Fast MACD (3, 10, 16) on ${tf} is on your side and still building - the reason to hold rather than bank early.`
            : `Still on your side but fading on ${tf}: this is where a scalp stalls. Many scalpers tighten or bank here.`
          : `Fast MACD on ${tf} has flipped against you - usually the first of these to turn.`,
        short: "momentum turned", say: "Momentum has flipped against you - usually the first thing to turn.",
      });
    }

    const e10 = ema(closes, 10), e20 = ema(closes, 20);
    const f = e10[n - 1] as number, s = e20[n - 1] as number, f3 = e10[n - 4] as number;
    const up = f > s && f >= f3, down = f < s && f <= f3;
    const withT = long ? up : down, againstT = long ? down : up;
    cues.push({
      key: "trend", label: "Trend", value: withT ? "with you" : againstT ? "against you" : "mixed",
      tone: withT ? "ok" : againstT ? "warn" : "flat",
      tip: `EMA 10 against EMA 20 on ${tf}, and EMA 10's slope over 3 bars. ${againstT ? "It has crossed against your side: the push that justified the trade has turned." : withT ? "Still pointing your way." : "Flat or crossing - no push either way."}`,
      short: "trend turned", say: "EMA 10 has crossed against you: the push that justified the trade has turned.",
    });

    const vw = vwapCalc(candles)[n - 1];
    if (vw != null) {
      const dv = mine(price - vw) / atr;
      cues.push({
        key: "vwap", label: "VWAP", value: Math.abs(dv) < 0.1 ? "on it" : `${signed(dv)} ATR ${dv > 0 ? "your side" : "wrong side"}`,
        tone: Math.abs(dv) < 0.1 ? "flat" : dv > 0 ? "ok" : "warn",
        tip: `Price against the session VWAP, measured your way. ${dv > 0 ? "On your side of it: the session's average buyer is with you." : "Price is on the wrong side of the VWAP for this trade - the level it has to win back."}`,
        short: "lost the VWAP", say: "Price is on the wrong side of the VWAP - the level it has to win back.",
      });
    }

    const rs = rsiCalc(closes, rsiPeriod);
    const rv = rs[n - 1], rp = rs[n - 2];
    if (rv != null) {
      const yours = long ? rv : 100 - rv;   // the RSI read from your side: high = going your way
      const toward = rp != null && mine(rv - rp) > 0;
      cues.push({
        key: "rsi", label: `RSI ${rsiPeriod}`,
        value: `${rv.toFixed(0)}${yours >= 80 ? " stretched" : yours <= 20 ? " against" : toward ? " ↑" : " ↓"}`,
        tone: yours >= 80 || yours <= 20 ? "warn" : toward ? "ok" : "flat",
        tip: yours >= 80
          ? `Stretched in your favour on ${tf}: good so far, but a burst this far usually pauses or snaps back - a common place to bank a scalp or tighten the stop.`
          : yours <= 20
            ? `Stretched against you on ${tf}: the pressure is hard the other way.`
            : `Fast RSI on ${tf}, ${toward ? "moving your way" : "moving against you"}.`,
        short: yours >= 80 ? "RSI stretched your way" : "RSI hard against you",
        say: yours >= 80
          ? "Stretched in your favour: a common place to bank a scalp or tighten the stop."
          : "The pressure is hard the other way.",
      });
    }
  }

  // Profit handed back from the best point. Only once the best point was worth something: half an R,
  // or - with no stop to measure R by - twice what the spread costs.
  const oneR = risk0 != null && risk0 > 0 && perUnit ? risk0 * perUnit : null;
  const spreadCost = spec && perUnit ? spec.spread * perUnit : 0;
  const worthIt = oneR ? peak >= 0.5 * oneR : peak > 2 * spreadCost && peak > 0;
  if (worthIt && peak > 0) {
    const gave = (peak - pnl) / peak;
    const fromWinner = oneR != null && peak >= oneR;
    cues.push({
      key: "giveback", label: "Peak", value: `${fmtUsd(peak, { sign: true })} · ${gave >= 0.5 ? `gave back ${Math.round(gave * 100)}%` : `kept ${Math.round((1 - gave) * 100)}%`}`,
      tone: gave >= 1 && fromWinner ? "exit" : gave >= 0.5 ? "warn" : "ok",
      tip: gave >= 1 && fromWinner
        ? "This trade was a full R up and has handed all of it back. Letting a winner turn into a loser is the costliest habit in scalping."
        : gave >= 0.5
          ? "More than half of the best profit is gone. Protect what is left: tighten the stop or bank it."
          : "Most of the best profit is still in the trade.",
      short: gave >= 1 && fromWinner ? "winner handed back" : `gave back ${Math.round(gave * 100)}%`,
      say: gave >= 1 && fromWinner
        ? "A full-R winner is back to nothing - letting winners turn into losers is the costliest habit in scalping."
        : "More than half of the best profit is gone - tighten the stop or bank what's left.",
    });
  }

  // Protect a winner: past +1R with the stop still on the losing side.
  if (r != null && r >= 1 && (sl == null || sign * (sl - entry) < 0)) {
    cues.push({
      key: "protect", label: "+1R", value: "stop still at risk",
      tone: "warn",
      tip: "Past +1R and the stop can still turn this into a loss. A common scalping rule: once +1R, drag the SL line on the chart to the entry (break-even).",
      short: "+1R: protect it", say: "Past +1R with the stop still at risk - drag the SL line to the entry (break-even).",
    });
  }

  // A scalp that hasn't moved after ten bars usually isn't going to.
  const bars = secs != null ? secs / (TF_SECONDS[tf] ?? 60) : null;
  if (secs != null) {
    const stuck = bars != null && bars >= 10 && (r == null || r < 0.3);
    cues.push({
      key: "time", label: "Time", value: `${fmtDur(secs)}${stuck ? " · no progress" : ""}`,
      tone: stuck ? "warn" : "flat",
      tip: stuck
        ? `${Math.floor(bars as number)} ${tf} bars in and it hasn't gone a third of an R your way. A scalp is a bet on a quick move; a time stop gets you out of the ones that don't come.`
        : `Time in the trade (${bars != null ? `${Math.floor(bars)} ${tf} bars` : "—"}).`,
      short: "no progress", say: "Ten bars without a third of an R - a time stop gets you out of the moves that don't come.",
    });
  }

  // The headline. Order matters: the missing stop first, then the target, then what has turned.
  const turned = cues.filter((c) => ["momentum", "trend", "vwap"].includes(c.key) && c.tone === "warn");
  const warns = cues.filter((c) => c.tone === "warn" || c.tone === "exit");
  // How much of the way price has gone from the entry to the target, and from the entry to the stop.
  const toTarget = at != null && entryAt != null && entryAt < 1 ? (at - entryAt) / (1 - entryAt) : null;
  const nearStop = at != null && entryAt != null && entryAt > 0 && (entryAt - at) / entryAt >= 0.8;
  const verdict: TradeWatch["verdict"] =
    sl == null
      ? { tone: "danger", title: "NO STOP", detail: "Nothing limits this loss - drag a stop onto the chart now, or close it." }
      : toTarget != null && toTarget >= 0.8 && pnl > 0
        ? { tone: "target", title: "TARGET IN SIGHT", detail: "The target closes it on its own - or bank it now." }
        : turned.length >= 2
          ? {
              tone: "exit", title: `EXIT CUES · ${turned.length} of 3 turned`,
              detail: `${turned.map((c) => c.short).join(", ")}. Closing early here is a fair call; the stop stays the backstop.`,
            }
          : cues.some((c) => c.tone === "exit")
            ? { tone: "exit", title: "EXIT CUE · winner handed back", detail: cues.find((c) => c.tone === "exit")?.say ?? "" }
            : nearStop
              ? { tone: "watch", title: "NEAR YOUR STOP", detail: "Let the stop do its job - widening it turns a planned loss into a bigger one." }
              : warns.length
                ? {
                    tone: "watch", title: `WATCH · ${warns[0].short ?? warns[0].label}`,
                    detail: `${warns[0].say ?? ""}${warns.length > 1 ? ` (+${warns.length - 1} more below)` : ""}`,
                  }
                : {
                    tone: "hold", title: "HOLD · plan intact",
                    detail: cues.length ? "Nothing has turned against you. The stop and target do the rest." : "The checklist fills in once the candles load.",
                  };

  return {
    dir: long ? "long" : "short", lots: pos.qty, entry, price, pnl, r, sl, tp,
    slUsd: sl != null ? usd(sl) : null, tpUsd: tp != null ? usd(tp) : null,
    at, entryAt, peakAt: pk != null ? scale(pk) : null, troughAt: tr != null ? scale(tr) : null,
    peak, trough, secs, cues, verdict,
  };
}

// The day's loss against the preset's daily stop. Today's realized P&L is the account's (all trades,
// the broker's deal history), measured against the equity the day started with.
// The ticket's daily stop. `pct` (of the day's starting equity) replaces the preset's own when the user
// set one; `on: false` keeps the meter but never locks (RISK.md, user's choice 2026-11-01).
export function dailyStop(risk: RiskState | null, equity: number | null, level: ScalpLevel,
                          opts: { pct?: number | null; on?: boolean } = {}) {
  if (!risk) return null;
  const base = risk.starting_equity && risk.starting_equity > 0 ? risk.starting_equity : equity ?? 0;
  const pct = opts.pct ?? SCALP_PRESETS[level].dailyStop * 100;
  const on = opts.on ?? true;
  const limit = (pct / 100) * base;
  const lost = Math.max(0, -risk.realized_pnl);
  return { limit, lost, pct, on, locked: on && lost > 0 && lost >= limit };
}

export function untilUtcMidnight(now = Date.now()): string {
  const d = new Date(now);
  const next = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
  const mins = Math.ceil((next - now) / 60000);
  return mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`;
}

const norm = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

// The broker's name for a symbol can carry a suffix the chart's doesn't (EURUSD -> EURUSDm).
export function sameSymbol(a: string, b: string): boolean {
  const x = norm(a), y = norm(b);
  return !!x && !!y && (x.startsWith(y) || y.startsWith(x));
}

// The live spec for the charted pair, every 1.5 s while scalp mode is on (paused in a hidden tab).
// A reply still for the previous pair is dropped rather than shown against the new one.
export function useScalpSpec(symbol: string, assetClass: AssetClass, enabled: boolean) {
  const { data, error } = usePolling<ScalpSpec | null>(
    () => (enabled ? api.scalpSpec(symbol, assetClass) : Promise.resolve(null)),
    1500, [symbol, assetClass, enabled],
  );
  const same = !!data && sameSymbol(data.symbol, symbol);
  return { spec: enabled && same ? data : null, error: enabled ? error : null };
}
