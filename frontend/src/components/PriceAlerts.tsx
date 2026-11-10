import { useState } from "react";
import { displaySymbol, fmtPrice } from "../format";
import { addAlert, removeAlert, usePriceAlerts } from "../hooks/priceAlerts";
import type { AssetClass } from "../types";

/**
 * Price alerts: be told when price reaches a level, WITHOUT placing an order.
 *
 * Arming a real order was the only way to act on a level unattended, and armed setups are the
 * worst-measured way into a trade here (armed_hybrid -0.28R). An alert just tells you, and you
 * decide (full review 2026-10-20). Set one with the chart's 🔔 Alert button (then click the price; the
 * line stays on the chart and can be dragged) or type a price here. This sidebar's own "Pick on chart" went
 * 2026-11-08: it only switched the chart to that tool, from the bottom of the page. The checking lives in App
 * (useAlertWatcher), so alerts fire whichever view is open.
 */
export function PriceAlerts({ symbol, assetClass }: { symbol: string; assetClass: AssetClass }) {
  const alerts = usePriceAlerts();
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const add = async () => {
    if (!draft.trim()) return;   // nothing typed: the chart's 🔔 Alert button is the way to pick one
    const price = Number(draft);
    if (!Number.isFinite(price) || price <= 0) {
      setErr("Not a price - or press 🔔 Alert on the chart and click it there");
      return;
    }
    setErr(null);
    try {
      await addAlert(symbol, assetClass, price);
      setDraft("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  // This pair's alerts first: they are the lines on the chart in front of you.
  const sorted = [...alerts].sort((a, b) =>
    Number(b.symbol.toUpperCase() === symbol.toUpperCase()) - Number(a.symbol.toUpperCase() === symbol.toUpperCase()));

  return (
    <div className="space-y-1.5 px-3 py-2 text-xs">
      <div className="flex items-center gap-1.5">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
          inputMode="decimal"
          placeholder={`${displaySymbol(symbol)} price (optional)…`}
          className="w-full rounded bg-neutral-800 px-2 py-1 text-neutral-200 placeholder:text-neutral-600"
        />
        <button
          onClick={() => void add()}
          disabled={!draft.trim()}
          className="btn btn-subtle whitespace-nowrap px-2 py-1 text-xs disabled:opacity-40"
          title={draft.trim() ? "Add an alert at the typed price" : "Type a price - or press 🔔 Alert on the chart and click the price there"}
        >
          Add
        </button>
      </div>
      {err && <div className="text-bear">{err}</div>}
      {alerts.length === 0 ? (
        <div className="text-neutral-600">
          No alerts. Press <span className="text-neutral-400">🔔 Alert</span> on the chart, then click the price -
          or type one here. An alert only notifies you - it never places an order.
        </div>
      ) : (
        <ul className="space-y-0.5">
          {sorted.map((a) => (
            <li key={a.id} className="flex items-center justify-between text-neutral-300">
              <span>
                {displaySymbol(a.symbol)} {a.side === "above" ? "↑ above" : "↓ below"} {fmtPrice(a.price)}
              </span>
              <button
                onClick={() => removeAlert(a.id)}
                className="text-neutral-500 hover:text-bear"
                aria-label={`Remove the ${a.symbol} alert`}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
