import { useEffect, useRef, useState } from "react";

const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
// Back beside the account strip only with this much to spare, and not sooner than this after stepping aside, so a
// P&L that gains and loses a digit every few seconds does not make the clock blink in and out.
const RETURN_SPARE_PX = 16;
const RETURN_HOLD_MS = 30_000;

/**
 * A digital clock in the header (asked 2026-11-08: "add a nice watch ... like a nice digital watch to always keep
 * seeing the time there"). Your own time, big, with its seconds and a blinking colon; under it the date, as big and
 * bright as the box allows - "the date is not clear" (2026-11-09): it was 9px grey text, sharing its line with UTC.
 * UTC, the broker's clock, is in the hover with its own date: just past midnight here it is still yesterday there,
 * and "Mon, Nov 9 · UTC 21:41" read as Monday's 21:41.
 * It ticks on its own, so nothing else re-renders. Shown from 1700px wide, in the gap the account strip leaves before
 * the tabs, as tall as the strip so the header keeps its height.
 *
 * `strip` is the account strip's box. The account numbers come first: on a heavy day they grow ("-$107.87 (-10.32%)
 * · 28 today", "3 / 3 · full", TRIPPED), and at 1745px they ran 5px past their box - it scrolled them behind a bar
 * and half hid BREAKER, the one that matters then. So once a second the clock checks, steps aside while they need
 * the room, and comes back once they fit again.
 */
export function DeskClock({ strip }: { strip?: { readonly current: HTMLElement | null } }) {
  // The second, read 4x a second: a re-render only when it changes, and never more than 250 ms late.
  const [sec, setSec] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const t = window.setInterval(() => setSec(Math.floor(Date.now() / 1000)), 250);
    return () => window.clearInterval(t);
  }, []);

  const [room, setRoom] = useState(true);
  const rootRef = useRef<HTMLDivElement>(null);
  const ownWidth = useRef(110);
  const asideSince = useRef(0);
  useEffect(() => {
    const box = strip?.current;
    const row = box?.firstElementChild;
    const first = row?.firstElementChild;
    const last = row?.lastElementChild;
    if (!box || !first || !last) return;
    const spare = box.clientWidth - (last.getBoundingClientRect().right - first.getBoundingClientRect().left);
    if (room) {
      const w = rootRef.current?.offsetWidth ?? 0;
      if (w > 0) ownWidth.current = w;
      if (spare < 0) {
        asideSince.current = Date.now();
        setRoom(false);
      }
    } else if (Date.now() - asideSince.current >= RETURN_HOLD_MS) {
      const gap = parseFloat(getComputedStyle(box.parentElement ?? box).columnGap) || 16;
      if (spare >= ownWidth.current + gap + RETURN_SPARE_PX) setRoom(true);
    }
  }, [sec, room, strip]);

  const d = new Date(sec * 1000);
  const two = (n: number) => String(n).padStart(2, "0");
  const hh = two(d.getHours());
  const mm = two(d.getMinutes());
  const ss = two(d.getSeconds());
  // Spelled out, not toLocaleDateString: the same "MON 9 NOV" whatever the browser's language, day before month.
  const date = `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
  const utc = `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
  const off = -d.getTimezoneOffset() / 60;
  const offText = `UTC${off >= 0 ? "+" : "−"}${Number.isInteger(Math.abs(off)) ? Math.abs(off) : Math.abs(off).toFixed(1)}`;
  let zone = "your time";
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone || zone;
  } catch {
    /* an old browser: "your time" */
  }
  return (
    <div
      ref={rootRef}
      role="timer"
      aria-label={`${hh}:${mm}:${ss}, ${date}`}
      title={`${date} ${d.getFullYear()} · ${hh}:${mm}:${ss} - ${zone} (${offText})\nUTC, the broker's clock: ${utc}`}
      className={`hidden shrink-0 select-none flex-col items-center rounded-lg border border-emerald-500/20 bg-neutral-950/80 px-3 py-px shadow-[inset_0_0_14px_rgba(0,0,0,0.65),0_0_14px_-6px_rgba(52,211,153,0.45)] ${room ? "xl:flex" : ""}`}
    >
      <div className="font-mono text-[18px] font-semibold leading-none tracking-wider tabular-nums text-emerald-300 [text-shadow:0_0_8px_rgba(52,211,153,0.6)]">
        {hh}
        <span className={`transition-opacity duration-300 ${sec % 2 ? "opacity-25" : "opacity-100"}`}>:</span>
        {mm}
        <span className="ml-1 align-baseline text-[12px] text-emerald-400/70">{ss}</span>
      </div>
      {/* No glow here: on small letters it blurs them, and this line has to read at a glance. */}
      <div className="mt-0.5 whitespace-nowrap text-[12px] font-semibold leading-none tracking-[0.12em] text-emerald-200">
        {date}
      </div>
    </div>
  );
}
