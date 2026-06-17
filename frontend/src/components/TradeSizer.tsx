import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import type { SizePreviewResponse } from "../types";

// Lot input + live dollar figures (risk / potential reward / cost) for a proposal. Adjust the lot
// and the dollars update (broker-computed via size_preview; the backend clamps to the 2% per-trade
// cap). Reports the chosen lot up to the parent so Approve uses it.
export function TradeSizer({ proposalId, entry, stopLoss, takeProfit, onLots }: {
  proposalId: number;
  entry: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  onLots: (lots: number | null) => void;
}) {
  const [lots, setLots] = useState("");
  const [riskUsd, setRiskUsd] = useState<number | null>(null);
  const [marginUsd, setMarginUsd] = useState<number | null>(null);
  const [maxLots, setMaxLots] = useState<number | null>(null);
  const [capped, setCapped] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const apply = (r: SizePreviewResponse) => {
    setRiskUsd(r.risk?.risk_amount ?? null);
    setMarginUsd(r.economics?.margin_usd ?? null);
    setMaxLots(r.max_lots ?? null);
    setCapped(r.capped);
    const lv = r.economics?.lots ?? r.risk?.approved_qty ?? null;
    if (lv != null) {
      setLots(String(lv));
      onLots(lv);
    }
  };

  // AI default size + economics on mount.
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    api.sizePreview(proposalId, null)
      .then((r) => { if (!cancelled) apply(r); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [proposalId]);

  // Re-price at the user's lot (debounced so typing doesn't spam the broker).
  const reprice = (val: string) => {
    const n = Number(val);
    onLots(Number.isFinite(n) && n > 0 ? n : null);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      setBusy(true);
      try {
        apply(await api.sizePreview(proposalId, Number.isFinite(n) && n > 0 ? n : null));
      } catch {
        /* ignore — keep last good figures */
      } finally {
        setBusy(false);
      }
    }, 500);
  };

  // Potential reward in $: reward and risk scale by the same lot×point factor, so reward = risk×R.
  const rr = entry != null && stopLoss != null && takeProfit != null && entry !== stopLoss
    ? Math.abs(takeProfit - entry) / Math.abs(entry - stopLoss)
    : null;
  const rewardUsd = riskUsd != null && rr != null ? riskUsd * rr : null;

  return (
    <div className="mt-2 rounded border border-neutral-800 bg-neutral-950/40 px-2 py-2">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-neutral-400">
          <div className="mb-1">Size (lots)</div>
          <input
            name={`lots-${proposalId}`}
            autoComplete="off"
            inputMode="decimal"
            value={lots}
            onChange={(e) => { setLots(e.target.value); reprice(e.target.value); }}
            className="w-24 rounded bg-neutral-800 px-2 py-1 text-sm tabular-nums text-neutral-100"
          />
          {maxLots != null && <div className="mt-0.5 text-[10px] text-neutral-600">max {maxLots} (2% cap)</div>}
        </label>
        <Stat label="Risk" value={fmtUsd(riskUsd)} cls="text-bear" />
        <Stat label="Reward" value={fmtUsd(rewardUsd)} cls="text-bull" />
        <Stat label="Cost (margin)" value={fmtUsd(marginUsd)} />
        {busy && <span className="pb-1 text-[10px] text-neutral-500">…</span>}
      </div>
      {capped && (
        <div className="mt-1 text-[10px] text-warn">Capped to the 2% per-trade limit.</div>
      )}
    </div>
  );
}

function Stat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div>
      <div className="text-[10px] text-neutral-500">{label}</div>
      <div className={`text-sm tabular-nums ${cls ?? "text-neutral-200"}`}>{value}</div>
    </div>
  );
}
