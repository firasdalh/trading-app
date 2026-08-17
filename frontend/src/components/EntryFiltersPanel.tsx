import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { DetFiltersView } from "../types";

// Control panel for the DETERMINISTIC engine's entry checklist — toggle each filter on/off.
// Applies to Run analysis, the watchlist scan, and the deterministic Hybrid path. All on = the
// tuned default; turning one off makes the engine less strict on that check.
export function EntryFiltersPanel() {
  const [cfg, setCfg] = useState<DetFiltersView | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);   // collapsed by default — advanced/rarely-touched controls
  // MUST sit with the other hooks, above the `if (!cfg) return null` below. Declared after that
  // early return it only ran once config had loaded, so the hook COUNT changed between renders —
  // React error #310, and a blank panel.
  const [showOff, setShowOff] = useState(false);

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
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full flex-wrap items-baseline gap-x-2 gap-y-0.5 text-left"
        title={open ? "Hide the entry filters" : "Show the entry filters"}
      >
        <span className="text-neutral-500">{open ? "▾" : "▸"}</span>
        <span className="whitespace-nowrap text-sm font-semibold">⚙️ Deterministic entry filters</span>
        <span className="text-xs text-neutral-500">
          {active}/{cfg.filters.length} active · the entry checklist the deterministic engine applies (structure → R:R)
        </span>
      </button>

      {open && (<>
      {/* Active filters first, switched-off ones folded away below. With 24 entries the list was a
          wall of checkboxes where the handful you actually run got no more prominence than the ones
          you deliberately turned off after they failed a backtest.
          The off ones are KEPT, not deleted: this session alone, `failed_break` went from "known
          loser" to the best out-of-sample strategy in the book. Code you can re-measure is how that
          gets found; code you deleted is not. */}
      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {cfg.filters.filter((f) => !disabled.has(f.key)).map((f) => {
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

      {cfg.disabled.length > 0 && (
        <div className="mt-3 border-t border-neutral-800 pt-2">
          <button
            onClick={() => setShowOff((v) => !v)}
            className="text-[11px] text-neutral-500 hover:text-neutral-300"
          >
            {showOff ? "▾" : "▸"} {cfg.disabled.length} switched off — kept so they can be re-measured
          </button>
          {showOff && (
            <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {cfg.filters.filter((f) => disabled.has(f.key)).map((f) => (
                <label
                  key={f.key}
                  title={f.desc}
                  className="flex cursor-pointer items-start gap-2 rounded-md border border-neutral-800 bg-neutral-900/30 px-2.5 py-1.5 opacity-70"
                >
                  <input
                    type="checkbox"
                    checked={false}
                    disabled={busy}
                    onChange={() => toggle(f.key)}
                    className="mt-0.5 h-3.5 w-3.5 accent-emerald-500"
                  />
                  <span className="min-w-0">
                    <span className="text-xs font-medium text-neutral-500">{f.label}</span>
                    <span className="block text-[10px] leading-tight text-neutral-600">{f.desc}</span>
                  </span>
                </label>
              ))}
            </div>
          )}
        </div>
      )}

      <p className="mt-2 text-[11px] leading-snug text-neutral-600">
        All on = the tuned default. Turning a filter off makes the engine less strict on that check — it
        affects Run analysis, the scan, and the deterministic Hybrid path (not RSI-Over / SuperTrend, which
        have their own rules). Validate changes on the Backtest before trusting them live.
      </p>
      </>)}
    </div>
  );
}
