import { useState } from "react";
import { api } from "../api/client";
import { fmtPrice } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { ConditionalSetupView } from "../types";

const STATUS_STYLE: Record<string, string> = {
  armed: "text-amber-300",
  triggered: "text-bull",
  rejected: "text-bear",
  expired: "text-neutral-500",
  cancelled: "text-neutral-500",
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
  const others = items.filter((i) => i.status !== "armed").slice(0, 5);
  const visible = [...armed, ...others];

  const cancel = async (id: number) => {
    try {
      await api.cancelConditional(id);
      setBump((b) => b + 1);
    } catch {
      /* ignore — the next poll reflects the true state */
    }
  };

  if (visible.length === 0) return null; // nothing armed yet — keep the dashboard clean

  return (
    <div className="card">
      <div className="mb-1 text-sm font-semibold">
        Armed / pending setups <span className="text-xs text-neutral-500">· {armed.length} armed</span>
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
                <span className="rounded bg-blue-600/30 px-1 py-0.5 text-[10px] text-blue-200">hybrid</span>
              )}
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
            {s.last_note && <div className="mt-0.5 text-xs text-neutral-500">{s.last_note}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}
