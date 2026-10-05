import { fmtPrice } from "../format";
import type { ChartState } from "../types";

/**
 * WHAT PRICE IS DOING - the read a trader makes at a glance and the engine never stated.
 *
 * A 1h uptrend pulling back to value and a 1h uptrend just rejected at resistance are the same
 * trend and completely different trades. This says which one you are looking at, and - the part
 * that makes it honest - gives the two PRICES that settle it. "Resumes above 84,900, wrong below
 * 83,500" is checkable tomorrow. "Looks bullish" is not.
 */

const STATE_META: Record<string, { label: string; cls: string; icon: string }> = {
  breakout:  { label: "Breaking out",  cls: "bg-bull/15 text-bull ring-bull/30",        icon: "↗" },
  breakdown: { label: "Breaking down", cls: "bg-bear/15 text-bear ring-bear/30",        icon: "↘" },
  pullback:  { label: "Pullback",      cls: "bg-brand-500/15 text-brand-400 ring-brand-500/30", icon: "↩" },
  rejection: { label: "Rejection",     cls: "bg-warn/15 text-warn ring-warn/30",        icon: "⊣" },
  reversal:  { label: "Reversal",      cls: "bg-warn/15 text-warn ring-warn/30",        icon: "⇄" },
  range:     { label: "Ranging",       cls: "bg-neutral-700/50 text-neutral-300 ring-neutral-600", icon: "↔" },
  impulse:   { label: "Impulse",       cls: "bg-neutral-700/50 text-neutral-200 ring-neutral-600", icon: "⇉" },
  unclear:   { label: "Unclear",       cls: "bg-neutral-800 text-neutral-400 ring-neutral-700", icon: "?" },
};

interface Props {
  cs: ChartState | null | undefined;
  levelsShown?: boolean;
  onShowLevels?: (lv: { support: number | null; resistance: number | null;
                        target: number | null; invalidation: number | null } | null) => void;
}

export function ChartStateCard({ cs, levelsShown, onShowLevels }: Props) {
  if (!cs || cs.state === "unclear") return null;
  const m = STATE_META[cs.state] ?? STATE_META.unclear;
  const canPlot = onShowLevels && (cs.trigger != null || cs.invalidation != null || cs.target_level != null);

  return (
    <div className="rounded-md border border-neutral-800 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
          What price is doing
        </span>
        {canPlot && (
          <button
            onClick={() => onShowLevels!(levelsShown ? null : {
              support: cs.trend === "up" ? cs.invalidation : null,
              resistance: cs.trend === "up" ? null : cs.invalidation,
              target: cs.target_level,
              invalidation: cs.trigger,
            })}
            className="btn btn-ghost text-xs"
            title="Draw the trigger, invalidation and objective on the chart"
          >
            {levelsShown ? "Hide on chart" : "Show on chart"}
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded px-2 py-0.5 text-xs font-bold uppercase tracking-wide ring-1 ring-inset ${m.cls}`}>
          {m.icon} {m.label}
        </span>
        <span className="text-sm text-neutral-200">{cs.headline}</span>
        {cs.retrace_frac != null && (
          <span className="text-xs tabular-nums text-neutral-500">
            {(cs.retrace_frac * 100).toFixed(0)}% retraced
          </span>
        )}
      </div>

      {cs.evidence.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {cs.evidence.map((e, i) => (
            <li key={i} className="flex gap-1.5 text-xs text-neutral-400">
              <span className="text-neutral-600">•</span>
              <span className="prose-measure">{e}</span>
            </li>
          ))}
        </ul>
      )}

      {/* The two prices. This is the whole reason the card is allowed to talk about the future. */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <Price label="Confirms at" v={cs.trigger} tone="text-neutral-100" />
        <Price label="Wrong at" v={cs.invalidation} tone="text-bear" />
        <Price label="Objective" v={cs.target_level} tone="text-bull" />
      </div>

      {cs.expected && (
        <p className="prose-measure mt-2 text-xs leading-relaxed text-neutral-400">
          <span className="font-medium text-neutral-300">Next: </span>{cs.expected}
        </p>
      )}
    </div>
  );
}

function Price({ label, v, tone }: { label: string; v: number | null; tone: string }) {
  return (
    <div className="rounded bg-neutral-900/50 px-2.5 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`text-sm font-semibold tabular-nums ${v == null ? "text-neutral-600" : tone}`}>
        {v == null ? "—" : fmtPrice(v)}
      </div>
    </div>
  );
}
