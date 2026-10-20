import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import { play } from "../hooks/useApprovalChime";
import { useLocalStorage } from "../hooks/useLocalStorage";
import type { AssetClass } from "../types";

interface Alert {
  id: string;
  symbol: string;
  asset_class: string;
  price: number;
  /** Fire when price goes "above" or "below" the level - fixed when the alert is created. */
  side: "above" | "below";
}

const POLL_MS = 10_000;

/**
 * Price alerts: be told when price reaches a level, WITHOUT placing an order.
 *
 * Arming a real order was the only way to act on a level unattended, and armed setups are the
 * worst-measured way into a trade here (armed_hybrid -0.28R). An alert just tells you, and you
 * decide (full review 2026-10-20). Stored in this browser; checked every 10 s while the app is
 * open; fires a desktop notification and the approval chime, then removes itself.
 */
export function PriceAlerts({ symbol, assetClass }: { symbol: string; assetClass: AssetClass }) {
  const [alerts, setAlerts] = useLocalStorage<Alert[]>("price.alerts", []);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const alertsRef = useRef(alerts);
  alertsRef.current = alerts;

  const add = async () => {
    const price = Number(draft);
    if (!Number.isFinite(price) || price <= 0) {
      setErr("Enter a price");
      return;
    }
    setErr(null);
    try {
      const q = await api.quote(symbol, assetClass);
      const side: Alert["side"] = price >= q.price ? "above" : "below";
      setAlerts([...alerts, { id: `${Date.now()}`, symbol, asset_class: assetClass, price, side }]);
      setDraft("");
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        void Notification.requestPermission();
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    const check = async () => {
      const list = alertsRef.current;
      if (!list.length) return;
      const fired: string[] = [];
      for (const a of list) {
        try {
          const q = await api.quote(a.symbol, a.asset_class as AssetClass);
          const hit = a.side === "above" ? q.price >= a.price : q.price <= a.price;
          if (!hit) continue;
          fired.push(a.id);
          const text = `${displaySymbol(a.symbol)} is ${a.side} ${fmtPrice(a.price)} (now ${fmtPrice(q.price)})`;
          play();
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            new Notification("Price alert", { body: text });
          } else {
            window.alert(`Price alert: ${text}`);
          }
        } catch {
          /* a quote hiccup: try again next round */
        }
      }
      if (fired.length) setAlerts(alertsRef.current.filter((a) => !fired.includes(a.id)));
    };
    const id = window.setInterval(() => void check(), POLL_MS);
    return () => window.clearInterval(id);
  }, [setAlerts]);

  return (
    <div className="space-y-1.5 px-3 py-2 text-xs">
      <div className="flex items-center gap-1.5">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
          inputMode="decimal"
          placeholder={`Alert ${displaySymbol(symbol)} at price…`}
          className="w-full rounded bg-neutral-800 px-2 py-1 text-neutral-200 placeholder:text-neutral-600"
        />
        <button onClick={() => void add()} className="btn btn-subtle px-2 py-1 text-xs">Add</button>
      </div>
      {err && <div className="text-bear">{err}</div>}
      {alerts.length === 0 ? (
        <div className="text-neutral-600">No alerts. An alert only notifies you - it never places an order.</div>
      ) : (
        <ul className="space-y-0.5">
          {alerts.map((a) => (
            <li key={a.id} className="flex items-center justify-between text-neutral-300">
              <span>
                {displaySymbol(a.symbol)} {a.side === "above" ? "↑ above" : "↓ below"} {fmtPrice(a.price)}
              </span>
              <button
                onClick={() => setAlerts(alerts.filter((x) => x.id !== a.id))}
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
