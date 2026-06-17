import { useEffect, useRef, useState } from "react";
import { fmtUsd } from "../format";
import type { SizePreviewResponse } from "../types";

// Lot input + live dollar figures (risk / potential reward / cost). Adjust the lot and the dollars
// update (broker-computed via `preview`; the backend clamps to the 2% per-trade cap). `onLots` fires
// immediately (parent tracks the chosen lot, e.g. for Approve); `onCommit` fires debounced (e.g. to
// persist the lot on an armed setup). Works for any trade — proposals or armed conditionals.
export function TradeSizer({ preview, entry, stopLoss, takeProfit, onLots, onCommit }: {
  preview: (lots: number | null) => Promise<SizePreviewResponse>;
  entry: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  onLots?: (lots: number | null) => void;
  onCommit?: (lots: number | null) => void;
}) {
  const [lots, setLots] = useState("");
  const [riskUsd, setRiskUsd] = useState<number | null>(null);
  const [marginUsd, setMarginUsd] = useState<number | null>(null);
  const [maxLots, setMaxLots] = useState<number | null>(null);
  const [capped, setCapped] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Hold the latest preview closure in a ref so a new closure each render doesn't re-fire the mount
  // fetch (the effect runs once; reprice always calls the current closure).
  const previewRef = useRef(preview);
  previewRef.current = preview;

  const apply = (r: SizePreviewResponse) => {
    setRiskUsd(r.risk?.risk_amount ?? null);
    setMarginUsd(r.economics?.margin_usd ?? null);
    setMaxLots(r.max_lots ?? null);
    setCapped(r.capped);
    const lv = r.economics?.lots ?? r.risk?.approved_qty ?? null;
    if (lv != null) {
      setLots(String(lv));
      onLots?.(lv);
    }
  };

  // AI default size + economics on mount.
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    previewRef.current(null)
      .then((r) => { if (!cancelled) apply(r); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-price at the user's lot (debounced so typing doesn't spam the broker), then commit the lot.
  const reprice = (val: string) => {
    const n = Number(val);
    const lots = Number.isFinite(n) && n > 0 ? n : null;
    onLots?.(lots);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      setBusy(true);
      try {
        apply(await previewRef.current(lots));
        onCommit?.(lots);
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
            name="lots"
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
