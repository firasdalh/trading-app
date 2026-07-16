// A friendly label + colour for each trade source (WHO opened a position). Shared by the journal
// by-source stats and the Position advisor badge so the origin reads the same everywhere.
export const SOURCE_META: Record<string, { label: string; color: string }> = {
  ai: { label: "AI decision", color: "text-violet-300" },
  rsi_over: { label: "RSI-Over", color: "text-sky-300" },
  armed: { label: "Armed break", color: "text-amber-300" },
  hybrid: { label: "Hybrid", color: "text-emerald-300" },
  auto_trade: { label: "Auto-trade", color: "text-fuchsia-300" },
  manual: { label: "Manual", color: "text-neutral-200" },
  deterministic: { label: "Deterministic", color: "text-blue-300" },
  analysis: { label: "Analysis (legacy)", color: "text-indigo-300" },
  supertrend: { label: "SuperTrend", color: "text-teal-300" },
  unknown: { label: "Unknown", color: "text-neutral-500" },
};

export const srcLabel = (s: string): string => SOURCE_META[s]?.label ?? s;
export const srcColor = (s: string): string => SOURCE_META[s]?.color ?? "text-neutral-300";
