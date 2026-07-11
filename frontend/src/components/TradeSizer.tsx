import { useEffect, useRef, useState } from "react";
import { fmtUsd } from "../format";
import type { SizePreviewResponse } from "../types";

// Lot input + live dollar figures (risk / potential reward / cost). Adjust the lot and the dollars
// update (broker-computed via `preview`; the backend clamps to the 3% per-trade cap). `onLots` fires
// immediately (parent tracks the chosen lot, e.g. for Approve). When `onCommit` is provided a Save
// button persists the lot (e.g. on an armed setup). Works for proposals or armed conditionals.
export function TradeSizer({ preview, entry, stopLoss, takeProfit, onLots, onCommit, initialLots }: {
  preview: (lots: number | null) => Promise<SizePreviewResponse>;
  entry: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  onLots?: (lots: number | null) => void;
  onCommit?: (lots: number | null) => void;
  initialLots?: number | null;   // seed from a previously-saved lot (else the AI default)
}) {
  const [lots, setLots] = useState("");
  const [notionalUsd, setNotionalUsd] = useState<number | null>(null);
  const [riskAmt, setRiskAmt] = useState<number | null>(null);   // fallback when notional missing
  const [marginUsd, setMarginUsd] = useState<number | null>(null);
  const [maxLots, setMaxLots] = useState<number | null>(null);
  const [capped, setCapped] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Latest preview closure in a ref so a new closure each render doesn't re-fire the mount fetch.
  const previewRef = useRef(preview);
  previewRef.current = preview;

  // Update only the dollar figures — never the lot input (so a re-price doesn't fight typing).
  const applyFigures = (r: SizePreviewResponse) => {
    setNotionalUsd(r.economics?.notional_usd ?? null);
    setRiskAmt(r.risk?.risk_amount ?? null);
    setMarginUsd(r.economics?.margin_usd ?? null);
    setMaxLots(r.max_lots ?? null);
    setCapped(r.capped);
  };

  // On mount, price at the previously-saved lot if there is one, else the AI default size — and
  // seed the input ONCE from that (the only time we set the lot value programmatically).
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    previewRef.current(initialLots && initialLots > 0 ? initialLots : null)
      .then((r) => {
        if (cancelled) return;
        applyFigures(r);
        const lv = r.economics?.lots ?? r.risk?.approved_qty ?? null;
        if (lv != null) {
          setLots(String(lv));
          onLots?.(lv);
        }
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-price at the user's lot (debounced so typing doesn't spam the broker). Updates the $ figures
  // only — the input keeps exactly what you typed (the backend re-clamps to the cap at save/fire).
  const reprice = (val: string) => {
    const n = Number(val);
    const lv = Number.isFinite(n) && n > 0 ? n : null;
    setSaved(false);
    onLots?.(lv);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      setBusy(true);
      try {
        applyFigures(await previewRef.current(lv));
      } catch {
        /* ignore — keep last good figures */
      } finally {
        setBusy(false);
      }
    }, 500);
  };

  const save = () => {
    const n = Number(lots);
    onCommit?.(Number.isFinite(n) && n > 0 ? n : null);
    setSaved(true);
  };

  // Risk / reward in $ derived from the notional exposure + the levels — robust even when the
  // CURRENT decision is gated (e.g. a position is open), which is fine for a future conditional
  // entry. (risk = notional × stop-distance / entry; reward = notional × target-distance / entry.)
  const dist = (a: number, b: number) => Math.abs(a - b);
  const rr = entry != null && stopLoss != null && takeProfit != null && entry !== stopLoss
    ? dist(takeProfit, entry) / dist(entry, stopLoss)
    : null;
  const riskUsd = notionalUsd != null && entry && stopLoss != null
    ? notionalUsd * dist(entry, stopLoss) / entry
    : riskAmt;
  const rewardUsd = notionalUsd != null && entry && takeProfit != null
    ? notionalUsd * dist(takeProfit, entry) / entry
    : riskUsd != null && rr != null ? riskUsd * rr : null;

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
          {maxLots != null && <div className="mt-0.5 text-[10px] text-neutral-600">max {maxLots} (3% cap)</div>}
        </label>
        <Stat label="Risk" value={fmtUsd(riskUsd)} cls="text-bear" />
        <Stat label="Reward" value={fmtUsd(rewardUsd)} cls="text-bull" />
        <Stat label="Cost (margin)" value={fmtUsd(marginUsd)} />
        {busy && <span className="pb-1 text-[10px] text-neutral-500">…</span>}
        {onCommit && (
          <button
            onClick={save}
            disabled={busy}
            className="btn ml-auto bg-brand-600/80 text-xs text-white hover:bg-brand-600"
            title="Use this lot when the setup fires"
          >
            {saved ? "Saved ✓" : "Save lot"}
          </button>
        )}
      </div>
      {capped && (
        <div className="mt-1 text-[10px] text-warn">Capped to the 3% per-trade limit.</div>
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
