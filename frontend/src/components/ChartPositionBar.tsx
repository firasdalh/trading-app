import { useEffect, useState } from "react";
import { fmtPrice, fmtUsd } from "../format";
import type { PositionView } from "../types";

// The account-currency value an open position would show AT a price level (its SL/TP) and now.
// Derived from the live floating P&L per price unit — which already encodes lot size, contract size
// and FX conversion — so it's exact in USD. null until price moves off entry (ratio undefined there).
function usdAtLevel(pos: PositionView, level: number | null | undefined): number | null {
  if (level == null || pos.last_price == null || pos.last_price === pos.entry_price) return null;
  const perPrice = pos.unrealized_pnl / (pos.last_price - pos.entry_price);
  if (!isFinite(perPrice)) return null;
  return (level - pos.entry_price) * perPrice;
}

// Resume of the open position for the charted symbol, sitting on top of the chart: direction/entry,
// current P&L, risk ($ to SL) and reward ($ to TP) in dollars, R:R, and a quick (confirmed) Close.
export function ChartPositionBar({
  pos,
  onClose,
}: {
  pos: PositionView | null;
  onClose: (p: PositionView) => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  // Auto-cancel the confirm if it isn't acted on quickly, so a stray click can't sit armed and get
  // confirmed by accident later (this closes a real position).
  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 5000);
    return () => clearTimeout(t);
  }, [confirming]);

  if (!pos) return null;

  const isLong = pos.direction === "long";
  const riskUsd = usdAtLevel(pos, pos.stop_loss);       // negative (loss at the stop)
  const rewardUsd = usdAtLevel(pos, pos.take_profit);   // positive (gain at the target)
  const rr = riskUsd && rewardUsd && riskUsd !== 0 ? Math.abs(rewardUsd / riskUsd) : null;
  const isBE =
    pos.stop_loss != null &&
    Math.abs(pos.stop_loss - pos.entry_price) <= Math.abs(pos.entry_price) * 1e-4;

  const close = async () => {
    setBusy(true);
    try {
      await onClose(pos);
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border border-neutral-700 bg-neutral-900/60 px-3 py-2 text-sm">
      <span
        className={`rounded px-1.5 py-0.5 text-xs font-bold ${
          isLong ? "bg-bull/20 text-bull" : "bg-bear/20 text-bear"
        }`}
      >
        {isLong ? "▲ LONG" : "▼ SHORT"} @ {fmtPrice(pos.entry_price)}
      </span>

      <span className="text-neutral-400">
        P&L{" "}
        <span className={`font-semibold tabular-nums ${pos.unrealized_pnl >= 0 ? "text-bull" : "text-bear"}`}>
          {fmtUsd(pos.unrealized_pnl, { sign: true })}
        </span>
      </span>

      <span className="text-neutral-400">
        Risk{" "}
        <span className="tabular-nums text-neutral-300">
          {pos.stop_loss != null ? fmtPrice(pos.stop_loss) : "—"}
          {isBE && <span className="ml-1 text-bull">(BE)</span>}
        </span>
        {riskUsd != null && (
          <span className="ml-1 font-semibold tabular-nums text-bear">{fmtUsd(riskUsd, { sign: true })}</span>
        )}
      </span>

      <span className="text-neutral-400">
        Reward{" "}
        <span className="tabular-nums text-neutral-300">
          {pos.take_profit != null ? fmtPrice(pos.take_profit) : "—"}
        </span>
        {rewardUsd != null && (
          <span className="ml-1 font-semibold tabular-nums text-bull">{fmtUsd(rewardUsd, { sign: true })}</span>
        )}
      </span>

      {rr != null && (
        <span className="text-neutral-400">
          R:R <span className="font-semibold tabular-nums text-neutral-200">{rr.toFixed(2)}</span>
        </span>
      )}

      <span className="ml-auto">
        {confirming ? (
          <span className="flex items-center gap-2">
            <button
              disabled={busy}
              onClick={close}
              className="btn bg-bear text-white hover:bg-red-700"
              title={`Confirm closing ${pos.symbol} ${pos.direction.toUpperCase()}`}
            >
              {busy ? "Closing…" : "Confirm close"}
            </button>
            <button
              disabled={busy}
              onClick={() => setConfirming(false)}
              className="btn bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            className="btn border border-bear/50 bg-bear/10 text-bear hover:bg-bear/20"
            title={`Close ${pos.symbol} ${pos.direction.toUpperCase()} — asks to confirm`}
          >
            Quick close
          </button>
        )}
      </span>
    </div>
  );
}
