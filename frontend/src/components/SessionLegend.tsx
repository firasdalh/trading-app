import { useEffect, useState } from "react";
import { span } from "./MarketHoursChip";

/**
 * Key for the chart's session bands, with the session we are in NOW lit up in its own colour, plus the time it
 * has left. The other two are dimmed. Asked 2026-11-08: the bands are a faint tint, and under a trade's stop and
 * target zones and the Fibonacci zones "which session is this?" couldn't be read off the chart any more.
 *
 * The hours and hues are the band painter's in Chart.tsx (the "trading sessions" block): Asia 00-08, London
 * 08-13, New York 13-21 UTC. Change one and change the other. "Now" is the real clock, not the last candle, and
 * the clock is UTC like the bands, whichever clock the chart's axis is written in.
 */
const SESSIONS = [
  { name: "Asia", from: 0, to: 8, rgb: "56,189,248",
    about: "Tokyo / Sydney. Thinnest liquidity; breaks here fail most often." },
  { name: "London", from: 8, to: 13, rgb: "167,139,250",
    about: "The first big-volume session of the day." },
  { name: "New York", from: 13, to: 21, rgb: "34,197,94",
    about: "It overlaps London until 16:00 UTC - the busiest window of the day." },
] as const;

/** Milliseconds from `now` until the next time the UTC clock reads `hour`:00. */
function untilHour(now: number, hour: number): number {
  const d = new Date(now);
  const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour);
  return t > now ? t - now : t + 86_400_000 - now;
}

/** `hour`:00 UTC on the viewer's own clock, e.g. "04:00". */
function localHour(hour: number): string {
  const d = new Date();
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour))
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

const pad = (h: number) => `${String(h).padStart(2, "0")}:00`;

export function SessionLegend() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(t);
  }, []);

  const hour = new Date(now).getUTCHours();
  const live = SESSIONS.find((s) => hour >= s.from && hour < s.to) ?? null;

  return (
    <div className="pointer-events-none absolute bottom-8 left-14 z-10 flex items-center gap-1 rounded bg-neutral-950/80 px-1 py-0.5 text-[10px]">
      {SESSIONS.map((s) => {
        const on = s === live;
        const hours = `${pad(s.from)}-${pad(s.to)} UTC = ${localHour(s.from)}-${localHour(s.to)} your time`;
        return (
          <span
            key={s.name}
            title={on
              ? `NOW: ${s.name}, until ${localHour(s.to)} your time (${span(untilHour(now, s.to))} left). ${hours}. ${s.about}`
              : `${s.name}: ${hours}. ${s.about}`}
            className={`pointer-events-auto flex items-center gap-1 rounded px-1.5 py-0.5 ${
              on ? "font-semibold text-white" : "text-neutral-500"}`}
            style={on ? {
              backgroundColor: `rgba(${s.rgb},0.24)`,
              boxShadow: `inset 0 0 0 1px rgba(${s.rgb},0.9), 0 0 12px rgba(${s.rgb},0.5)`,
            } : undefined}
          >
            <span
              className="h-2.5 w-3.5 rounded-sm"
              style={{
                backgroundColor: `rgba(${s.rgb},${on ? 1 : 0.35})`,
                boxShadow: on ? `0 0 6px rgba(${s.rgb},0.9)` : undefined,
              }}
            />
            {s.name}
            {on && (
              <span className="font-normal tabular-nums" style={{ color: `rgb(${s.rgb})` }}>
                now · {span(untilHour(now, s.to))} left
              </span>
            )}
          </span>
        );
      })}
      {!live && (
        <span
          className="pointer-events-auto flex items-center gap-1 px-1.5 py-0.5 text-neutral-300"
          title={`21:00-24:00 UTC (${localHour(21)}-${localHour(0)} your time): no band on the chart. Asia opens at ${localHour(0)} your time.`}
        >
          <span className="h-2 w-2 rounded-full border border-neutral-400" />
          between sessions · Asia in <span className="tabular-nums">{span(untilHour(now, 0))}</span>
        </span>
      )}
    </div>
  );
}
