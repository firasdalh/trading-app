import { useEffect, useRef } from "react";
import type { PositionView } from "./types";

// "Follow trades" (the user, 2026-11-02): when the charted pair's trade closes, the chart moves on to the
// OLDEST trade still open, so the screen always watches an open trade. It waits until nobody has touched the
// mouse, touchpad or keyboard for IDLE_MS - it never pulls the chart from under your hand - and SETTLE_MS
// after the close, so the result shows first. Picking another pair yourself cancels it. With nothing open
// when the trade closes, it waits for the next trade to open and moves to that.
const IDLE_MS = 15_000;
const SETTLE_MS = 4_000;
const TICK_MS = 1_000;

const same = (a: string, b: string) => a.toUpperCase() === b.toUpperCase();

export function oldestOpen(positions: PositionView[]): PositionView | null {
  const t = (p: PositionView) => (p.opened_at ? Date.parse(p.opened_at) : Number.POSITIVE_INFINITY);
  return [...positions].sort((a, b) => t(a) - t(b))[0] ?? null;
}

export function useFollowTrades({ on, symbol, positions, busy, onFollow }: {
  on: boolean;
  symbol: string;
  positions: PositionView[] | null;
  /** Something you are in the middle of (a staged order): it waits while that lasts. */
  busy: boolean;
  onFollow: (to: PositionView, from: string) => void;
}) {
  const lastInput = useRef(Date.now());
  const had = useRef<string | null>(null);                       // the charted pair, while a trade on it is open
  const armed = useRef<{ symbol: string; at: number } | null>(null);
  const latest = useRef({ on, symbol, positions, busy, onFollow });
  latest.current = { on, symbol, positions, busy, onFollow };

  useEffect(() => {
    const touch = () => { lastInput.current = Date.now(); };
    const events = ["pointermove", "pointerdown", "keydown", "wheel", "touchstart"] as const;
    events.forEach((e) => window.addEventListener(e, touch, { capture: true, passive: true }));
    return () => events.forEach((e) => window.removeEventListener(e, touch, { capture: true }));
  }, []);

  // The charted pair's trade: open -> remembered; gone -> the follow is armed. A pair picked by hand cancels.
  useEffect(() => {
    if (!positions) return;
    if (armed.current && !same(armed.current.symbol, symbol)) armed.current = null;
    if (positions.some((p) => same(p.symbol, symbol))) {
      had.current = symbol;
      armed.current = null;
    } else if (had.current && same(had.current, symbol)) {
      had.current = null;
      armed.current = { symbol, at: Date.now() };
    } else {
      had.current = null;
    }
  }, [positions, symbol]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const a = armed.current;
      const { on, symbol, positions, busy, onFollow } = latest.current;
      if (!a || !on || !positions || busy) return;
      const now = Date.now();
      if (!same(a.symbol, symbol) || now - a.at < SETTLE_MS || now - lastInput.current < IDLE_MS) return;
      const to = oldestOpen(positions);
      if (!to) return;                                          // nothing open: wait for the next trade
      armed.current = null;
      onFollow(to, a.symbol);
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, []);
}
