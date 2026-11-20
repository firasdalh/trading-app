import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { playCelebration, winJingle, type Jingle } from "../celebrate";
import { displaySymbol, fmtUsd } from "../format";
import { fmtR } from "../scalp";
import type { PositionView } from "../types";
import { entryStopOf, usdAtLevel } from "./LiveBook";

/**
 * EVERY winning close celebrates (asked 2026-11-08: "any profit, don't need to only reach the take profit ...
 * when we close it manually or something, make some celebration ... for motivation and for vibe"). Before, only
 * a take-profit hit did. Now any trade that leaves the book in profit gets a card and a show, however it closed:
 * the target, by hand on the desk or the phone, a strategy's close rule, or a trailed stop that locked a profit.
 *
 * - How it closed picks the headline: 🎯 TARGET HIT / 💰 PROFIT TAKEN / 🤖 STRATEGY WIN / 🛡️ LOCKED IN PROFIT, and
 *   🚀 RODE PAST THE TARGET for a trade that followed past the target the app keeps (2026-11-20: the last act of
 *   its ride, TargetRide.tsx) - always fireworks.
 * - How BIG it was picks the show: a sparkle burst for a small win, gold coins or confetti (taking turns) for a
 *   solid one, fireworks from 1R. A target hit is never less than confetti.
 * - A different line of encouragement every time, the win streak when there is one, the amount counting up.
 *
 * Nothing here blocks the desk: the shows are click-through and the card takes clicks only on itself (one click
 * dismisses it), so a trade can still be managed while it plays. Watched in App, on every view.
 */

// A close counts as "at" its target / stop within this fraction of the entry->level distance: the fill on a TP
// or SL order is a few points of spread and slippage off the line.
const LEVEL_TOLERANCE = 0.1;
// The journal reads broker deal history, which can trail the close by a poll or two.
const LOOKUP_TRIES = 4;
const LOOKUP_GAP_MS = 3000;
const SHOW_MS = 6500;
const SHOW_MS_QUEUED = 3500;   // another win is waiting: make room for it

export type WinKind = "target" | "locked" | "strategy" | "manual" | "rode";
export type Show = Jingle;

export interface Win {
  key: string;
  symbol: string;
  direction: string;
  pnl: number;
  kind: WinKind;
  r: number | null;      // in R of the risk taken at entry, when that is known
  streak: number;        // wins in a row, this one included
  show: Show;
  line: string;
}

interface Snapshot {
  symbol: string;
  direction: string;
  entry: number;
  tp: number | null;
  stop: number | null;
  riskUsd: number | null;   // what its entry stop risked - kept from earlier polls once the stop trails past entry
  source: string | null;
  rode: boolean;            // it followed past the target the app keeps (kept once seen)
}

const keyOf = (p: PositionView) => `${p.id ?? ""}|${p.symbol.toUpperCase()}|${p.direction}|${p.entry_price}`;

const HEAD: Record<WinKind, { icon: string; title: string }> = {
  target: { icon: "🎯", title: "Target hit" },
  locked: { icon: "🛡️", title: "Locked in profit" },
  strategy: { icon: "🤖", title: "Strategy win" },
  manual: { icon: "💰", title: "Profit taken" },
  rode: { icon: "🚀", title: "Rode past the target" },
};

const LINES: Record<WinKind | "any", string[]> = {
  any: [
    "Clean exit. On to the next one.", "Green is the vibe. 💚", "Profit banked.", "That's how it's done.",
    "Discipline pays.", "Small wins build big accounts.", "Stay sharp, stay green.", "Paid. 💸",
    "Patience paid off.", "Another one in the bag.",
  ],
  target: ["Right on target.", "The plan, then the profit.", "Bullseye."],
  locked: ["Protected, then paid.", "The trailing stop did its job."],
  strategy: ["Your rules did the work.", "The strategy delivered.", "Rules over emotions."],
  manual: ["Good call on the exit.", "You read it right.", "Taken at the right time."],
  rode: ["Past the target - and paid more.", "Let the winner run. 🏁", "The target was only the start.", "Rode it all the way."],
};

let lastLine = "";
let lastShow: Show | null = null;

