import { useEffect, useRef, useState } from "react";
import { play } from "../hooks/useApprovalChime";
import { activeOpens, clockOf, type OpenAlert } from "../marketOpens";
import { askNotifyPermission } from "./StrategySignals";

/**
 * MARKET-OPEN ALERTS (asked 2026-11-20, after a bounce at the New York open stopped three shorts: "an alert that
 * market X opens soon - half an hour before and half an hour after"). DISPLAY ONLY: nothing here reaches the engine
 * or an order. Shown on every view (App), from 30 min before each cash open (Tokyo, Hong Kong, Europe, New York)
 * until 30 min after it: a card under the header (✕ hides that one), a chime and - with the browser's permission -
 * a system notification when the half hour before begins.
 * Why (study 2026-11-20, 23 markets, M15 since 2022-11): the hour after an open turns against the last 2 h trend no
 * more often than any other hour (about 50%), but it swings against it about twice as far, so stops get hit more easily.
 */

const TICK_MS = 15_000;
const DISMISS_KEY = "openAlerts.dismissed";
// Its own three notes (C5 E5 C5): never mistaken for a signal (three rising) or an approval waiting (two).
const NOTES = [
  { hz: 523.25, at: 0.0, len: 0.16 },
  { hz: 659.25, at: 0.15, len: 0.16 },
  { hz: 523.25, at: 0.3, len: 0.26 },
];
const WHY = "The first hour after an open usually swings about twice as far as other hours, so stops get hit more " +
  "easily; the trend turns no more often than at other times (23 markets, 2022-2026).";

function readDismissed(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(DISMISS_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function notify(a: OpenAlert) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    const n = new Notification(`${a.market.name} opens at ${clockOf(a.open)}`, {
      body: `In 30 minutes · ${a.market.pairs}. ${WHY} Display only - the strategy keeps running.`,
      tag: `market-open-${a.key}`,   // two desk tabs show it once
    });
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch {
    /* no page notifications in this browser: the card still shows */
  }
}

/** The opens to show now (not hidden with ✕) and a way to hide one. Announces each when its half hour before starts. */
export function useOpenAlerts(): [OpenAlert[], (key: string) => void] {
  const [now, setNow] = useState(() => Date.now());
  const [dismissed, setDismissed] = useState<string[]>(readDismissed);
  const announced = useRef<Set<string> | null>(null);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), TICK_MS);
    const onStorage = (e: StorageEvent) => e.key === DISMISS_KEY && setDismissed(readDismissed());
    window.addEventListener("storage", onStorage);
    return () => {
      window.clearInterval(t);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const live = activeOpens(now);
  const liveKeys = live.map((a) => a.key).join(",");
  useEffect(() => {
    // The desk opening inside a window shows its card but does not sound: only a window that starts while it is open.
    if (announced.current == null) {
      announced.current = new Set(live.map((a) => a.key));
      return;
    }
    const seen = announced.current;
    const fresh = live.filter((a) => !seen.has(a.key) && Date.now() < a.open);
    live.forEach((a) => seen.add(a.key));
    if (!fresh.length) return;
    play(NOTES, 0.14);
    fresh.forEach(notify);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveKeys]);

  const dismiss = (key: string) => {
    // Keep the last couple of days only: a key names one open, so an old one never matters again.
    const cut = Date.now() - 2 * 86_400_000;
    const next = [...readDismissed().filter((k) => Number(k.split(":")[1]) > cut), key];
    try {
      localStorage.setItem(DISMISS_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable: hidden in this tab only */
    }
    setDismissed(next);
  };

  return [live.filter((a) => !dismissed.includes(a.key)), dismiss];
}

/** One open's card: counting down to it, then the minutes since. */
export function OpenAlertCard({ a, onClose }: { a: OpenAlert; onClose: () => void }) {
  const now = Date.now();
  const before = now < a.open;
  const gap = Math.abs(a.open - now) / 60_000;
  const mins = Math.max(1, before ? Math.ceil(gap) : Math.floor(gap));   // "in 20 min" counts up, "11 min ago" down
  const ask = typeof Notification !== "undefined" && Notification.permission === "default";
  return (
    <div role="status" className="pointer-events-auto w-[23rem] max-w-[calc(100vw-2rem)] rounded-lg border border-sky-400/60 bg-neutral-900/95 p-2.5 text-xs shadow-xl backdrop-blur-sm">
      <div className="flex items-start gap-2">
        <span className="text-base leading-none" aria-hidden="true">🔔</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-1.5">
            <b className="text-sky-200">{a.market.name}</b>
            <span className="text-neutral-200">
              {before ? "opens at" : "opened at"} <b className="tabular-nums">{clockOf(a.open)}</b>
            </span>
            <span className={`ml-auto tabular-nums font-semibold ${before ? "text-amber-300" : "text-neutral-400"}`}>
              {before ? `in ${mins} min` : `${mins} min ago`}
            </span>
          </div>
          <div className="mt-0.5 text-neutral-400">
            {a.market.pairs} · {before ? "the open is coming" : "its first half hour"} - swings run about 2× bigger.
          </div>
        </div>
        <button type="button" onClick={onClose} title="Hide this alert" className="-mr-1 -mt-1 rounded px-1 text-neutral-500 hover:bg-neutral-800 hover:text-neutral-200">
          ✕
        </button>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
        <span className="text-neutral-500" title={`${WHY} Holidays are not known. On your clock.`}>
          Display only - the strategy keeps running.
        </span>
        {ask && (
          <button
            type="button"
            onClick={askNotifyPermission}
            title="Let the browser show these as system notifications too - they then reach you in another window"
            className="ml-auto whitespace-nowrap rounded px-1 text-amber-200/90 hover:bg-neutral-800"
          >
            🔔 Allow notifications
          </button>
        )}
      </div>
    </div>
  );
}

/** The cards, soonest first - mounted once, inside the pop-up column under the header (App, SignalToasts). Its
 * 15-second tick re-renders only this, never the desk. */
export function OpenAlerts() {
  const [alerts, dismiss] = useOpenAlerts();
  if (!alerts.length) return null;
  return (
    <>
      {alerts.map((a) => (
        <OpenAlertCard key={a.key} a={a} onClose={() => dismiss(a.key)} />
      ))}
    </>
  );
}
