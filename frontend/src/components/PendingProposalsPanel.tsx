import { useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { ProposalView } from "../types";

interface Props {
  onSelect?: (p: { symbol: string; asset_class: string }) => void; // open on the chart
  onChanged?: () => void; // refresh positions after approve/reject
}

const DIR: Record<string, { label: string; cls: string }> = {
  long: { label: "LONG", cls: "bg-bull/20 text-bull" },
  short: { label: "SHORT", cls: "bg-bear/20 text-bear" },
};

function ago(iso: string): string {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 60) return `${mins}m ago`;
  const h = Math.floor(mins / 60);
  return `${h}h ${mins % 60}m ago`;
}

// Proposals that passed the Risk Manager and are waiting for your approval (Mode A). Approve to
// open, or reject to discard. Stale ones auto-expire, so this list stays clean.
export function PendingProposalsPanel({ onSelect, onChanged }: Props) {
  const [bump, setBump] = useState(0);
  const { data } = usePolling(
    () => api.proposals({ status: "pending_approval", limit: 50 }),
    8000,
    [bump],
  );
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
  const refresh = () => setBump((b) => b + 1);

  const act = async (id: number, fn: () => Promise<unknown>) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
      refresh();
      onChanged?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // 409 = the proposal's status changed under us (rejected / expired / already acted on).
      // Refresh so the stale row drops off the list, and show a calm note, not the raw conflict.
      if (/409|cannot (approve|reject)/i.test(msg)) {
        setError("That setup is no longer pending (rejected, expired, or already acted on).");
      } else {
        setError(msg);
      }
      refresh();
      onChanged?.();
    } finally {
      setBusyId(null);
    }
  };

  const items: ProposalView[] = data ?? [];

  return (
    <div className="card">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-1 text-sm font-semibold hover:text-neutral-300"
          title={open ? "Collapse" : "Expand"}
        >
          <span className="text-xs text-neutral-500">{open ? "▾" : "▸"}</span>
          Pending approval
        </button>
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${
            items.length ? "bg-warn/20 text-warn" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          {items.length}
        </span>
        <span className="text-xs text-neutral-500">risk-approved setups waiting for you to approve</span>
      </div>

      {error && (
        <div className="mb-2 rounded border border-bear/40 bg-bear/10 px-2 py-1 text-xs text-bear">{error}</div>
      )}

      {!open ? null : items.length === 0 ? (
        <div className="text-sm text-neutral-500">No proposals awaiting approval.</div>
      ) : (
        <div className="max-h-[28rem] space-y-2 overflow-y-auto pr-1">
          {items.map((p) => {
            const dir = DIR[p.direction] ?? { label: p.direction.toUpperCase(), cls: "bg-neutral-800" };
            const busy = busyId === p.id;
            return (
              <div key={p.id} className="rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => onSelect?.({ symbol: p.symbol, asset_class: p.asset_class })}
                    className="font-semibold hover:text-blue-400 hover:underline"
                    title="Open on the chart"
                  >
                    {displaySymbol(p.symbol)}
                  </button>
                  <span className="text-xs text-neutral-500">{p.timeframe}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${dir.cls}`}>
                    {dir.label}
                  </span>
                  <span className="text-xs tabular-nums text-neutral-400">
                    conf {Math.round(p.confidence * 100)}%
                  </span>
                  <span className="text-xs text-neutral-600">{ago(p.created_at)}</span>
                  <div className="ml-auto flex gap-2">
                    <button
                      onClick={() => act(p.id, () => api.approve(p.id))}
                      disabled={busy}
                      className="btn bg-bull/20 text-bull hover:bg-bull/30"
                    >
                      {busy ? "…" : "Approve"}
                    </button>
                    <button
                      onClick={() => act(p.id, () => api.reject(p.id))}
                      disabled={busy}
                      className="btn bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
                    >
                      Reject
                    </button>
                  </div>
                </div>
                <div className="mt-1 text-xs tabular-nums text-neutral-400">
                  entry {fmtPrice(p.entry)} · SL <span className="text-bear">{fmtPrice(p.stop_loss)}</span> · TP{" "}
                  <span className="text-bull">{fmtPrice(p.take_profit)}</span>
                  {p.risk_amount != null && (
                    <span className="text-neutral-500"> · risk ${p.risk_amount.toFixed(2)}</span>
                  )}
                </div>
                {p.rationale && (
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500">{p.rationale}</p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
