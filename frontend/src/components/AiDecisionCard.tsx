import type { AiDecision } from "../types";
import { fmtPrice } from "../format";

const MEDAL = ["🥇", "🥈"];

function dirArrow(d: string): string {
  if (d === "up" || d === "long") return "▲";
  if (d === "down" || d === "short") return "▼";
  return "→";
}

// A clear header pill for the AI's chosen action.
function ActionPill({ d }: { d: AiDecision }) {
  const map: Record<string, { label: string; cls: string }> = {
    open: {
      label: `OPEN ${(d.direction ?? "").toUpperCase()} NOW`,
      cls: d.direction === "short" ? "bg-bear/20 text-bear" : "bg-bull/20 text-bull",
    },
    arm: {
      label: `ARM ${(d.direction ?? "").toUpperCase()} (pending)`,
      cls: "bg-amber-500/20 text-amber-300",
    },
    stand_aside: { label: "STAND ASIDE", cls: "bg-neutral-700 text-neutral-300" },
    blocked: { label: "NO TRADE (blocked)", cls: "bg-bear/20 text-bear" },
  };
  const m = map[d.kind] ?? map.stand_aside;
  return <span className={`rounded px-2 py-0.5 text-xs font-bold ${m.cls}`}>{m.label}</span>;
}

/**
 * Renders the structured AI decision cleanly: the action, the trade levels, the chosen scenario,
 * both scenarios with odds, why it was chosen, and the risks — instead of one wall of text.
 */
export function AiDecisionCard({ d }: { d: AiDecision }) {
  const hasLevels = d.kind === "open" || d.kind === "arm";
  const orderLabel = d.order_type ? d.order_type.replace("_", "-") : null;

  return (
    <div className="rounded-lg border border-violet-800/50 bg-violet-950/15 p-3 text-sm">
      <div className="mb-2 flex items-center gap-2">
        <span className="font-semibold text-violet-300">🤖 AI decision</span>
        <ActionPill d={d} />
        {d.conviction != null && (
          <span className="ml-auto text-xs text-neutral-400">
            conviction <span className="font-semibold text-neutral-200">{Math.round(d.conviction * 100)}%</span>
          </span>
        )}
      </div>

      {/* Trade levels */}
      {hasLevels && (
        <div className="mb-2 grid grid-cols-4 gap-2 rounded bg-neutral-900/50 px-2 py-1.5 text-center text-xs">
          <div>
            <div className="text-[10px] uppercase text-neutral-500">{d.kind === "arm" ? "Trigger" : "Entry"}</div>
            <div className="tabular-nums text-neutral-100">{fmtPrice(d.entry ?? null)}</div>
            {orderLabel && <div className="text-[10px] text-amber-300/80">{orderLabel}</div>}
          </div>
          <div>
            <div className="text-[10px] uppercase text-neutral-500">Stop</div>
            <div className="tabular-nums text-bear">{fmtPrice(d.stop ?? null)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-neutral-500">Target</div>
            <div className="tabular-nums text-bull">{fmtPrice(d.target ?? null)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-neutral-500">R:R</div>
            <div className="tabular-nums font-semibold text-neutral-100">{d.rr != null ? `${d.rr}R` : "—"}</div>
          </div>
        </div>
      )}

      {d.kind === "blocked" && d.note && (
        <div className="mb-2 rounded border border-bear/40 bg-bear/10 px-2 py-1 text-xs text-bear">
          Blocked: {d.note}. The AI's read is below, but nothing was armed or opened.
        </div>
      )}

      {/* Scenarios the AI built */}
      {d.scenarios.length > 0 && (
        <div className="mb-2 space-y-1.5">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500">
            Scenarios the AI built
          </div>
          {d.scenarios.map((s, i) => {
            const chosen = s.label === d.chosen;
            return (
              <div
                key={i}
                className={`rounded border px-2 py-1 ${
                  chosen ? "border-violet-700/60 bg-violet-900/25" : "border-neutral-800 bg-neutral-900/40"
                }`}
              >
                <div className="flex items-center justify-between text-xs">
                  <span className="font-semibold text-neutral-100">
                    {MEDAL[i] ?? "•"} {s.label} <span className="text-neutral-500">{dirArrow(s.direction)}</span>
                  </span>
                  <span className="flex items-center gap-1.5 tabular-nums">
                    {s.rr != null && (
                      <span className={s.tradeable ? "text-bull" : "text-neutral-500 line-through"}>
                        {s.rr}R
                      </span>
                    )}
                    {s.tradeable === false && s.rr != null && <span className="text-neutral-600">thin</span>}
                    <span className={`font-bold ${chosen ? "text-violet-200" : "text-neutral-300"}`}>{s.prob}%</span>
                  </span>
                </div>
                <div className="my-1 h-1 w-full overflow-hidden rounded bg-neutral-800">
                  <div
                    className={chosen ? "h-full bg-violet-400" : "h-full bg-neutral-500"}
                    style={{ width: `${Math.max(0, Math.min(100, s.prob))}%` }}
                  />
                </div>
                {s.path && <div className="text-[11px] text-neutral-400">{s.path}</div>}
              </div>
            );
          })}
        </div>
      )}

      {/* Why the chosen scenario won */}
      {d.why_chosen && (
        <div className="mb-2 rounded border border-violet-800/40 bg-violet-950/30 px-2 py-1 text-xs text-violet-200">
          <span className="font-semibold">Why “{d.chosen}”: </span>
          {d.why_chosen}
        </div>
      )}

      {/* The plan in one line */}
      {d.summary && <p className="mb-2 text-xs leading-relaxed text-neutral-300">{d.summary}</p>}

      {/* Risks */}
      {d.risks.length > 0 && (
        <div className="text-[11px] text-neutral-400">
          <span className="font-semibold text-amber-300/90">⚠ Risks:</span>
          <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
            {d.risks.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
