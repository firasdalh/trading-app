import type { PositionAdvice } from "../types";

interface Props {
  advice: PositionAdvice[] | null;
}

const TONE: Record<PositionAdvice["severity"], { box: string; chip: string; label: string }> = {
  danger: { box: "border-bear/50 bg-bear/10", chip: "bg-bear/20 text-bear", label: "Act now" },
  warn: { box: "border-warn/40 bg-warn/10", chip: "bg-warn/20 text-warn", label: "Decide" },
  info: { box: "border-neutral-800 bg-neutral-900/40", chip: "bg-neutral-800 text-neutral-300", label: "OK" },
};

// AI guidance for OPEN positions — protect winners / cut losers, especially around news.
// Advisory only: it tells you what to consider; you act via the positions table below.
export function PositionAdvicePanel({ advice }: Props) {
  if (!advice || advice.length === 0) return null;
  // Surface the most urgent first.
  const order = { danger: 0, warn: 1, info: 2 } as const;
  const sorted = [...advice].sort((a, b) => order[a.severity] - order[b.severity]);

  return (
    <div className="card">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
        Position advisor
        <span className="text-xs font-normal text-neutral-500">
          managing open trades — suggestions only
        </span>
      </div>
      <div className="space-y-2">
        {sorted.map((a) => {
          const tone = TONE[a.severity];
          return (
            <div key={`${a.symbol}-${a.direction}`} className={`rounded-md border px-3 py-2 ${tone.box}`}>
              <div className="flex items-center gap-2">
                <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${tone.chip}`}>
                  {tone.label}
                </span>
                <span className="text-sm font-medium">{a.headline}</span>
                <span
                  className={`ml-auto text-xs tabular-nums ${
                    a.unrealized_pnl >= 0 ? "text-bull" : "text-bear"
                  }`}
                >
                  {a.unrealized_pnl >= 0 ? "+" : ""}
                  {a.unrealized_pnl.toFixed(2)}
                </span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-neutral-300">{a.detail}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
