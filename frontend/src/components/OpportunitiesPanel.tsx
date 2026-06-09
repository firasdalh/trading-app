import { useState } from "react";
import { api } from "../api/client";
import { fmtPrice } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { OpportunityView } from "../types";

interface Props {
  onSelect?: (o: { symbol: string; asset_class: string }) => void; // open on the chart
  onOpened?: () => void; // refresh positions after opening
}

const DIR: Record<string, { label: string; cls: string }> = {
  long: { label: "LONG", cls: "bg-bull/20 text-bull" },
  short: { label: "SHORT", cls: "bg-bear/20 text-bear" },
  no_trade: { label: "NO TRADE", cls: "bg-neutral-800 text-neutral-400" },
};

// Scan every watchlist pair at once and rank the best setups — so you don't check pair by pair.
export function OpportunitiesPanel({ onSelect, onOpened }: Props) {
  const [items, setItems] = useState<OpportunityView[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [openingKey, setOpeningKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [listOpen, setListOpen] = useState(true); // collapse the (long) results list

  const scan = async () => {
    setBusy(true);
    setError(null);
    try {
      setItems(await api.opportunities());
      setListOpen(true); // show results after a scan
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const open = async (o: OpportunityView) => {
    const key = `${o.symbol}-${o.timeframe}`;
    setOpeningKey(key);
    setError(null);
    try {
      const res = await api.analyze(o.symbol, o.asset_class, o.timeframe);
      // Mode A leaves it pending approval; approve to execute. Mode B already auto-executed.
      if (res.status === "pending_approval") await api.approve(res.proposal_id);
      onOpened?.();
      await scan(); // refresh the list (the pair is now open)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setOpeningKey(null);
    }
  };

  const actionableCount = items?.filter(
    (o) => (o.direction === "long" || o.direction === "short") && o.risk_approved && !o.already_open,
  ).length;

  return (
    <div className="card">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <button
          onClick={() => setListOpen((o) => !o)}
          className="flex items-center gap-1 text-sm font-semibold hover:text-neutral-300"
          title={listOpen ? "Collapse the results" : "Expand the results"}
          aria-expanded={listOpen}
        >
          <span className="text-xs text-neutral-500">{listOpen ? "▾" : "▸"}</span>
          Opportunities
        </button>
        <span className="text-xs text-neutral-500">scan all watchlist pairs &amp; rank the best</span>
        {items && (
          <span className="text-xs text-neutral-400">
            · {actionableCount} actionable / {items.length} scanned
          </span>
        )}
        <button
          onClick={scan}
          disabled={busy}
          className="btn ml-auto bg-blue-600 text-white hover:bg-blue-500"
        >
          {busy ? "Scanning…" : "Scan watchlist"}
        </button>
      </div>

      <HybridControl onOpened={onOpened} />

      {error && <div className="mb-2 rounded border border-bear/40 bg-bear/10 px-2 py-1 text-xs text-bear">{error}</div>}

      {!listOpen ? (
        items && (
          <div className="text-xs text-neutral-500">
            {items.length} setups hidden — click “Opportunities” to expand.
          </div>
        )
      ) : !items ? (
        <div className="text-sm text-neutral-500">
          Press <span className="font-semibold text-neutral-300">Scan watchlist</span> to analyze every
          pair and see the best setups ranked, with one-click open.
        </div>
      ) : items.length === 0 ? (
        <div className="text-sm text-neutral-500">No enabled watchlist pairs to scan.</div>
      ) : (
        <div className="max-h-[32rem] space-y-2 overflow-y-auto pr-1">
          {items.map((o, i) => {
            const dir = DIR[o.direction] ?? DIR.no_trade;
            const actionable = o.direction === "long" || o.direction === "short";
            const best = i === 0 && actionable && o.risk_approved && !o.already_open;
            const canOpen = actionable && o.risk_approved && !o.already_open;
            return (
              <div
                key={`${o.symbol}-${o.timeframe}`}
                className={`rounded-md border px-3 py-2 ${
                  best ? "border-blue-500/60 bg-blue-500/5" : "border-neutral-800 bg-neutral-900/40"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => onSelect?.({ symbol: o.symbol, asset_class: o.asset_class })}
                    className="font-semibold hover:text-blue-400 hover:underline"
                    title="Open on the chart"
                  >
                    {o.symbol}
                  </button>
                  <span className="text-xs text-neutral-500">{o.timeframe}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${dir.cls}`}>
                    {o.watch ? "WATCHING" : dir.label}
                  </span>
                  {actionable && (
                    <span className="text-xs tabular-nums text-neutral-400">
                      conf {(o.confidence * 100).toFixed(0)}%{o.rr ? ` · ${o.rr.toFixed(1)}R` : ""}
                    </span>
                  )}
                  {o.already_open && (
                    <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-400">
                      already open
                    </span>
                  )}
                  {best && (
                    <span className="rounded bg-blue-600 px-1.5 py-0.5 text-[10px] font-bold uppercase text-white">
                      best
                    </span>
                  )}
                  {canOpen && (
                    <button
                      onClick={() => open(o)}
                      disabled={openingKey !== null}
                      className="btn ml-auto bg-bull/20 text-bull hover:bg-bull/30"
                    >
                      {openingKey === `${o.symbol}-${o.timeframe}` ? "Opening…" : "Open"}
                    </button>
                  )}
                </div>
                {actionable && o.entry != null && (
                  <div className="mt-1 text-xs tabular-nums text-neutral-400">
                    entry {fmtPrice(o.entry)} · SL <span className="text-bear">{fmtPrice(o.stop_loss)}</span> · TP{" "}
                    <span className="text-bull">{fmtPrice(o.take_profit)}</span>
                  </div>
                )}
                <p className="mt-1 text-xs leading-relaxed text-neutral-500">
                  {!o.risk_approved && o.risk_reason ? `Risk: ${o.risk_reason}. ` : ""}
                  {o.rationale}
                </p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Hybrid auto-pilot toggle: one button. When on, every ~35 min it opens the single best
// watchlist setup above the confidence threshold if there's room — all risk gates still apply.
function HybridControl({ onOpened }: { onOpened?: () => void }) {
  const [bump, setBump] = useState(0);
  const { data: state } = usePolling(() => api.hybridState(), 15000, [bump]);
  const [busy, setBusy] = useState(false);
  const refresh = () => setBump((b) => b + 1);

  const on = state?.enabled ?? false;
  const intervalMin = state ? Math.round(state.interval_seconds / 60) : 35;
  const confPct = state ? Math.round(state.min_confidence * 100) : 70;

  const toggle = async () => {
    setBusy(true);
    try {
      await api.setHybridConfig({ enabled: !on });
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    try {
      await api.hybridRun();
      refresh();
      onOpened?.();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className={`mb-2 rounded-md border px-3 py-2 ${
        on ? "border-bull/50 bg-bull/5" : "border-neutral-800 bg-neutral-900/40"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">🤖 Hybrid auto-pilot</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
            on ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          {on ? "ON" : "OFF"}
        </span>
        <button
          onClick={toggle}
          disabled={busy}
          className={`btn ml-auto text-white ${on ? "bg-bear/80 hover:bg-bear" : "bg-bull/80 hover:bg-bull"}`}
        >
          {on ? "Turn off" : "Activate"}
        </button>
        {on && (
          <button onClick={runNow} disabled={busy} className="btn bg-neutral-700 text-white hover:bg-neutral-600">
            {busy ? "…" : "Run now"}
          </button>
        )}
      </div>
      <p className="mt-1 text-xs text-neutral-500">
        Every ~{intervalMin} min, if fewer than 3 trades are open, it scans the watchlist and
        auto-opens the single best setup above <span className="text-neutral-300">{confPct}%</span>{" "}
        confidence. Kill-switch, daily-loss, exposure & no-stacking limits still apply.
      </p>
      {on && state?.last_result && (
        <div className="mt-1 text-xs text-neutral-400">
          Last check{state.last_run_at ? ` (${new Date(state.last_run_at).toLocaleTimeString()})` : ""}:{" "}
          {state.last_result}
        </div>
      )}
    </div>
  );
}