function pickLine(kind: WinKind): string {
  const pool = [...LINES[kind], ...LINES.any].filter((l) => l !== lastLine);
  lastLine = pool[Math.floor(Math.random() * pool.length)];
  return lastLine;
}

function pickShow(kind: WinKind, r: number | null, pnl: number): Show {
  // Sized by R when the entry risk is known; by dollars otherwise (a ~$1,000 account: $4 is a solid scalp).
  if (kind === "rode") return (lastShow = "fireworks");
  const big = r != null ? r >= 1 : pnl >= 20;
  const solid = r != null ? r >= 0.3 : pnl >= 4;
  const show: Show = big ? "fireworks"
    : kind === "target" ? "confetti"
      : solid ? (lastShow === "coins" ? "confetti" : lastShow === "confetti" ? "coins" : Math.random() < 0.5 ? "coins" : "confetti")
        : "sparkle";
  lastShow = show;
  return show;
}

function kindOf(s: Snapshot, t: PositionView): WinKind {
  if (s.rode) return "rode";
  const exit = t.last_price;
  const long = s.direction === "long";
  if (exit != null && s.tp != null) {
    const tol = Math.abs(s.tp - s.entry) * LEVEL_TOLERANCE;
    if (long ? exit >= s.tp - tol : exit <= s.tp + tol) return "target";
  }
  // A stop on the profit side of entry, and the exit on it: trailed, then hit.
  if (exit != null && s.stop != null && (long ? s.stop > s.entry : s.stop < s.entry)) {
    const tol = Math.max(Math.abs(s.stop - s.entry) * LEVEL_TOLERANCE, Math.abs(s.entry) * 2e-4);
    if (Math.abs(exit - s.stop) <= tol) return "locked";
  }
  return (t.source ?? s.source ?? "").startsWith("strategy") ? "strategy" : "manual";
}

/** Wins in a row, newest first - the one just closed included. */
function streakOf(closed: PositionView[]): number {
  const byTime = closed.filter((c) => c.closed_at).sort((a, b) => Date.parse(b.closed_at!) - Date.parse(a.closed_at!));
  let n = 0;
  for (const c of byTime) {
    if ((c.realized_pnl ?? 0) > 0) n++;
    else break;
  }
  return n;
}

const SHOWS: readonly Show[] = ["sparkle", "coins", "confetti", "fireworks"];

/**
 * Watches the live book for a position that LEAVES it, then asks the journal how it closed: in profit, by any
 * route, is a win. The first load is silent - trades that closed before the app opened are not news.
 */
