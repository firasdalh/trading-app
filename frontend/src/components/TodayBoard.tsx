import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import type { AssetClass, Candle } from "../types";

/**
 * "Today" (asked 2026-11-20: "a small panel with our 12 pairs, a colour per pair showing up or down from today's open
 * and how big that move is against the usual - the same number as the Noise. Display only"). DISPLAY ONLY: nothing
 * here reaches the engine.
 *
 * Each strategy pair: its move since today's open (the first candle of the UTC day, as the learner reads it) and that
 * move over the move it usually has made from its open by this minute - the average |price / open - 1| at the same
 * minute over the 14 earlier days with candles (up to 30 days back) - i.e. the learner's noise-area reading
 * (backend learning.noise_of) for a buy; a sell reads it with the sign flipped. The learner reads 1m candles; this
 * reads 30m ones (1000 reach 14 days on every pair) and takes a day's price at a minute inside a 30m candle between
 * the candle before's close and its own. Checked on the 400 trades of strategy 1 (2026-11-20): same bucket
 * (<0.5 / 0.5-1 / 1+) 96%, median gap 0.09.
 *
 * Load: one 30m read per pair every 10 minutes and one 1m read per pair a minute, one after another - never a burst
 * on the single MT5 connection - and none while the window is hidden.
 */

const HIST_MS = 10 * 60_000;
const PRICE_MS = 60_000;
const DAYS = 14;
const LOOKBACK = 30;
const DAY_MS = 86_400_000;

interface Day {
  open: number;
  starts: number[];   // minute of the UTC day each 30m candle opened
  closes: number[];
}
interface Hist {
  days: Map<number, Day>;
  at: number;
}
interface Pair {
  symbol: string;
  asset_class: AssetClass;
}

const short = (s: string) => s.replace(/m$/i, "");

function daysOf(candles: Candle[]): Map<number, Day> {
  const out = new Map<number, Day>();
  for (const c of candles) {
    const t = Date.parse(c.ts);
    if (!Number.isFinite(t)) continue;
    const d = Math.floor(t / DAY_MS);
    let day = out.get(d);
    if (!day) {
      day = { open: c.open, starts: [], closes: [] };
      out.set(d, day);
    }
    day.starts.push(Math.floor((t - d * DAY_MS) / 60_000));
    day.closes.push(c.close);
  }
  return out;
}

/** A day's price at minute `m`: inside the 30m candle that opened by then, between the close before and its own. */
function priceAt(day: Day, m: number): number | null {
  let j = -1;
  for (let i = 0; i < day.starts.length && day.starts[i] <= m; i++) j = i;
  if (j < 0) return null;
  const prev = j > 0 ? day.closes[j - 1] : day.open;
  const f = Math.min(1, (m - day.starts[j] + 1) / 30);
  return prev + (day.closes[j] - prev) * f;
}

export interface TodayRead {
  pct: number;          // % from today's open
  noise: number | null; // that move over the usual one by now (signed: + up, - down)
  open: number;
  price: number;
}

export function todayRead(h: Hist, price: number | null, now: number): TodayRead | null {
  const d = Math.floor(now / DAY_MS);
  const today = h.days.get(d);
  const m = Math.floor((now - d * DAY_MS) / 60_000);
  if (!today || !today.open || price == null || today.starts[0] > m) return null;
  const moves: number[] = [];
  for (let k = 1; k <= LOOKBACK && moves.length < DAYS; k++) {
    const dd = h.days.get(d - k);
    if (!dd || !dd.open) continue;
    const p = priceAt(dd, m);
    if (p != null) moves.push(Math.abs(p / dd.open - 1));
  }
  const move = price / today.open - 1;
  const usual = moves.length >= DAYS ? moves.reduce((a, b) => a + b, 0) / moves.length : 0;
  return { pct: move * 100, noise: usual > 0 ? move / usual : null, open: today.open, price };
}

function zone(n: number): string {
  const a = Math.abs(n);
  if (a < 0.5) return "a quiet day so far";
  if (a < 1) return "inside its usual move (the noise area)";
  if (a < 2) return "outside the noise area - a real move";
  return "a big day - more than twice its usual move";
}

function tone(r: TodayRead | null): string {
  if (!r || r.noise == null) return "border-neutral-800 bg-neutral-900/40";
  const up = r.noise >= 0;
  const a = Math.abs(r.noise);
  if (a < 0.5) return up ? "border-neutral-700/70 bg-bull/[0.04]" : "border-neutral-700/70 bg-bear/[0.04]";
  if (a < 1) return up ? "border-bull/25 bg-bull/10" : "border-bear/25 bg-bear/10";
  if (a < 2) return up ? "border-bull/50 bg-bull/20" : "border-bear/50 bg-bear/20";
  return up ? "border-bull/70 bg-bull/30" : "border-bear/70 bg-bear/30";
}

