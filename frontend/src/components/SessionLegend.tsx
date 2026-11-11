import { useEffect, useState } from "react";
import { localHour, SESSIONS, sessionAtHour, utcHour } from "../sessions";
import { span } from "./MarketHoursChip";

/**
 * Key for the chart's session bands, with the session we are in NOW lit up in its own colour, plus the time it
 * has left. The others are dimmed. Asked 2026-11-08: the bands are a faint tint, and under a trade's stop and
 * target zones and the Fibonacci zones "which session is this?" couldn't be read off the chart any more.
 *
 * The hours and hues are ../sessions.ts: the band painter's in Chart.tsx too, and the journal's (the backend tags
 * each closed trade by the same hours). Middle East joined on 2026-11-10. "Now" is the real clock, not the last
 * candle, and the clock is UTC like the bands, whichever clock the chart's axis is written in.
 */
const BANDED = SESSIONS.filter((s) => s.band != null);

/** Milliseconds from `now` until the next time the UTC clock reads `hour`:00. */
function untilHour(now: number, hour: number): number {
  const d = new Date(now);
  const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour);
  return t > now ? t - now : t + 86_400_000 - now;
}

export function SessionLegend() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(t);
  }, []);

  const here = sessionAtHour(new Date(now).getUTCHours());
  const live = here.band != null ? here : null;

  return (
    <div className="pointer-events-none absolute bottom-8 left-14 z-10 flex items-center gap-1 rounded bg-neutral-950/80 px-1 py-0.5 text-[10px]">
      {BANDED.map((s) => {
        const on = s === live;
        const hours = `${utcHour(s.from)}-${utcHour(s.to)} UTC = ${localHour(s.from)}-${localHour(s.to % 24)} your time`;
        return (
          <span
            key={s.key}
            title={on
              ? `NOW: ${s.name}, until ${localHour(s.to % 24)} your time (${span(untilHour(now, s.to % 24))} left). ${hours}. ${s.about}`
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
                now · {span(untilHour(now, s.to % 24))} left
              </span>
            )}
          </span>
        );
      })}
      {!live && (
        <span
          className="pointer-events-auto flex items-center gap-1 px-1.5 py-0.5 text-neutral-300"
          title={`${utcHour(here.from)}-${utcHour(here.to)} UTC (${localHour(here.from)}-${localHour(here.to % 24)} your time): no band on the chart. Asia opens at ${localHour(0)} your time.`}
        >
          <span className="h-2 w-2 rounded-full border border-neutral-400" />
          between sessions · Asia in <span className="tabular-nums">{span(untilHour(now, 0))}</span>
        </span>
      )}
    </div>
  );
}
