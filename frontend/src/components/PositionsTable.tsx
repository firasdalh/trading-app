import { useState } from "react";
import type { PositionView } from "../types";

// Broker prices come back as raw floats that carry IEEE-754 representation noise
// (e.g. 51664.00000000001, 25142.800000000003). Round to 12 significant figures to strip
// the noise, then drop trailing zeros — this keeps the right precision for every instrument
// (JPY 3dp, indices 1dp, 5-digit FX, sub-cent crypto) without hard-coding a decimal count.
function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  if (!Number.isFinite(v)) return String(v);
  return Number(v.toPrecision(12)).toString();
}

interface Props {
  positions: PositionView[] | null;
  onClose?: (p: PositionView) => Promise<void> | void;
  onSetSlTp?: (p: PositionView, sl: number | null, tp: number | null) => Promise<void> | void;
  onSelect?: (p: PositionView) => void;
}

// Broker's live open positions with editable SL/TP and a per-row Close.
export function PositionsTable({ positions, onClose, onSetSlTp, onSelect }: Props) {
  return (
    <div className="card">
      <div className="mb-2 text-sm font-semibold">Open Positions (broker)</div>
      {!positions || positions.length === 0 ? (
        <div className="text-sm text-neutral-500">No open positions.</div>
      ) : (
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full border-separate border-spacing-0 whitespace-nowrap text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-neutral-500">
              <tr>
                <th className="px-2 py-2">Symbol</th>
                <th className="px-2 py-2">Side</th>
                <th className="px-2 py-2 text-right">Qty</th>
                <th className="px-2 py-2 text-right" title="Margin required to hold the position (cost to open), in USD">
                  Cost
                </th>
                <th className="px-2 py-2 text-right">Entry</th>
                <th className="px-2 py-2 text-right">Last</th>
                <th className="px-2 py-2 pr-4 text-right">P&amp;L</th>
                <th className="px-2 py-2">Stop</th>
                <th className="px-2 py-2">Target</th>
                <th className="px-2 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow key={p.id} p={p} onClose={onClose} onSetSlTp={onSetSlTp} onSelect={onSelect} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PositionRow({
  p,
  onClose,
  onSetSlTp,
  onSelect,
}: {
  p: PositionView;
  onClose?: (p: PositionView) => Promise<void> | void;
  onSetSlTp?: (p: PositionView, sl: number | null, tp: number | null) => Promise<void> | void;
  onSelect?: (p: PositionView) => void;
}) {
  const [sl, setSl] = useState(p.stop_loss != null ? fmtPrice(p.stop_loss) : "");
  const [tp, setTp] = useState(p.take_profit != null ? fmtPrice(p.take_profit) : "");
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<void> | void) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  const missingProtection = p.stop_loss == null && p.take_profit == null;

  return (
    <tr className="border-t border-neutral-800 [&>td]:px-2 [&>td]:py-2 [&>td]:align-middle">
      <td className="font-medium">
        {onSelect ? (
          <button
            onClick={() => onSelect(p)}
            className="hover:text-blue-400 hover:underline"
            title={`Open ${p.symbol} on the chart`}
          >
            {p.symbol}
          </button>
        ) : (
          p.symbol
        )}
      </td>
      <td className={`uppercase ${p.direction === "long" ? "text-bull" : "text-bear"}`}>
        {p.direction}
      </td>
      <td className="text-right tabular-nums">{p.qty}</td>
      <td
        className="text-right tabular-nums text-neutral-300"
        title="Margin required to hold the position (cost to open), in USD"
      >
        {p.cost_usd != null ? `$${p.cost_usd.toFixed(2)}` : "—"}
      </td>
      <td className="text-right tabular-nums">{fmtPrice(p.entry_price)}</td>
      <td className="text-right tabular-nums">{fmtPrice(p.last_price)}</td>
      <td
        className={`pr-4 text-right tabular-nums ${p.unrealized_pnl >= 0 ? "text-bull" : "text-bear"}`}
      >
        {p.unrealized_pnl >= 0 ? "+" : ""}
        {p.unrealized_pnl.toFixed(2)}
      </td>
      <td>
        <input
          value={sl}
          onChange={(e) => setSl(e.target.value)}
          placeholder="SL"
          inputMode="decimal"
          className={`w-20 rounded bg-neutral-800 px-2 py-1 text-xs tabular-nums ${
            missingProtection ? "ring-1 ring-warn/50" : ""
          }`}
        />
      </td>
      <td>
        <input
          value={tp}
          onChange={(e) => setTp(e.target.value)}
          placeholder="TP"
          inputMode="decimal"
          className="w-20 rounded bg-neutral-800 px-2 py-1 text-xs tabular-nums"
        />
      </td>
      <td className="text-right">
        <div className="flex items-center justify-end gap-2">
          {onSetSlTp && (
            <button
              disabled={busy}
              onClick={() => run(() => onSetSlTp(p, sl ? Number(sl) : null, tp ? Number(tp) : null))}
              className="btn bg-neutral-700 text-white hover:bg-neutral-600"
              title="Attach/modify stop-loss and take-profit on this position"
            >
              Set
            </button>
          )}
          {onClose && (
            <button
              disabled={busy}
              onClick={() => run(() => onClose(p))}
              className="btn border border-bear/50 bg-bear/10 text-bear hover:bg-bear/20"
            >
              Close
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}
