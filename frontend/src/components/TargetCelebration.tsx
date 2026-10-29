import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { displaySymbol, fmtUsd } from "../format";
import { play, type Note } from "../hooks/useApprovalChime";
import type { PositionView } from "../types";
import { usdAtLevel } from "./LiveBook";

// A rising C-major arpeggio: unmistakably "good news", and different from the approval chime.
const FANFARE: Note[] = [
  { hz: 523.25, at: 0.0, len: 0.12 },   // C5
  { hz: 659.25, at: 0.1, len: 0.12 },   // E5
  { hz: 783.99, at: 0.2, len: 0.12 },   // G5
  { hz: 1046.5, at: 0.3, len: 0.4 },    // C6
];
// A close counts as "at the target" within this fraction of the entry->target distance - the fill
// on a TP order is a few points of spread/slippage off the line.
const TP_TOLERANCE = 0.1;
// The journal reads broker deal history, which can trail the close by a poll or two.
const LOOKUP_TRIES = 4;
const LOOKUP_GAP_MS = 3000;
const SHOW_MS = 7000;

export interface TargetHit {
  key: string;
  symbol: string;
  direction: string;
  pnl: number;
}

interface Snapshot {
  symbol: string;
  direction: string;
  entry: number;
  tp: number;
  tpUsd: number | null;
}

const keyOf = (p: PositionView) => `${p.id ?? ""}|${p.symbol.toUpperCase()}|${p.direction}|${p.entry_price}`;

/**
 * Watches the live book for a position that LEAVES it, then asks the journal how it closed: at or
 * beyond its take-profit with a profit is a target hit. Judged on the broker's own exit price, so a
 * trade stopped out or closed by hand halfway never counts. The first load is silent - trades that
 * closed before you opened the app are not news.
 */
export function useTargetHits(positions: PositionView[] | null | undefined): [TargetHit[], (key: string) => void] {
  // ?celebrate in the URL plays a sample, so the effect can be seen without waiting for a real target.
  const [hits, setHits] = useState<TargetHit[]>(() =>
    new URLSearchParams(window.location.search).has("celebrate")
      ? [{ key: "preview", symbol: "EURUSDm", direction: "short", pnl: 29.47 }]
      : [],
  );
  const prev = useRef<Map<string, Snapshot> | null>(null);
  const announced = useRef<Set<string>>(new Set());
  // The lookup outlives the effect run that started it: the book re-polls every 4s (a new array, so
  // a new run), and its retries take longer than that. Only unmounting stops it.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!positions) return;
    const now = new Map<string, Snapshot>();
    for (const p of positions) {
      if (p.take_profit == null) continue;
      now.set(keyOf(p), {
        symbol: p.symbol, direction: p.direction, entry: p.entry_price, tp: p.take_profit,
        tpUsd: usdAtLevel(p, p.take_profit),
      });
    }
    // Back in the book (a broker read that briefly came back empty): watch it again.
    for (const k of now.keys()) announced.current.delete(k);
    const before = prev.current;
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
            closed = await api.journalTrades(20);
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
          const exit = t.last_price;
          const tol = Math.abs(s.tp - s.entry) * TP_TOLERANCE;
          const atTarget =
            exit != null && (s.direction === "long" ? exit >= s.tp - tol : exit <= s.tp + tol);
          if (atTarget && (t.realized_pnl ?? 0) > 0 && alive.current) {
            setHits((h) => [...h, { key, symbol: s.symbol, direction: s.direction, pnl: t.realized_pnl ?? s.tpUsd ?? 0 }]);
          }
          break;
        }
      }
    })();
  }, [positions]);

  const dismiss = (key: string) => setHits((h) => h.filter((x) => x.key !== key));
  return [hits, dismiss];
}

/**
 * The celebration: confetti over the whole screen and a card with the pair and what it paid. Portalled
 * into the full-screen element when the chart is full screen, since nothing outside it is visible
 * there. Click (or wait) to dismiss. No confetti under prefers-reduced-motion - the card still shows.
 */
export function TargetCelebration({ hit, onDone }: { hit: TargetHit | null; onDone: (key: string) => void }) {
  useEffect(() => {
    if (!hit) return;
    play(FANFARE, 0.16);
    const t = setTimeout(() => onDone(hit.key), SHOW_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hit?.key]);

  if (!hit) return null;
  const host = (document.fullscreenElement as HTMLElement | null) ?? document.body;
  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-start justify-center pt-[18vh]" onClick={() => onDone(hit.key)}>
      <Confetti key={hit.key} />
      <div
        role="status"
        className="relative animate-[pop_0.45s_cubic-bezier(0.2,1.4,0.4,1)] cursor-pointer rounded-2xl border border-bull/50 bg-neutral-950/90 px-8 py-5 text-center shadow-2xl shadow-bull/20 backdrop-blur"
        title="Click to dismiss"
      >
        <div className="text-4xl">🎯</div>
        <div className="mt-1 text-xs font-semibold uppercase tracking-[0.2em] text-bull">Target hit</div>
        <div className="mt-1 text-lg font-semibold text-neutral-100">
          {displaySymbol(hit.symbol)}{" "}
          <span className={hit.direction === "long" ? "text-bull" : "text-bear"}>
            {hit.direction === "long" ? "▲ LONG" : "▼ SHORT"}
          </span>
        </div>
        <div className="mt-1 text-3xl font-bold tabular-nums text-bull">{fmtUsd(hit.pnl, { sign: true })}</div>
      </div>
      <style>{"@keyframes pop{0%{transform:scale(.6);opacity:0}100%{transform:scale(1);opacity:1}}"}</style>
    </div>,
    host,
  );
}

const COLORS = ["#22c55e", "#4ade80", "#facc15", "#38bdf8", "#a78bfa", "#f472b6", "#ffffff"];

/** Canvas confetti: ~160 pieces burst from the top, fall with drag and spin, gone in ~4s. */
function Confetti() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const c = ref.current;
    const g = c?.getContext("2d");
    if (!c || !g) return;
    const dpr = window.devicePixelRatio || 1;
    const w = window.innerWidth;
    const h = window.innerHeight;
    c.width = w * dpr;
    c.height = h * dpr;
    g.scale(dpr, dpr);
    const parts = Array.from({ length: 160 }, () => ({
      x: w / 2 + (Math.random() - 0.5) * w * 0.3,
      y: h * 0.2,
      vx: (Math.random() - 0.5) * 16,
      vy: -Math.random() * 14 - 4,
      size: 5 + Math.random() * 6,
      rot: Math.random() * Math.PI,
      vr: (Math.random() - 0.5) * 0.4,
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
    }));
    const start = performance.now();
    let raf = 0;
    const tick = (t: number) => {
      const age = (t - start) / 1000;
      g.clearRect(0, 0, w, h);
      g.globalAlpha = Math.max(0, 1 - Math.max(0, age - 2.8) / 1.2);
      for (const p of parts) {
        p.vy += 0.35;
        p.vx *= 0.985;
        p.vy *= 0.985;
        p.x += p.vx;
        p.y += p.vy;
        p.rot += p.vr;
        g.save();
        g.translate(p.x, p.y);
        g.rotate(p.rot);
        g.fillStyle = p.color;
        g.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2);
        g.restore();
      }
      if (age < 4) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return <canvas ref={ref} className="pointer-events-none fixed inset-0 h-full w-full" />;
}
