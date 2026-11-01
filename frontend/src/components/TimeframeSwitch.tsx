import { Fragment } from "react";
import { FAST_TF, SCALP_FAST, TIMEFRAMES, analysisTimeframe, isSecondsTf } from "../timeframes";

// Segmented timeframe switch: one click instead of a dropdown, in the chart's header (desk, its full
// screen and chart windows). Switching timeframe is the most common thing you do while reading a
// chart. `options` narrows it. In scalp mode the fast charts lead, a divider, then the bigger picture.
export function TimeframeSwitch({ value, onChange, options = TIMEFRAMES, scalp = false }: {
  value: string;
  onChange: (tf: string) => void;
  options?: string[];
  scalp?: boolean;
}) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-md border p-0.5 ${
        scalp ? "border-amber-500/40 bg-neutral-900/80" : "border-neutral-700 bg-neutral-800/60"
      }`}
      role="group"
      aria-label="Timeframe"
    >
      {options.map((t, i) => {
        // Where the fast charts end and the context ones begin.
        const split = scalp && i > 0 && SCALP_FAST.includes(options[i - 1]) && !SCALP_FAST.includes(t);
        const context = scalp && !SCALP_FAST.includes(t);
        return (
          <Fragment key={t}>
            {split && <span className="mx-1 h-4 w-px bg-neutral-600" aria-hidden="true" />}
            <button
              onClick={() => onChange(t)}
              aria-pressed={t === value}
              title={
                (context ? `${t}: the bigger picture around a scalp. A pre-order staged here takes its ATR stop from the ${t} bars. ` : "") +
                (isSecondsTf(t)
                  ? `${t} candles, built from the broker's ticks. Chart only: analysis, tickets and setups from it run on 1m`
                  : analysisTimeframe(t) !== t
                    ? `${t} candles (MT5's own). Chart only: analysis, tickets and setups from it run on ${analysisTimeframe(t)}`
                    : FAST_TF.has(t)
                      ? `${t} — backtested as loss-making for entries; fine for timing`
                      : `Switch the chart and the analysis to ${t}`)
              }
              className={`rounded px-2 py-0.5 text-xs font-medium tabular-nums transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50 ${
                t === value
                  ? scalp ? "bg-amber-500 font-bold text-neutral-950" : "bg-brand-600 text-white"
                  : scalp
                    ? context ? "text-neutral-500 hover:bg-neutral-700 hover:text-neutral-200" : "text-amber-100/90 hover:bg-amber-500/20 hover:text-white"
                    : FAST_TF.has(t) || analysisTimeframe(t) !== t
                      ? "text-neutral-500 hover:bg-neutral-700 hover:text-neutral-200"
                      : "text-neutral-300 hover:bg-neutral-700 hover:text-white"
              }`}
            >
              {t}
            </button>
          </Fragment>
        );
      })}
    </span>
  );
}

// Named at the point of use, not buried in a doc: the cost of a fast timeframe is invisible on the
// chart but decisive in the results. `compact` (scalp mode, where every chart is a fast one) keeps
// the warning and puts its explanation in the tooltip.
export function TimeframeBadge({ tf, compact = false }: { tf: string; compact?: boolean }) {
  if (FAST_TF.has(tf)) {
    return (
      <span
        className="shrink-0 rounded bg-warn/10 px-1.5 py-0.5 text-[11px] text-warn"
        title="Backtested over 959 signals: 15m lost -0.159R per trade in both halves of the data — the win rate drops and the fixed spread eats about twice as much of a smaller stop. Fine for reading price; poor for trading."
      >
        {compact ? `⚠ ${tf}: tested −R` : `⚠ ${tf}: timing only, tested loss-making`}
      </span>
    );
  }
  const aTf = analysisTimeframe(tf);
  if (aTf !== tf) {
    return (
      <span
        className="shrink-0 rounded bg-warn/10 px-1.5 py-0.5 text-[11px] text-warn"
        title={
          isSecondsTf(tf)
            ? "MT5 has no candle under 1 minute, so these are built from the broker's ticks (the bid, as on every MT5 chart). Run analysis, the price map, AI scenarios, order blocks, chart tickets, armed setups and auto-trade all work on 1m from here."
            : `The engine was never built or tested on ${tf}. Run analysis, the price map, AI scenarios, order blocks, chart tickets, armed setups and auto-trade all work on ${aTf} from here.`
        }
      >
        {compact ? `⚠ analysis on ${aTf}` : `⚠ ${tf}: chart only, analysis on ${aTf}`}
      </span>
    );
  }
  return null;
}