export function TodayBoard({ chartSymbol, onPick }: {
  chartSymbol?: string;
  onPick?: (symbol: string, assetClass: AssetClass) => void;
}) {
  const { data: strategies } = usePolling(() => api.strategies(), 60_000, []);
  const pairs = useMemo<Pair[]>(() => {
    const seen = new Set<string>();
    const out: Pair[] = [];
    for (const s of strategies ?? []) {
      for (const p of s.pairs ?? []) {
        if (seen.has(p.symbol)) continue;
        seen.add(p.symbol);
        out.push({ symbol: p.symbol, asset_class: p.asset_class as AssetClass });
      }
    }
    return out;
  }, [strategies]);
  const pairsKey = pairs.map((p) => p.symbol).join(",");

  const hist = useRef<Record<string, Hist>>({});
  const prices = useRef<Record<string, number>>({});
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!pairs.length) return;
    let cancelled = false;
    let first = true;
    const pass = async () => {
      if (!first && document.visibilityState === "hidden") return;
      first = false;
      for (const p of pairs) {
        if (cancelled) return;
        try {
          const h = hist.current[p.symbol];
          if (!h || Date.now() - h.at > HIST_MS) {
            const s = await api.ohlcv(p.symbol, p.asset_class, "30m", 1000);
            const last = s.candles[s.candles.length - 1];
            hist.current[p.symbol] = { days: daysOf(s.candles), at: Date.now() };
            if (last) prices.current[p.symbol] = last.close;
          } else {
            const s = await api.ohlcv(p.symbol, p.asset_class, "1m", 1);
            const last = s.candles[s.candles.length - 1];
            if (last) prices.current[p.symbol] = last.close;
          }
        } catch {
          /* one pair not readable (market closed, broker hiccup): it keeps its last read */
        }
      }
      if (!cancelled) setTick((v) => v + 1);
    };
    pass();
    const id = setInterval(pass, PRICE_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairsKey]);

  const now = Date.now();
  const reads = pairs.map((p) => {
    const h = hist.current[p.symbol];
    return { p, r: h ? todayRead(h, prices.current[p.symbol] ?? null, now) : null, loaded: !!h };
  });
  const up = reads.filter((x) => x.r && x.r.pct > 0).length;
  const down = reads.filter((x) => x.r && x.r.pct < 0).length;
  const hhmm = new Date(now).toISOString().slice(11, 16);

  if (!pairs.length) {
    return <div className="px-3 py-2.5 text-xs text-neutral-500">No strategy pairs yet.</div>;
  }
  return (
    <div className="px-2 pb-2">
      <div className="mb-1 flex items-center justify-between px-0.5 text-[10px] text-neutral-500">
        <span title="Pairs up / down from today's open (00:00 UTC)">
          <span className="font-semibold text-bull">▲{up}</span> <span className="font-semibold text-bear">▼{down}</span>
          <span className="ml-1">since 00:00 UTC</span>
        </span>
        <span title="The × number: today's move from the open over the move the pair usually has made by this time of day (average of the 14 earlier days) - the learner's noise reading. 1× or more = outside the noise area.">
          × = vs its usual move by {hhmm}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-1">
        {reads.map(({ p, r, loaded }) => {
          const name = short(p.symbol);
          const isUp = !!r && r.pct >= 0;
          const title = !r
            ? `${name}: ${loaded ? "no candle yet today (market closed?)" : "reading…"}`
            : `${name}: ${isUp ? "up" : "down"} ${Math.abs(r.pct).toFixed(2)}% since today's open (00:00 UTC). ` +
              (r.noise == null
                ? "Not enough earlier days to say what is usual."
                : `That is ${Math.abs(r.noise).toFixed(1)}× the move it usually makes by ${hhmm} UTC (average of the ` +
                  `last 14 days): ${zone(r.noise)}. The learner's noise reading for a ${isUp ? "buy" : "sell"} ` +
                  "(from 30m candles, about its 1m number).") +
              " Display only - click to chart it.";
          return (
            <button
              key={p.symbol}
              type="button"
              onClick={() => onPick?.(p.symbol, p.asset_class)}
              title={title}
              className={`min-w-0 rounded-md border px-1.5 py-1 text-left transition hover:brightness-125 ${tone(r)} ${
                chartSymbol === p.symbol ? "ring-1 ring-brand-500/70" : ""
              }`}
            >
              <div className="flex items-center justify-between gap-1 text-[11px] font-semibold text-neutral-100">
                <span className="truncate">{name}</span>
                <span className={r ? (isUp ? "text-bull" : "text-bear") : "text-neutral-600"}>
                  {r ? (isUp ? "▲" : "▼") : "·"}
                </span>
              </div>
              <div className="flex items-baseline justify-between gap-1 text-[10px] tabular-nums">
                <span className={r ? "text-neutral-300" : "text-neutral-600"}>
                  {r ? `${r.pct >= 0 ? "+" : ""}${r.pct.toFixed(2)}%` : "—"}
                </span>
                <span className="font-semibold text-neutral-100">
                  {r && r.noise != null ? `${Math.abs(r.noise).toFixed(1)}×` : ""}
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
