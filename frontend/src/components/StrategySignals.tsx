import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import { play } from "../hooks/useApprovalChime";
import type { AssetClass, StrategyActivity } from "../types";

/**
 * WATCH-ONLY SIGNALS (asked 2026-11-06): a strategy's BUY / SELL group switched to "watch" on the board opens
 * nothing, but the engine still reads it and logs a "signal" the moment all of it starts to hold. Announced
 * here, on every view: a chime, a pop-up under the header (Chart › / ✕, gone after a minute) and - with the
 * browser's permission - a system notification, which reaches you in another window too. Mount ONCE (App).
 */

const POLL_MS = 5000;
const SHOW_MS = 60_000;
const MAX_SHOWN = 4;
/** App asks the desk to chart a signal's pair with this window event: `{ symbol, assetClass }`. */
export const SIGNAL_PICK_EVENT = "strategy-signal:pick";
// Its own three rising notes: a signal never sounds like an approval waiting (two notes).
const NOTES = [
  { hz: 587.33, at: 0.0, len: 0.12 },   // D5
  { hz: 739.99, at: 0.11, len: 0.12 },  // F#5
  { hz: 987.77, at: 0.22, len: 0.26 },  // B5
];

export interface Signal {
  id: number;
  at: string | null;
  symbol: string;
  assetClass: AssetClass | null;
  side: "long" | "short";
  group: number;
  name: string | null;
  price: number | null;
  rules: string[];
}

/** Ask for system notifications - from a click (browsers ignore or bury a request made without one). */
export function askNotifyPermission() {
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    void Notification.requestPermission();
  }
}

function signalOf(a: StrategyActivity): Signal | null {
  const d = (a.detail ?? {}) as Record<string, unknown>;
  if (a.event !== "signal" || !a.symbol || (d.side !== "long" && d.side !== "short")) return null;
  return {
    id: a.id, at: a.at, symbol: a.symbol, assetClass: a.asset_class ?? null, side: d.side,
    group: typeof d.group === "number" ? d.group : 0,
    name: typeof d.name === "string" ? d.name : null,
    price: typeof d.price === "number" ? d.price : null,
    rules: Array.isArray(d.rules) ? (d.rules as string[]) : [],
  };
}

const sideText = (s: Signal) => (s.side === "long" ? "BUY" : "SELL");

