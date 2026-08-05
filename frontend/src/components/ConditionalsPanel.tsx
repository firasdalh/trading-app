import { useState } from "react";
import { api } from "../api/client";
import { fmtPrice } from "../format";
import { usePolling } from "../hooks/usePolling";
import { ago, localTime, until } from "./advisorFormat";
import { TradeSizer } from "./TradeSizer";
import type { ConditionalSetupView } from "../types";

const STATUS_STYLE: Record<string, string> = {
  armed: "text-amber-300",
  triggered: "text-bull",
  rejected: "text-bear",
  expired: "text-neutral-500",
  cancelled: "text-neutral-500",
  invalidated: "text-bear",
};

// Armed / pending ('wait for the break') setups. On a confirmed trigger the system re-checks the
// trade and only then opens it (Hybrid / Modes B-C) or queues it for approval (Mode A).
export function ConditionalsPanel({ onSelect }: {
  onSelect?: (p: { symbol: string; asset_class: string }) => void;  // open on the chart
} = {}) {
  const [bump, setBump] = useState(0);
  const { data } = usePolling(() => api.conditionals(), 10000, [bump]);
  const items: ConditionalSetupView[] = data ?? [];
  const armed = items.filter((i) => i.status === "armed");
  const finished = items.filter((i) => i.status !== "armed");
  const visible = [...armed, ...finished.slice(0, 5)];

  const cancel = async (id: number) => {
    try {
      await api.cancelConditional(id);
      setBump((b) => b + 1);
    } catch {
      /* ignore — the next poll reflects the true state */
    }
  };

  const clearFinished = async () => {
    try {
      await api.clearFinishedConditionals();
      setBump((b) => b + 1);
    } catch {
      /* ignore */
    }
  };

  if (visible.length === 0) {
    // Placeholder so the side-by-side column isn't blank when nothing is armed.
    return (
      <div className="card">
        <div className="mb-1 text-sm font-semibold">Armed / pending setups</div>
        <p className="text-xs text-neutral-500">
          No armed setups. The Hybrid arms “wait for the break” setups here, or you can arm one from
          a scan/analysis suggestion — its trigger then shows on the chart.
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-sm font-semibold">Armed / pending setups</span>
        <span className="text-xs text-neutral-500">· {armed.length} armed</span>
        {finished.length > 0 && (
          <button
            onClick={clearFinished}
            className="ml-auto text-xs text-neutral-500 hover:text-neutral-300"
            title="Remove finished (cancelled / rejected / expired / triggered) — armed stay"
          >
            Clear finished
          </button>
        )}
      </div>
      <p className="mb-2 text-xs text-neutral-500">
        “Wait for the break” entries. On a confirmed trigger the system re-checks the trade and only
        then opens it (or queues it for approval). All risk gates still apply.
      </p>
      <div className="space-y-2">
        {visible.map((s) => (
          <div key={s.id} className="rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <button
                onClick={() => onSelect?.({ symbol: s.symbol, asset_class: s.asset_class })}
                className="font-semibold hover:text-blue-400 hover:underline"
                title="Open on the chart"
              >
                {s.symbol}
              </button>
              <span className="text-xs text-neutral-500">{s.timeframe}</span>
              <span className={`text-[10px] font-bold uppercase ${s.direction === "long" ? "text-bull" : "text-bear"}`}>
                {s.direction}
              </span>
              <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] uppercase text-neutral-300">
                {s.order_type.replace("_", " ")}
              </span>
              <span className={`text-[10px] font-bold uppercase ${STATUS_STYLE[s.status] ?? ""}`}>{s.status}</span>
              {s.source === "hybrid" && (
                <span className="rounded bg-brand-600/30 px-1 py-0.5 text-[10px] text-blue-200">hybrid</span>
              )}
              <span className="text-[10px] text-neutral-500" title={`armed ${localTime(s.created_at)}`}>
                armed {ago(s.created_at)}
                {s.status === "armed" && s.valid_until ? ` · expires ${until(s.valid_until)}` : ""}
              </span>
              {s.status === "armed" && (
                <button onClick={() => cancel(s.id)} className="ml-auto text-xs text-neutral-500 hover:text-bear">
                  cancel
                </button>
              )}
            </div>
            <div className="mt-1 text-xs tabular-nums text-neutral-400">
              trigger {fmtPrice(s.trigger_price)} · SL <span className="text-bear">{fmtPrice(s.stop_loss)}</span> · TP{" "}
              <span className="text-bull">{fmtPrice(s.take_profit)}</span>
              {s.rr != null ? ` · ~${s.rr.toFixed(1)}R` : ""} · conf {(s.confidence * 100).toFixed(0)}%
            </div>
            {s.status === "armed" && s.break_level != null && (
              <RetestStages setup={s} />
            )}
            {s.status === "armed" && s.current_price != null && (
              <div className="mt-0.5 text-xs tabular-nums text-amber-300/90">
                now {fmtPrice(s.current_price)} ·{" "}
                {s.pips_to_trigger != null
                  ? `${s.pips_to_trigger} pips`
                  : s.pct_to_trigger != null
                    ? `${s.pct_to_trigger}%`
                    : "—"}{" "}
                to trigger
              </div>
            )}
            {s.status === "armed" && (
              <TradeSizer
                preview={(l) => api.conditionalSizePreview(s.id, l)}
                onCommit={(l) => { void api.setConditionalLots(s.id, l).catch(() => {}); }}
                entry={s.trigger_price}
                stopLoss={s.stop_loss}
                takeProfit={s.take_profit}
                initialLots={s.desired_lots}
              />
            )}
            {s.last_note && <div className="mt-0.5 text-xs text-neutral-500">{s.last_note}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

// Two-stage progress for a BREAK-AND-RETEST arm.
//
// These setups do nothing until a level CLOSE-breaks, and only then place their limit back at it.
// Without this the panel just shows a buy_limit sitting under resistance, which looks like the
// engine is doing the opposite of what it intends — so the stage has to be visible, not buried in
// the note text. The completed stage is dimmed, the live one highlighted.
function RetestStages({ setup }: { setup: ConditionalSetupView }) {
  const broken = setup.break_confirmed_at != null;
  const done = "text-neutral-500";
  const live = "text-amber-300 font-semibold";
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] tabular-nums">
      <span
        className={broken ? done : live}
        title={
          broken
            ? `Broke ${localTime(setup.break_confirmed_at)}`
            : "Nothing can trigger until this level closes through — a wick past it is the fake break itself"
        }
      >
        {broken ? "✓" : "①"} break {fmtPrice(setup.break_level)}
      </span>
      <span className="text-neutral-600">→</span>
      <span
        className={broken ? live : done}
        title={
          broken
            ? "The break held. Now waiting for price to come back to the level and enter there."
            : "Entry price once the break is confirmed — dormant until then"
        }
      >
        {broken ? "②" : "②"} retest {fmtPrice(setup.trigger_price)}
      </span>
      <span className="text-neutral-500">
        {broken ? "· waiting for the pullback" : "· not live yet"}
      </span>
    </div>
  );
}
