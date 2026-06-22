// Small colored chip showing the market regime + the strategy it permits, so "what are we doing
// and why" is visible at a glance (trend in a trend, fade in a range, aside in a whipsaw).
const REGIME_COLOR: Record<string, string> = {
  trending: "bg-bull/15 text-bull",
  moderate: "bg-blue-500/15 text-blue-300",
  ranging: "bg-warn/15 text-warn",
  volatile: "bg-bear/15 text-bear",
};
const STRATEGY_LABEL: Record<string, string> = {
  trend: "trend",
  mean_reversion: "fade",
  stand_aside: "aside",
};

export function RegimeBadge({
  regime,
  strategy,
  className = "",
}: {
  regime?: string | null;
  strategy?: string | null;
  className?: string;
}) {
  if (!regime) return null;
  const s = strategy ? STRATEGY_LABEL[strategy] ?? strategy : null;
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium ${REGIME_COLOR[regime] ?? "bg-neutral-700 text-neutral-200"} ${className}`}
      title={`Market regime: ${regime}${strategy ? ` → ${strategy}` : ""}`}
    >
      {regime}
      {s ? ` · ${s}` : ""}
    </span>
  );
}
