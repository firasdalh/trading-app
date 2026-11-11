// The trading sessions on the UTC clock: ONE definition for the chart's session bands and their key (Chart.tsx,
// SessionLegend.tsx) and the journal's session colours (SessionStats.tsx). The backend's app/core/sessions.py has
// the same hours - it tags each closed trade with the session it opened in - so change both together.
// Middle East added 2026-11-10 ("Asia, Middle East, New York or London - to know which session is performing with
// us better"): the Gulf markets' first hours, before London. Fixed UTC hours, no overlaps.
export interface TradingSession {
  key: "asia" | "middle_east" | "london" | "new_york" | "between";
  name: string;
  from: number;          // UTC hour, inclusive
  to: number;            // UTC hour, exclusive
  rgb: string;           // its hue: the legend, the journal, and the chart band at `band` opacity
  band: number | null;   // the chart band's opacity; null = no band (between sessions)
  about: string;
}

export const SESSIONS: readonly TradingSession[] = [
  { key: "asia", name: "Asia", from: 0, to: 6, rgb: "56,189,248", band: 0.05,
    about: "Tokyo, Hong Kong, Singapore. Thinnest liquidity; breaks here fail most often." },
  { key: "middle_east", name: "Middle East", from: 6, to: 8, rgb: "45,212,191", band: 0.05,
    about: "The Gulf markets open (10:00 in Dubai) while Asia winds down, before London." },
  { key: "london", name: "London", from: 8, to: 13, rgb: "167,139,250", band: 0.06,
    about: "The first big-volume session of the day." },
  { key: "new_york", name: "New York", from: 13, to: 21, rgb: "34,197,94", band: 0.05,
    about: "It overlaps London until 16:00 UTC - the busiest window of the day." },
  { key: "between", name: "Between sessions", from: 21, to: 24, rgb: "163,163,163", band: null,
    about: "After New York closes, before Asia opens: the thinnest hours. Only crypto trades then." },
];

/** The session a UTC hour (0-23) belongs to. */
export const sessionAtHour = (hour: number): TradingSession =>
  SESSIONS.find((s) => hour >= s.from && hour < s.to) ?? SESSIONS[SESSIONS.length - 1];

export const sessionByKey = (key: string | null | undefined): TradingSession | null =>
  SESSIONS.find((s) => s.key === key) ?? null;

/** `hour`:00 UTC on the viewer's own clock, e.g. "10:00". */
export function localHour(hour: number): string {
  const d = new Date();
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour))
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** "06:00" - a UTC hour written out. */
export const utcHour = (h: number) => `${String(h % 24).padStart(2, "0")}:00`;
