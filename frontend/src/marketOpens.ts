/**
 * The stock markets' cash opens, for the market-open alerts (components/OpenAlerts.tsx, asked 2026-11-20). Each open
 * is kept on its exchange's own clock, so summer / winter clock changes are followed, and turned into a moment here.
 * Weekdays only. Exchange holidays are not known.
 */

export interface MarketOpen {
  key: string;
  name: string;
  zone: string;   // IANA time zone of the exchange
  h: number;      // local opening hour
  m: number;      // local opening minute
  pairs: string;  // the desk's index pairs it moves first
}

export const MARKET_OPENS: MarketOpen[] = [
  { key: "tokyo", name: "Tokyo", zone: "Asia/Tokyo", h: 9, m: 0, pairs: "JP225" },
  { key: "hongkong", name: "Hong Kong", zone: "Asia/Hong_Kong", h: 9, m: 30, pairs: "HK50" },
  // Frankfurt and Paris open at 09:00 CET and London at 08:00 UK time: the same moment, all year round.
  { key: "europe", name: "Europe", zone: "Europe/Berlin", h: 9, m: 0, pairs: "DE30 · FR40 · UK100" },
  { key: "newyork", name: "New York", zone: "America/New_York", h: 9, m: 30, pairs: "US30 · USTEC · US500" },
];

export const LEAD_MS = 30 * 60_000;   // from half an hour before an open...
export const AFTER_MS = 30 * 60_000;  // ...until half an hour after it

const DAY_MS = 86_400_000;
const fmts = new Map<string, Intl.DateTimeFormat>();

function partsIn(zone: string, ms: number) {
  let f = fmts.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    fmts.set(zone, f);
  }
  const p: Record<string, string> = {};
  for (const x of f.formatToParts(new Date(ms))) p[x.type] = x.value;
  return { y: Number(p.year), mo: Number(p.month), d: Number(p.day), h: Number(p.hour) % 24, mi: Number(p.minute), s: Number(p.second) };
}

/** How far the zone's clock is ahead of UTC at `ms`, in ms. */
function offsetAt(zone: string, ms: number): number {
  const p = partsIn(zone, ms);
  return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi, p.s) - Math.floor(ms / 1000) * 1000;
}

/** The moment `y-mo-d h:m` on `zone`'s clock. */
export function zonedTime(zone: string, y: number, mo: number, d: number, h: number, m: number): number {
  const wall = Date.UTC(y, mo - 1, d, h, m);
  const first = wall - offsetAt(zone, wall);
  return wall - offsetAt(zone, first);   // again from the first guess: right across a clock change
}

/** The market's opens on yesterday's, today's and tomorrow's date on its own calendar, weekdays only. */
export function opensAround(mk: MarketOpen, now: number): number[] {
  const out: number[] = [];
  for (const k of [-1, 0, 1]) {
    const p = partsIn(mk.zone, now + k * DAY_MS);
    const wd = new Date(Date.UTC(p.y, p.mo - 1, p.d)).getUTCDay();
    if (wd === 0 || wd === 6) continue;
    const t = zonedTime(mk.zone, p.y, p.mo, p.d, mk.h, mk.m);
    if (!out.includes(t)) out.push(t);
  }
  return out;
}

export interface OpenAlert {
  key: string;     // market:moment - one per open
  market: MarketOpen;
  open: number;    // the moment it opens
}

/** The opens whose alert window (LEAD_MS before to AFTER_MS after) holds `now`, soonest first. */
export function activeOpens(now: number): OpenAlert[] {
  const out: OpenAlert[] = [];
  for (const mk of MARKET_OPENS) {
    for (const t of opensAround(mk, now)) {
      if (now >= t - LEAD_MS && now < t + AFTER_MS) out.push({ key: `${mk.key}:${t}`, market: mk, open: t });
    }
  }
  return out.sort((a, b) => a.open - b.open);
}

/** "18:30" on the viewer's own clock. */
export const clockOf = (ms: number) =>
  new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
