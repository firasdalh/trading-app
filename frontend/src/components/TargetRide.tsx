import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { winJingle } from "../celebrate";
import { fmtPrice, fmtUsd } from "../format";
import type { PositionView } from "../types";
import "./ride.css";

/**
 * Follow the target, made a show (asked 2026-11-20: "when the price breaks the target and we keep following it,
 * celebrate, and give us suspense while the price runs and we ride with it, until the final celebration").
 *
 * A strategy trade whose target the APP keeps (`follow_target`) goes through three acts on its sidebar card:
 * - WAITING for the target: the card's rail runs to that target (the broker's take profit is only a safety net
 *   further out), and the 🎯 chip starts to pulse as the price closes in - faster in the last tenth.
 * - The BREAK (`following` turns true: the stop is locked under the target and follows the 30s SuperTrend):
 *   a flash, confetti and "TARGET SMASHED!" on the card, and a short chime (the desk's 🔊 switch mutes it).
 * - RIDING: the card glows with a turning gold-to-violet edge; a banner keeps what it has made past the target
 *   and its best so far ("✨ new best" floats up on each one), and the rail now runs from the locked stop to the
 *   broker's safety target 🏁. The heartbeat - the glow and the ring on the price - quickens as the price comes
 *   back toward the stop: 🚀 flying / 🔥 riding / 😬 on the edge.
 * Its close is the last act: WinCelebration's "🚀 Rode past the target", fireworks.
 *
 * Cost: what loops is CSS on transform / opacity (ride.css) and only on a card past its target; React does
 * nothing between the book's 4 s polls. Reduced motion stops all of it.
 */

export type RidePhase = "none" | "waiting" | "riding";
export type RideMood = "flying" | "riding" | "edge";

export const ridePhase = (p: PositionView): RidePhase =>
  p.follow_target == null ? "none" : p.following ? "riding" : "waiting";

const BURST_MS = 2600;
// A new best past the target pops once it beats the last pop by this share of what the target paid (at least $1).
const POP_STEP = 0.05;
// The price's room above the stop, as a share of the entry -> target distance, that sets the mood.
const EDGE = 0.15;
const FLYING = 0.45;
const BEAT: Record<RideMood, string> = { flying: "2.2s", riding: "1.3s", edge: "0.7s" };
const MOOD: Record<RideMood, { label: string; title: string }> = {
  flying: { label: "🚀 flying", title: "Plenty of room between the price and the locked stop" },
  riding: { label: "🔥 riding", title: "The price is holding above the locked stop" },
  edge: { label: "😬 edge", title: "The price is close to the locked stop - this is where it gets tense" },
};

const keyOf = (p: PositionView) => `${p.symbol.toUpperCase()}|${p.direction}|${p.opened_at ?? ""}|${p.entry_price}`;

// Per page load, so they outlive a card that unmounts (another view) and comes back.
const sawWaiting = new Set<string>();   // seen before its target: its break is news
const celebrated = new Set<string>();   // its break already had its show
const best = new Map<string, number>(); // the best $ past the target seen
const poppedAt = new Map<string, number>();

// The scalp ticket's 🔊 switch mutes the desk's sounds (as WinCelebration reads it).
function soundOn(): boolean {
  try {
    return JSON.parse(localStorage.getItem("scalp.sound") ?? "true") !== false;
  } catch {
    return true;
  }
}

export interface Ride {
  phase: RidePhase;
  /** Non-zero while the break's show plays (it keys the show). */
  burst: number;
  /** $ made past the target now, and the best seen (riding only). */
  bonus: number | null;
  peak: number | null;
  /** Bumps on each new best: keys the "new best" pop. */
  pop: number;
  mood: RideMood;
}

