import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { DetFiltersView } from "../types";

// Control panel for the DETERMINISTIC engine's entry checklist — toggle each filter on/off.
// Applies to Run analysis, the watchlist scan, and the deterministic Hybrid path. All on = the
// tuned default; turning one off makes the engine less strict on that check.
export function EntryFiltersPanel() {
  const [cfg, setCfg] = useState<DetFiltersView | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.detFilters().then(setCfg).catch(() => {});
  }, []);

  if (!cfg) return null;
  const disabled = new Set(cfg.disabled);

  const toggle = async (key: string) => {
    const next = new Set(disabled);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setBusy(true);
    try {
      setCfg(await api.setDetFilters([...next]));
    } catch {
      /* next load reflects the true state */
    } finally {
      setBusy(false);
    }
  };

  const active = cfg.filters.length - cfg.disabled.length;

  return (
    <div className="card">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="whitespace-nowrap text-sm font-semibold">⚙️ Deterministic entry filters</span>
        <span className="text-xs text-neutral-500">
          {active}/{cfg.filters.length} active · the entry checklist the deterministic engine applies (structure → R:R)
        </span>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {cfg.filters.map((f) => {
          const on = !disabled.has(f.key);
          return (
            <label
              key={f.key}
              title={f.desc}
              className={`flex cursor-pointer items-start gap-2 rounded-md border px-2.5 py-1.5 ${
                on ? "border-emerald-700/40 bg-emerald-900/10" : "border-neutral-800 bg-neutral-900/30"
              }`}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={busy}
                onChange={() => toggle(f.key)}
                className="mt-0.5 h-3.5 w-3.5 accent-emerald-500"
              />
              <span className="min-w-0">
                <span className={`text-xs font-medium ${on ? "text-neutral-100" : "text-neutral-500"}`}>{f.label}</span>
                <span className="block text-[10px] leading-tight text-neutral-500">{f.desc}</span>
              </span>
            </label>
          );
        })}
      </div>

      <p className="mt-2 text-[11px] leading-snug text-neutral-600">
        All on = the tuned default. Turning a filter off makes the engine less strict on that check — it
        affects Run analysis, the scan, and the deterministic Hybrid path (not RSI-Over / SuperTrend, which
        have their own rules). Validate changes on the Backtest before trusting them live.
      </p>
    </div>
  );
}