/** The signals to show, and a way to dismiss one. ``onPick`` charts a pair (a system notification's click). */
export function useStrategySignals(onPick: (symbol: string, ac: AssetClass) => void): [Signal[], (id: number) => void] {
  const [shown, setShown] = useState<Signal[]>([]);
  const seen = useRef<number | null>(null);      // the newest event already looked at; null = not read yet
  const pick = useRef(onPick);
  pick.current = onPick;
  const dismiss = (id: number) => setShown((xs) => xs.filter((x) => x.id !== id));

  useEffect(() => {
    let alive = true;
    const timers: number[] = [];
    const check = async () => {
      let items: StrategyActivity[];
      try {
        items = await api.strategyActivityAll(30);
      } catch {
        return;                                    // the next poll tries again
      }
      if (!alive) return;
      const top = Math.max(0, ...items.map((a) => a.id));
      // The first read is the baseline: signals already there when the desk opened are not news.
      if (seen.current == null) {
        seen.current = top;
        return;
      }
      const after = seen.current;
      seen.current = Math.max(after, top);
      const fresh = items.filter((a) => a.id > after).map(signalOf).filter((s): s is Signal => s != null).reverse();
      if (!fresh.length) return;
      play(NOTES, 0.16);
      for (const s of fresh) {
        if (typeof Notification !== "undefined" && Notification.permission === "granted") {
          try {
            const n = new Notification(`${sideText(s)} signal · ${displaySymbol(s.symbol)}`, {
              body: `${s.name ?? "Strategy"} - group ${s.group} (watch only) holds${s.price != null ? ` at ${fmtPrice(s.price)}` : ""}. It opened nothing.`,
              tag: `strategy-signal-${s.id}`,          // two desk tabs show it once
            });
            n.onclick = () => {
              window.focus();
              if (s.assetClass) pick.current(s.symbol, s.assetClass);
              n.close();
            };
          } catch {
            /* a browser without page notifications: the pop-up below still shows */
          }
        }
        timers.push(window.setTimeout(() => alive && setShown((xs) => xs.filter((x) => x.id !== s.id)), SHOW_MS));
      }
      const newest = [...fresh].reverse();
      setShown((xs) => [...newest, ...xs].slice(0, MAX_SHOWN));
    };
    void check();
    const t = window.setInterval(() => void check(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(t);
      timers.forEach((x) => window.clearTimeout(x));
    };
  }, []);

  return [shown, dismiss];
}

function SignalCard({ s, onPick, onClose }: {
  s: Signal; onPick: (symbol: string, ac: AssetClass) => void; onClose: () => void;
}) {
  const [open, setOpen] = useState(false);
  const long = s.side === "long";
  const when = s.at ? new Date(s.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "";
  const ask = typeof Notification !== "undefined" && Notification.permission === "default";
  return (
    <div role="status" className="pointer-events-auto w-[23rem] max-w-[calc(100vw-2rem)] rounded-lg border border-sky-400/60 bg-neutral-900/95 p-2.5 text-xs shadow-xl backdrop-blur-sm">
      <div className="flex items-start gap-2">
        <span className="text-base leading-none" aria-hidden="true">👁</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-1.5">
            <b className={long ? "text-bull" : "text-bear"}>{long ? "▲ BUY" : "▼ SELL"} signal</b>
            <b className="text-neutral-100">{displaySymbol(s.symbol)}</b>
            {s.price != null && <span className="tabular-nums text-neutral-300">at {fmtPrice(s.price)}</span>}
            <span className="ml-auto tabular-nums text-neutral-500">{when}</span>
          </div>
          <div className="mt-0.5 text-sky-200/90">
            {s.name ?? "Strategy"} · group {s.group} · watch only - it opened nothing
          </div>
        </div>
        <button type="button" onClick={onClose} title="Dismiss" className="-mr-1 -mt-1 rounded px-1 text-neutral-500 hover:bg-neutral-800 hover:text-neutral-200">
          ✕
        </button>
      </div>
      {open && s.rules.length > 0 && (
        <ul className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto rounded bg-neutral-950/60 px-2 py-1 text-[11px] text-neutral-300">
          {s.rules.map((r, i) => (
            <li key={i} className="flex gap-1.5">
              <span className="text-bull">✓</span>
              <span className="min-w-0">{r}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        {s.rules.length > 0 && (
          <button type="button" onClick={() => setOpen((o) => !o)} className="whitespace-nowrap rounded px-1 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200">
            {open ? "Hide the rules ▴" : `All ${s.rules.length} rules held ▾`}
          </button>
        )}
        {ask && (
          <button
            type="button"
            onClick={askNotifyPermission}
            title="Let the browser show these as system notifications too - they then reach you in another window"
            className="whitespace-nowrap rounded px-1 text-amber-200/90 hover:bg-neutral-800"
          >
            🔔 Allow notifications
          </button>
        )}
        {s.assetClass && (
          <button
            type="button"
            onClick={() => {
              onPick(s.symbol, s.assetClass as AssetClass);
              onClose();
            }}
            className="ml-auto whitespace-nowrap rounded bg-sky-500/20 px-2 py-0.5 font-semibold text-sky-200 hover:bg-sky-500/30"
          >
            Chart {displaySymbol(s.symbol)} ›
          </button>
        )}
      </div>
    </div>
  );
}

/** The pop-ups, newest on top, under the header on the right. */
export function SignalToasts({ signals, onPick, onDismiss }: {
  signals: Signal[]; onPick: (symbol: string, ac: AssetClass) => void; onDismiss: (id: number) => void;
}) {
  if (!signals.length) return null;
  return (
    <div
      className="pointer-events-none fixed right-4 z-[60] flex flex-col gap-2"
      style={{ top: "calc(var(--hdr-h, 56px) + 0.5rem)" }}
      aria-live="polite"
    >
      {signals.map((s) => (
        <SignalCard key={s.id} s={s} onPick={onPick} onClose={() => onDismiss(s.id)} />
      ))}
    </div>
  );
}