/** The card's ride state. `targetUsd`: what the trade pays at the app's target. */
export function useRide(p: PositionView, targetUsd: number | null): Ride {
  const key = keyOf(p);
  const phase = ridePhase(p);
  const [burst, setBurst] = useState(0);
  const [pop, setPop] = useState(0);
  const bonus = phase === "riding" && targetUsd != null ? p.unrealized_pnl - targetUsd : null;

  useEffect(() => {
    if (phase === "waiting") sawWaiting.add(key);
    if (phase !== "riding" || celebrated.has(key)) return;
    celebrated.add(key);
    if (!sawWaiting.has(key)) return;      // already past it when the desk opened: not news
    setBurst(Date.now());
    if (soundOn()) winJingle("sparkle");
    const t = setTimeout(() => setBurst(0), BURST_MS);
    return () => clearTimeout(t);
  }, [phase, key]);

  useEffect(() => {
    if (bonus == null) return;
    const was = best.get(key);
    if (was == null) {
      best.set(key, Math.max(0, bonus));
      poppedAt.set(key, Math.max(0, bonus));
      return;
    }
    if (bonus <= was) return;
    best.set(key, bonus);
    const step = Math.max(1, Math.abs(targetUsd ?? 0) * POP_STEP);
    if (bonus - (poppedAt.get(key) ?? 0) >= step) {
      poppedAt.set(key, bonus);
      setPop((n) => n + 1);
    }
  }, [bonus, key, targetUsd]);

  let mood: RideMood = "riding";
  const t = p.follow_target, sl = p.stop_loss, last = p.last_price;
  if (phase === "riding" && t != null && sl != null && last != null && t !== p.entry_price) {
    const room = Math.abs(last - sl) / Math.abs(t - p.entry_price);
    const ourSide = p.direction === "long" ? last > sl : last < sl;
    mood = !ourSide || room < EDGE ? "edge" : room >= FLYING ? "flying" : "riding";
  }
  return { phase, burst, bonus, peak: phase === "riding" ? (best.get(key) ?? null) : null, pop, mood };
}

/** The classes and the heartbeat for the card's <li> while it rides. */
export function rideCard(ride: Ride): { className: string; style?: CSSProperties } {
  if (ride.phase !== "riding") return { className: "" };
  return { className: `ride-card ride-${ride.mood}`, style: { "--beat": BEAT[ride.mood] } as CSSProperties };
}

/** The banner on top of a riding card, and the break's show when it has just happened. */
export function RideBanner({ ride }: { ride: Ride }) {
  const mood = MOOD[ride.mood];
  return (
    <>
      <span className="ride-glow" aria-hidden="true" />
      {ride.burst > 0 && <RideBurst key={ride.burst} />}
      <div className="relative mb-1.5 flex items-center gap-2">
        <span
          className="ride-title"
          title="This trade reached the target the app keeps, with the 30s SuperTrend still its way: the stop is locked just under the target and now follows that line. It closes when the line turns (or at the broker's safety target)."
        >
          🎯 Past the target
        </span>
        <span className="ride-mood" title={mood.title}>{mood.label}</span>
        {ride.bonus != null && (
          <span className="ml-auto whitespace-nowrap text-[10px] tabular-nums text-neutral-400" title="Made past the target: the P&L now minus what the target paid, and the best it has been">
            <span className={`text-xs font-bold ${ride.bonus >= 0 ? "text-amber-300" : "text-neutral-300"}`}>
              {fmtUsd(ride.bonus, { sign: true })}
            </span>{" "}
            past 🎯
            {ride.peak != null && ride.peak > 0 && ride.peak > ride.bonus + 0.005 && (
              <span className="ml-1 text-neutral-500">· best {fmtUsd(ride.peak, { sign: true })}</span>
            )}
          </span>
        )}
        {ride.pop > 0 && ride.peak != null && (
          <span key={ride.pop} className="ride-pop" aria-hidden="true">✨ new best {fmtUsd(ride.peak, { sign: true })}</span>
        )}
      </div>
    </>
  );
}

const COLORS = ["#facc15", "#f472b6", "#a855f7", "#22d3ee", "#34d399", "#fb923c"];

