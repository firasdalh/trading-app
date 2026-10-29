import { useEffect, useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import type { MarketHours } from "../types";

// The schedule changes rarely and the countdown ticks here, so a minute between polls is plenty -
// the live open/closed reading is at most that stale.
const POLL_MS = 60_000;
const SOON_MIN = 60;     // amber inside the last hour before a close
const URGENT_MIN = 15;   // red inside the last 15 minutes

const CLOSURE: Record<NonNullable<MarketHours["closure"]>, string> = {
  daily_break: "daily break",
  overnight: "overnight",
  weekend: "weekend",
  unscheduled: "unusual for this hour",
};

const BASIS =
  "Hours learned from this broker's own prices over the last 3 weeks, in New York time so US clock " +
  "changes are followed. Holidays ahead aren't known.";

/** "35m", "2h 14m", "1d 4h". */
export function span(ms: number): string {
  const m = Math.max(1, Math.round(ms / 60_000));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h < 24 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** "21:00" today, otherwise "Fri 01:00" - on the viewer's own clock, like the chart's default. */
export function at(iso: string, now: number): string {
  const d = new Date(iso);
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return new Date(now).toDateString() === d.toDateString()
    ? t
    : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
}

function utc(iso: string): string {
  const d = new Date(iso);
  return `${d.toUTCString().slice(0, 3)} ${d.toISOString().slice(11, 16)} UTC`;
}

/**
 * Next to the symbol: is this market open, and when does it close (or reopen)? Open-now is the
 * broker's live check; the times are learned from the symbol's own trading hours. Amber inside the
 * last hour before a close and red inside the last 15 minutes - the moment a new trade would be
 * carried into a break or the weekend.
 */
export function MarketHoursChip({ symbol, assetClass }: { symbol: string; assetClass: string }) {
  const { data } = usePolling(() => api.marketHours(symbol, assetClass), POLL_MS, [symbol, assetClass]);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);

  // The poll keeps the previous symbol's answer until the new one lands - never show it under this name.
  const h = data && data.symbol.toUpperCase() === symbol.toUpperCase() ? data : null;
  if (!h || h.open == null) return null;

  let tone = "bg-neutral-800 text-neutral-400";
  let dot = "bg-neutral-500";
  let text: string;
  let tip: string;

  if (h.open && h.always_open) {
    tone = "bg-bull/10 text-bull";
    dot = "bg-bull";
    text = "Open 24/7";
    tip = "Trades around the clock - no closure in this broker's recent prices.";
  } else if (h.open && h.closes_at) {
    const left = Date.parse(h.closes_at) - now;
    const mins = left / 60_000;
    const urgent = mins <= URGENT_MIN;
    const soon = mins <= SOON_MIN;
    tone = urgent
      ? "bg-bear/20 text-bear ring-1 ring-inset ring-bear/40"
      : soon
        ? "bg-warn/15 text-warn ring-1 ring-inset ring-warn/30"
        : "bg-bull/10 text-bull";
    dot = urgent ? "bg-bear animate-pulse" : soon ? "bg-warn" : "bg-bull";
    text = left < 60_000 ? "Open · closing now" : `Open · closes ${at(h.closes_at, now)} · in ${span(left)}`;
    tip =
      `Closes ${at(h.closes_at, now)} (${utc(h.closes_at)})` +
      (h.closure ? ` for the ${CLOSURE[h.closure]}` : "") +
      (h.opens_at ? `, reopens ${at(h.opens_at, now)} (${utc(h.opens_at)})` : "") +
      `.\n${BASIS}`;
  } else if (h.open) {
    tone = "bg-bull/10 text-bull";
    dot = "bg-bull";
    text = "Open";
    tip = "Open now (live prices). The next close couldn't be worked out from the price history.";
  } else if (h.opens_at) {
    const left = Date.parse(h.opens_at) - now;
    text = left < 60_000 ? "Closed · opening now" : `Closed · opens ${at(h.opens_at, now)} · in ${span(left)}`;
    tip = `Reopens ${at(h.opens_at, now)} (${utc(h.opens_at)}).\n${BASIS}`;
  } else {
    text = "Closed";
    tip = "No live prices right now. The next open couldn't be worked out from the price history.";
  }

  if (h.off_schedule) {
    tip += h.open
      ? "\nTrading outside its usual hours right now."
      : "\nClosed although it normally trades at this hour - a holiday or a broker pause.";
  }
  const tag = !h.always_open && h.closure ? CLOSURE[h.closure] : null;

  return (
    <span
      className={`inline-flex shrink-0 cursor-help items-center gap-1.5 whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-medium tabular-nums ${tone}`}
      title={tip}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} aria-hidden="true" />
      {text}
      {tag && <span className={`font-normal ${h.closure === "unscheduled" ? "text-warn" : "opacity-70"}`}>· {tag}</span>}
    </span>
  );
}
