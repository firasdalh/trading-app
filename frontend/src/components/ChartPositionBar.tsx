import { useEffect, useState } from "react";
import { fmtPrice, fmtUsd } from "../format";
import type { Pulse } from "./Chart";
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
  pulse,
  onClose,
}: {
  pos: PositionView | null;
  pulse?: Pulse | null;
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

      {/* Live read for the trade you're IN. Each chip is judged against YOUR side, so green always
          means "this supports the position you are holding" and red always means "this argues for
          getting out" — no mental translation between a long and a short. */}
      {pulse && pulse.items.length > 0 && (
        <div className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 border-t border-neutral-800 pt-1.5 text-xs">
          <span
            className={`rounded px-1.5 py-0.5 font-bold ${
              pulse.againstCount === 0
                ? "bg-bull/20 text-bull"
                : pulse.againstCount >= pulse.withCount
                  ? "bg-bear/20 text-bear"
                  : "bg-warn/20 text-warn"
            }`}
            title="How many of the four readings still support the position you're holding. All four against you is the clearest exit signal this bar can give."
          >
            {pulse.withCount}/{pulse.items.length} with you
          </span>

          {pulse.items.map((it) => (
            <span
              key={it.label}
              title={it.tip}
              className={`cursor-help rounded px-1.5 py-0.5 tabular-nums ${
                it.verdict === "with"
                  ? "bg-bull/10 text-bull"
                  : it.verdict === "against"
                    ? "bg-bear/10 text-bear"
                    : "bg-neutral-800 text-neutral-400"
              }`}
            >
              <span className="opacity-70">{it.label}</span> {it.value}
            </span>
          ))}

          {pulse.rMultiple != null && (
            <span
              className="text-neutral-400"
              title="Profit measured in R — multiples of the risk you took. Dollars don't compare across pairs or sizes; R does, and every scale-out rule is written in it."
            >
              <span className={`font-semibold tabular-nums ${pulse.rMultiple >= 0 ? "text-bull" : "text-bear"}`}>
                {pulse.rMultiple >= 0 ? "+" : ""}{pulse.rMultiple.toFixed(2)}R
              </span>
            </span>
          )}

          {pulse.stopAtr != null && (
            <span
              className={`tabular-nums ${pulse.stopAtr < 1 ? "text-warn" : "text-neutral-500"}`}
              title={
                pulse.stopAtr < 1
                  ? `Your stop is only ${pulse.stopAtr.toFixed(1)} ATR away — inside one bar's normal range, so ordinary noise can take it out without the idea being wrong.`
                  : `Your stop sits ${pulse.stopAtr.toFixed(1)} ATR away — outside normal single-bar noise.`
              }
            >
              stop {pulse.stopAtr.toFixed(1)} ATR
            </span>
          )}
        </div>
      )}
    </div>
  );
}