export function useWinningCloses(positions: PositionView[] | null | undefined): [Win[], (key: string) => void] {
  // ?celebrate in the URL shows a sample (?celebrate=sparkle|coins|confetti|fireworks picks the show), so a
  // show can be seen without waiting for a real win.
  const [wins, setWins] = useState<Win[]>(() => {
    const q = new URLSearchParams(window.location.search).get("celebrate");
    if (q == null) return [];
    const show = SHOWS.find((s) => s === q) ?? (q === "rode" ? "fireworks" : "confetti");
    const kind: WinKind = q === "rode" ? "rode"
      : show === "confetti" ? "target" : show === "coins" ? "manual" : show === "sparkle" ? "strategy" : "locked";
    return [{
      key: "preview", symbol: "EURUSDm", direction: "short", pnl: show === "sparkle" ? 2.4 : 29.47, kind,
      r: show === "fireworks" ? 1.6 : show === "sparkle" ? 0.12 : 0.62, streak: 3, show, line: pickLine(kind),
    }];
  });
  const prev = useRef<Map<string, Snapshot> | null>(null);
  const announced = useRef<Set<string>>(new Set());
  // The lookup outlives the effect run that started it: the book re-polls every 4s (a new array, so a new run),
  // and its retries take longer than that. Only unmounting stops it.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!positions) return;
    const before = prev.current;
    const now = new Map<string, Snapshot>();
    for (const p of positions) {
      const k = keyOf(p);
      const at = usdAtLevel(p, entryStopOf(p) ?? p.stop_loss);
      now.set(k, {
        symbol: p.symbol, direction: p.direction, entry: p.entry_price, tp: p.take_profit, stop: p.stop_loss,
        riskUsd: (at != null && at < 0 ? -at : null) ?? before?.get(k)?.riskUsd ?? null,
        source: p.source,
        rode: !!p.following || !!before?.get(k)?.rode,
      });
    }
    // Back in the book (a broker read that briefly came back empty): watch it again.
    for (const k of now.keys()) announced.current.delete(k);
    prev.current = now;
    if (!before) return;
    const gone = [...before.entries()].filter(([k]) => !now.has(k) && !announced.current.has(k));
    if (!gone.length) return;

    for (const [key] of gone) announced.current.add(key);   // looked up once, whatever it finds
    void (async () => {
      for (const [key, s] of gone) {
        for (let i = 0; i < LOOKUP_TRIES && alive.current; i++) {
          if (i) await new Promise((r) => setTimeout(r, LOOKUP_GAP_MS));
          let closed: PositionView[];
          try {
            closed = await api.journalTrades(30);
          } catch {
            continue;
          }
          const t = closed.find(
            (c) =>
              c.symbol.toUpperCase() === s.symbol.toUpperCase() &&
              c.direction === s.direction &&
              Math.abs(c.entry_price - s.entry) <= Math.abs(s.entry) * 1e-3 &&
              c.closed_at != null &&
              Date.now() - Date.parse(c.closed_at) < 15 * 60_000,
          );
          if (!t) continue;
          const pnl = t.realized_pnl ?? 0;
          if (pnl > 0 && alive.current) {
            const kind = kindOf(s, t);
            const r = s.riskUsd ? pnl / s.riskUsd : null;
            setWins((w) => [...w, {
              key, symbol: s.symbol, direction: s.direction, pnl, kind, r, streak: streakOf(closed),
              show: pickShow(kind, r, pnl), line: pickLine(kind),
            }]);
          }
          break;
        }
      }
    })();
  }, [positions]);

  const dismiss = (key: string) => setWins((w) => w.filter((x) => x.key !== key));
  return [wins, dismiss];
}

const reduced = () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// The scalp ticket's 🔊 switch mutes these too: one switch for the desk's sounds.
function soundOn(): boolean {
  try {
    return JSON.parse(localStorage.getItem("scalp.sound") ?? "true") !== false;
  } catch {
    return true;
  }
}

/**
 * The celebration: the show over the whole screen and a card with the pair, what it paid and a line to go on
 * with. Portalled into the full-screen element when the chart is full screen, since nothing outside it is
 * visible there. Click the card (or wait) to dismiss. No show under reduced motion - the card still comes.
 */