function RideBurst() {
  const bits = useMemo(
    () =>
      Array.from({ length: 28 }, (_, i) => {
        const a = (i / 28) * Math.PI * 2 + Math.random() * 0.4;
        const d = 45 + Math.random() * 100;
        return {
          "--dx": `${(Math.cos(a) * d * 1.7).toFixed(1)}px`,
          "--dy": `${(Math.sin(a) * d * 0.75 - 18).toFixed(1)}px`,
          "--r": `${Math.round(Math.random() * 720 - 360)}deg`,
          "--c": COLORS[i % COLORS.length],
          "--w": `${(4 + Math.random() * 4).toFixed(1)}px`,
          "--delay": `${Math.round(Math.random() * 140)}ms`,
        } as CSSProperties;
      }),
    [],
  );
  return (
    <div className="ride-burst" aria-hidden="true">
      <div className="ride-flash" />
      {bits.map((s, i) => (
        <span key={i} className="ride-bit" style={s} />
      ))}
      <div className="ride-smash">
        🎯 TARGET SMASHED!
        <small>following it now - let's see how far it runs</small>
      </div>
    </div>
  );
}

/** A level on the rail with what the trade has there: price, $ and R (from the entry, on the risk taken at entry). */
export interface RailLevel {
  price: number | null;
  usd: number | null;
  r: number | null;
}

const rText = (r: number | null) => (r == null ? "" : ` · ${r >= 0 ? "+" : ""}${r.toFixed(1)}R`);

/**
 * Under a follow trade's rail, three levels, each with its price, then its $ and R: the stop (left), the target the
 * app keeps (🎯 gold, middle) and the broker's take profit (green, right - 2R past the app's target, so the whole
 * reach is on the card, asked 2026-11-20: "show me the full amount we could get to, and the 2R").
 */