export function WinCelebration({ win, queued, onDone }: { win: Win | null; queued: number; onDone: (key: string) => void }) {
  useEffect(() => {
    if (!win) return;
    // An ElevenLabs clip for this amount if there are any; otherwise the show's own jingle. Either is heard
    // once per win: the ticket that saw the same close may have played it already.
    if (soundOn()) void playCelebration(win.pnl, { target: win.kind === "target" || win.kind === "rode" }).then((ok) => { if (!ok) winJingle(win.show); });
    const t = setTimeout(() => onDone(win.key), queued > 0 ? SHOW_MS_QUEUED : SHOW_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [win?.key]);

  if (!win) return null;
  const head = HEAD[win.kind];
  const long = win.direction === "long";
  const gold = win.kind === "target" || win.kind === "rode";
  const host = (document.fullscreenElement as HTMLElement | null) ?? document.body;
  return createPortal(
    <div className="pointer-events-none fixed inset-0 z-[100] flex items-start justify-center pt-[30vh]">
      <ShowCanvas key={`${win.key}:show`} show={win.show} gold={gold} />
      <div
        // A new card per win: it pops in again, and its amount counts up from zero rather than from the last one's.
        key={`${win.key}:card`}
        role="status"
        onClick={() => onDone(win.key)}
        title="Click to dismiss"
        className="win-card pointer-events-auto relative min-w-[16rem] cursor-pointer rounded-2xl border bg-neutral-950/90 px-8 py-5 text-center backdrop-blur"
        style={{ "--win-c": gold ? "250,204,21" : "34,197,94" } as CSSProperties}
      >
        <div className="text-4xl">{head.icon}</div>
        <div className={`mt-1 text-xs font-semibold uppercase tracking-[0.2em] ${gold ? "text-amber-300" : "text-bull"}`}>{head.title}</div>
        <div className="mt-1 text-lg font-semibold text-neutral-100">
          {displaySymbol(win.symbol)}{" "}
          <span className={long ? "text-bull" : "text-bear"}>{long ? "▲ LONG" : "▼ SHORT"}</span>
        </div>
        <div className="mt-1 flex items-baseline justify-center gap-2">
          <CountUp value={win.pnl} className="text-3xl font-bold tabular-nums text-bull" />
          {win.r != null && <span className="text-sm font-semibold tabular-nums text-bull/80">{fmtR(win.r)}</span>}
        </div>
        <div className="mt-2 text-sm text-neutral-300">{win.line}</div>
        {win.streak >= 2 && <div className="mt-1 text-xs font-semibold text-amber-300">🔥 {win.streak} wins in a row</div>}
        {queued > 0 && <div className="mt-1 text-[10px] text-neutral-500">+{queued} more win{queued > 1 ? "s" : ""}</div>}
      </div>
      <style>{CARD_CSS}</style>
    </div>,
    host,
  );
}

const CARD_CSS = `
@keyframes win-pop { 0% { transform: scale(.6) translateY(14px); opacity: 0 } 60% { transform: scale(1.06); opacity: 1 } 100% { transform: scale(1) } }
@keyframes win-glow {
  0%, 100% { box-shadow: 0 0 0 1px rgba(var(--win-c), .35), 0 22px 50px -14px rgba(var(--win-c), .35) }
  50% { box-shadow: 0 0 0 1px rgba(var(--win-c), .65), 0 22px 74px -8px rgba(var(--win-c), .6) }
}
.win-card { border-color: rgba(var(--win-c), .5); animation: win-pop .5s cubic-bezier(.2,1.4,.4,1) both, win-glow 2.4s ease-in-out .5s infinite; }
@media (prefers-reduced-motion: reduce) { .win-card { animation: none } }
`;

/** The amount, counting up from zero - it lands, rather than just appearing. */
function CountUp({ value, className }: { value: number; className?: string }) {
  const [v, setV] = useState(() => (reduced() ? value : 0));
  useEffect(() => {
    if (reduced()) {
      setV(value);
      return;
    }
    const start = performance.now();
    let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / 900);
      setV(value * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return <span className={className}>{fmtUsd(v, { sign: true })}</span>;
}

// ------------------------------------------------------------------------------------------------------------
// The shows: one full-screen, click-through canvas each. A painter draws one frame and says whether it has
// more; `k` is the frame's length in 60fps frames, so a 144Hz screen doesn't play them twice as fast.
// ------------------------------------------------------------------------------------------------------------

type Painter = (g: CanvasRenderingContext2D, age: number, k: number) => boolean;

function ShowCanvas({ show, gold }: { show: Show; gold: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (reduced()) return;
    const c = ref.current;
    const g = c?.getContext("2d");
    if (!c || !g) return;
    const dpr = window.devicePixelRatio || 1;
    const w = window.innerWidth;
    const h = window.innerHeight;
    c.width = w * dpr;
    c.height = h * dpr;
    g.scale(dpr, dpr);
    const paint = show === "coins" ? coins(w, h) : show === "fireworks" ? fireworks(w, h)
      : show === "sparkle" ? sparkle(w, h) : confetti(w, h, gold);
    const start = performance.now();
    let last = start;
    let raf = 0;
    const tick = (t: number) => {
      // A frame's timestamp is when the frame BEGAN, which can be earlier than `start` was read (seen in a
      // busy page: -0.35 s, and a negative radius threw inside the sparkle) - so neither goes below zero.
      const k = Math.max(0, Math.min(3, (t - last) / (1000 / 60)));
      last = Math.max(last, t);
      if (paint(g, Math.max(0, (t - start) / 1000), k)) raf = requestAnimationFrame(tick);
      else g.clearRect(0, 0, w, h);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [show, gold]);
  return <canvas ref={ref} className="pointer-events-none fixed inset-0 h-full w-full" />;
}

const PARTY = ["#22c55e", "#4ade80", "#facc15", "#38bdf8", "#a78bfa", "#f472b6", "#ffffff"];
const GOLDEN = ["#facc15", "#fde68a", "#f59e0b", "#ffffff", "#22c55e", "#4ade80"];
const pick = <T,>(xs: readonly T[]): T => xs[Math.floor(Math.random() * xs.length)];

/** Confetti: ~170 pieces burst from the top, fall with drag and spin. Gold-heavy for a target. */
function confetti(w: number, h: number, gold: boolean): Painter {
  const colors = gold ? GOLDEN : PARTY;
  const parts = Array.from({ length: 170 }, () => ({
    x: w / 2 + (Math.random() - 0.5) * w * 0.3, y: h * 0.25,
    vx: (Math.random() - 0.5) * 16, vy: -Math.random() * 14 - 4,
    size: 5 + Math.random() * 6, rot: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.4, color: pick(colors),
  }));
  return (g, age, k) => {
    g.clearRect(0, 0, w, h);
    g.globalAlpha = Math.max(0, 1 - Math.max(0, age - 2.8) / 1.2);
    const drag = Math.pow(0.985, k);
    for (const p of parts) {
      p.vy += 0.35 * k;
      p.vx *= drag;
      p.vy *= drag;
      p.x += p.vx * k;
      p.y += p.vy * k;
      p.rot += p.vr * k;
      g.save();
      g.translate(p.x, p.y);
      g.rotate(p.rot);
      g.fillStyle = p.color;
      g.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2);
      g.restore();
    }
    g.globalAlpha = 1;
    return age < 4;
  };
}

/** Gold coins raining across the whole screen, spinning as they fall. */
function coins(w: number, h: number): Painter {
  const parts = Array.from({ length: 48 }, () => ({
    x: Math.random() * w, y: -20 - Math.random() * h * 0.7,
    vx: (Math.random() - 0.5) * 1.2, vy: 2 + Math.random() * 3,
    r: 9 + Math.random() * 7, spin: Math.random() * Math.PI * 2, vs: 0.12 + Math.random() * 0.12,
  }));
  return (g, age, k) => {
    g.clearRect(0, 0, w, h);
    g.globalAlpha = Math.max(0, 1 - Math.max(0, age - 3.2) / 0.8);
    g.textAlign = "center";
    g.textBaseline = "middle";
    for (const p of parts) {
      p.vy = Math.min(14, p.vy + 0.22 * k);
      p.x += p.vx * k;
      p.y += p.vy * k;
      p.spin += p.vs * k;
      if (p.y - p.r > h) continue;
      const face = Math.abs(Math.cos(p.spin));
      const rx = Math.max(p.r * 0.14, p.r * face);
      const shine = g.createRadialGradient(p.x - rx * 0.3, p.y - p.r * 0.35, p.r * 0.1, p.x, p.y, p.r);
      shine.addColorStop(0, "#fff7cc");
      shine.addColorStop(0.35, "#fcd34d");
      shine.addColorStop(0.8, "#f59e0b");
      shine.addColorStop(1, "#b45309");
      g.beginPath();
      g.ellipse(p.x, p.y, rx, p.r, 0, 0, Math.PI * 2);
      g.fillStyle = shine;
      g.fill();
      g.lineWidth = 1.2;
      g.strokeStyle = "rgba(120,53,15,0.85)";
      g.stroke();
      if (face > 0.55) {
        g.save();
        g.translate(p.x, p.y);
        g.scale(face, 1);
        g.fillStyle = "rgba(146,64,14,0.9)";
        g.font = `bold ${Math.round(p.r * 1.1)}px system-ui, sans-serif`;
        g.fillText("$", 0, 1);
        g.restore();
      }
    }
    g.globalAlpha = 1;
    return age < 4;
  };
}

/** A small win: a ring and a burst of sparkles out of the card. */
function sparkle(w: number, h: number): Painter {
  const cx = w / 2;
  const cy = h * 0.3 + 95;   // about the card's middle
  const colors = ["#4ade80", "#22c55e", "#fde68a", "#facc15", "#ffffff"];
  const parts = Array.from({ length: 70 }, () => {
    const a = Math.random() * Math.PI * 2;
    const s = 3 + Math.random() * 7;
    return { x: cx, y: cy, vx: Math.cos(a) * s, vy: Math.sin(a) * s, life: 1.1 + Math.random() * 0.8, size: 1.5 + Math.random() * 2.5, color: pick(colors) };
  });
  return (g, age, k) => {
    g.clearRect(0, 0, w, h);
    g.save();
    g.globalCompositeOperation = "lighter";
    if (age < 0.9) {
      const t = age / 0.9;
      g.beginPath();
      g.arc(cx, cy, 40 + t * 260, 0, Math.PI * 2);
      g.strokeStyle = `rgba(74,222,128,${0.5 * (1 - t)})`;
      g.lineWidth = 3 * (1 - t) + 0.5;
      g.stroke();
    }
    const drag = Math.pow(0.94, k);
    for (const p of parts) {
      const left = 1 - age / p.life;
      if (left <= 0) continue;
      p.vx *= drag;
      p.vy = p.vy * drag + 0.04 * k;
      p.x += p.vx * k;
      p.y += p.vy * k;
      const r = p.size * (0.6 + left);
      g.globalAlpha = left;
      g.fillStyle = p.color;
      g.beginPath();   // a four-point twinkle
      g.moveTo(p.x, p.y - r * 2);
      g.quadraticCurveTo(p.x, p.y, p.x + r * 2, p.y);
      g.quadraticCurveTo(p.x, p.y, p.x, p.y + r * 2);
      g.quadraticCurveTo(p.x, p.y, p.x - r * 2, p.y);
      g.quadraticCurveTo(p.x, p.y, p.x, p.y - r * 2);
      g.fill();
    }
    g.restore();
    return age < 2;
  };
}

/** A big win (1R and up): rockets from the bottom bursting into colour, with fading trails. */
function fireworks(w: number, h: number): Painter {
  const hues = ["#4ade80", "#facc15", "#38bdf8", "#a78bfa", "#f472b6", "#fb923c"];
  const rockets = Array.from({ length: 6 }, (_, i) => ({
    at: i * 0.32 + Math.random() * 0.15, x: w * (0.15 + Math.random() * 0.7), y: h + 10,
    ty: h * (0.14 + Math.random() * 0.26), color: pick(hues), burst: false,
  }));
  const sparks: { x: number; y: number; vx: number; vy: number; life: number; born: number; color: string; size: number }[] = [];
  return (g, age, k) => {
    g.save();
    // Last frame's light dims instead of vanishing: the trails.
    g.globalCompositeOperation = "destination-out";
    g.fillStyle = `rgba(0,0,0,${Math.min(1, 0.22 * k)})`;
    g.fillRect(0, 0, w, h);
    g.globalCompositeOperation = "lighter";
    for (const r of rockets) {
      if (age < r.at || r.burst) continue;
      r.y -= Math.min(26, Math.max(2.5, (r.y - r.ty) * 0.08)) * k;   // shoots up, slows to its height
      g.globalAlpha = 1;
      g.fillStyle = "#fff7e0";
      g.beginPath();
      g.arc(r.x, r.y, 2.2, 0, Math.PI * 2);
      g.fill();
      if (r.y <= r.ty + 4) {
        r.burst = true;
        for (let i = 0; i < 80; i++) {
          const a = (i / 80) * Math.PI * 2 + Math.random() * 0.1;
          const s = 2 + Math.random() * 4.5;
          sparks.push({
            x: r.x, y: r.y, vx: Math.cos(a) * s, vy: Math.sin(a) * s, life: 1.3 + Math.random() * 0.8, born: age,
            color: Math.random() < 0.15 ? "#ffffff" : r.color, size: 1.4 + Math.random() * 1.4,
          });
        }
      }
    }
    const drag = Math.pow(0.975, k);
    for (const s of sparks) {
      const t = (age - s.born) / s.life;
      if (t >= 1) continue;
      s.vx *= drag;
      s.vy = s.vy * drag + 0.05 * k;
      s.x += s.vx * k;
      s.y += s.vy * k;
      g.globalAlpha = 1 - t;
      g.fillStyle = s.color;
      g.beginPath();
      g.arc(s.x, s.y, s.size, 0, Math.PI * 2);
      g.fill();
    }
    g.restore();
    return age < 4.6;
  };
}