function RailLevels({
  stop, target, tp, locked, near = "",
}: {
  stop: RailLevel;
  target: RailLevel;
  tp: RailLevel;
  /** The stop is locked past the entry (riding): 🔒, and its $ is what the trade keeps. */
  locked?: boolean;
  /** The 🎯 chip's pulse (nearClass) while the price closes in on the target. */
  near?: string;
}) {
  return (
    <div className="mt-1 grid grid-cols-3 gap-1 text-[10px] leading-tight tabular-nums">
      <div
        className={locked ? "text-neutral-400" : "text-bear"}
        title={locked ? "The stop, locked past the entry - what the trade keeps if it is hit" : "The stop - what the trade loses if it is hit"}
      >
        <div>{locked ? "🔒 " : ""}SL {fmtPrice(stop.price)}</div>
        {stop.usd != null && (
          <div className={`font-semibold ${stop.usd >= 0 ? "text-bull" : "text-bear"}`}>{fmtUsd(stop.usd, { sign: true })}</div>
        )}
      </div>
      <div
        className="text-center text-amber-300"
        title={locked
          ? "The target the app kept - broken: the stop now follows the 30s SuperTrend"
          : "The target the app keeps: there, with the 30s SuperTrend still with the trade, the stop locks just under it and the trade rides on (if the line is not with it, the trade is taken there)"}
      >
        <div>🎯 {fmtPrice(target.price)}{locked ? " ✓" : ""}</div>
        {target.usd != null && (
          <span className={`inline-block rounded bg-amber-400/15 px-1 font-bold ${near}`}>
            {fmtUsd(target.usd, { sign: true })}
            {rText(target.r)}
          </span>
        )}
      </div>
      <div className="text-right text-bull" title="The broker's take profit, 2R past the app's target: the whole way the trade can run">
        <div>{locked ? "🏁 " : ""}TP {fmtPrice(tp.price)}</div>
        {tp.usd != null && (
          <span className="inline-block rounded bg-bull/15 px-1 font-bold">
            {fmtUsd(tp.usd, { sign: true })}
            {rText(tp.r)}
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * The rail while WAITING for the app's target: the stop -> the broker's take profit as on every card (red to the
 * entry, green after it), with the app's target a gold tick on the way, a light on it in the last tenth.
 */
export function FollowRail({
  entry, last, stop, target, tp,
}: {
  entry: number;
  last: number | null;
  stop: RailLevel & { price: number };
  target: RailLevel & { price: number };
  tp: RailLevel & { price: number };
}) {
  const sl = stop.price, end = tp.price;
  const frac = (x: number) => (end === sl ? 0 : Math.min(1, Math.max(0, (x - sl) / (end - sl))));
  const e = frac(entry) * 100;
  const t = frac(target.price) * 100;
  const now = last != null ? frac(last) * 100 : null;
  const prog = last != null && target.price !== entry ? ((last - entry) / (target.price - entry)) * 100 : null;
  const near = nearClass(prog);
  return (
    <div className="mt-2">
      <div className="relative h-1.5 rounded-full">
        <div className="absolute inset-y-0 left-0 rounded-l-full bg-bear/40" style={{ width: `${e}%` }} />
        <div className="absolute inset-y-0 right-0 rounded-r-full bg-bull/40" style={{ left: `${e}%` }} />
        <div className="absolute -inset-y-0.5 w-px bg-neutral-300/70" style={{ left: `${e}%` }} title={`Entry ${fmtPrice(entry)}`} />
        <div
          className="absolute -inset-y-1 w-0.5 -translate-x-1/2 rounded bg-amber-300"
          style={{ left: `${t}%` }}
          title={`🎯 The target the app keeps: ${fmtPrice(target.price)}`}
        />
        {near === "ride-chip-hot" && <span className="ride-goal ride-goal-at" style={{ left: `${t}%` }} aria-hidden="true" />}
        {now != null && (
          <div
            className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-neutral-950 bg-white shadow"
            style={{ left: `${now}%` }}
            title={`Now ${fmtPrice(last)}`}
          />
        )}
      </div>
      <RailLevels stop={stop} target={target} tp={tp} near={near} />
    </div>
  );
}

/**
 * The rail while RIDING: from the locked stop (left) to the broker's take profit 🏁 (right), the target it broke
 * as a gold tick, the price as a glowing dot whose ring beats with the mood, the road ahead moving. Left is the
 * stop and right the finish for longs and shorts alike, as on the plain rail.
 */
export function RideRail({
  last, stop, target, tp,
}: {
  last: number | null;
  stop: RailLevel;
  target: RailLevel;
  tp: RailLevel;
}) {
  const sl = stop.price;
  if (sl == null || last == null) return null;
  // No take profit at the broker (it was removed by hand): the road runs as far again past the price.
  const end = tp.price ?? last + (last - sl);
  if (end === sl) return null;
  const frac = (x: number) => Math.min(1, Math.max(0, (x - sl) / (end - sl)));
  const now = frac(last) * 100;
  const t = target.price != null ? frac(target.price) * 100 : null;
  return (
    <div className="mt-2">
      <div className="relative h-1.5 rounded-full bg-neutral-800/80">
        <div className="ride-cushion absolute inset-y-0 left-0 rounded-l-full" style={{ width: `${now}%` }} />
        <div className="ride-road absolute inset-y-0 right-0 overflow-hidden rounded-r-full" style={{ left: `${now}%` }}>
          <i />
        </div>
        {t != null && t > 0 && t < 100 && (
          <div
            className="absolute -inset-y-1 w-0.5 -translate-x-1/2 rounded bg-amber-300/90"
            style={{ left: `${t}%` }}
            title={`The target it broke: ${fmtPrice(target.price)}`}
          />
        )}
        <div className="ride-dot" style={{ left: `${now}%` }} title={`Now ${fmtPrice(last)}`} />
      </div>
      <RailLevels stop={stop} target={target} tp={tp} locked />
    </div>
  );
}

/** The 🎯 chip's class while waiting for the app's target: it pulses as the price closes in (`prog`: % of the way). */
export function nearClass(prog: number | null): string {
  return prog == null ? "" : prog >= 90 ? "ride-chip-hot" : prog >= 70 ? "ride-chip-near" : "";
}
