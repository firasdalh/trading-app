import { useEffect, useSyncExternalStore } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import type { AssetClass } from "../types";
import { play } from "./useApprovalChime";

/**
 * Price alerts: be told when price reaches a level, WITHOUT placing an order.
 *
 * One shared list, so the chart can draw (and drag) them as lines while the sidebar lists them, and
 * one watcher - mounted once in App - checks them whichever view is open. Stored in this browser.
 * An alert fires once, plays the approval chime and a desktop notification, then removes itself
 * (and so its line). Set from the chart's 🔔 Alert tool or typed into the sidebar box.
 */
export interface PriceAlert {
  id: string;
  symbol: string;
  asset_class: string;
  price: number;
  /** Fire when price goes "above" or "below" the level - fixed when it is set (or moved). */
  side: "above" | "below";
  /** ms epoch; only bars from then on can trigger it (an older wick must not fire a new alert). */
  created_at?: number;
}

const KEY = "price.alerts";
const POLL_MS = 5_000;

function load(): PriceAlert[] {
  try {
    const raw = localStorage.getItem(KEY);
    const v = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

let alerts: PriceAlert[] = load();
const subs = new Set<() => void>();

function emit() {
  subs.forEach((f) => f());
}

export function getAlerts(): PriceAlert[] {
  return alerts;
}

export function setAlerts(next: PriceAlert[]) {
  alerts = next;
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable: the alerts still work until the page is closed */
  }
  emit();
}

function subscribe(f: () => void) {
  subs.add(f);
  // Another window of the app changed them: pick that up too.
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY) {
      alerts = load();
      f();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    subs.delete(f);
    window.removeEventListener("storage", onStorage);
  };
}

export function usePriceAlerts(): PriceAlert[] {
  return useSyncExternalStore(subscribe, getAlerts, getAlerts);
}

/** Add an alert at ``price``. ``current`` decides which way it fires; fetched when not given. */
export async function addAlert(symbol: string, assetClass: string, price: number, current?: number | null) {
  const now = current ?? (await api.quote(symbol, assetClass as AssetClass)).price;
  setAlerts([...alerts, {
    id: `${Date.now()}-${Math.round(Math.random() * 1e6)}`, symbol, asset_class: assetClass, price,
    side: price >= now ? "above" : "below", created_at: Date.now(),
  }]);
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    void Notification.requestPermission();
  }
}

/** Move an alert (dragged on the chart). The side is re-derived: the line may have crossed price. */
export function moveAlert(id: string, price: number, current: number | null) {
  setAlerts(alerts.map((a) => (a.id !== id ? a : {
    ...a, price, created_at: Date.now(),
    side: current == null ? a.side : price >= current ? "above" : "below",
  })));
}

/** Ask the chart to switch to its 🔔 Alert tool: the next click on the chart sets the alert there.
 *  (The sidebar's Add button with an empty box - nobody should have to type a price.) */
export const PICK_EVENT = "price-alert:pick";
export function requestChartPick() {
  window.dispatchEvent(new Event(PICK_EVENT));
}

export function removeAlert(id: string) {
  setAlerts(alerts.filter((a) => a.id !== id));
}

function fire(a: PriceAlert, now: number) {
  const text = `${displaySymbol(a.symbol)} reached ${fmtPrice(a.price)} (now ${fmtPrice(now)})`;
  play();
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    new Notification("Price alert", { body: text, requireInteraction: true });
  } else {
    window.alert(`Price alert: ${text}`);
  }
}

/**
 * Checks every alert every 5 s. It reads the last few 1-minute candles, not just the current price:
 * a spike that touches the level and comes back between two checks still has that high/low in its
 * candle, so it is not missed. Mount ONCE (App).
 */
export function useAlertWatcher() {
  useEffect(() => {
    let busy = false;
    const check = async () => {
      if (busy || !alerts.length) return;
      busy = true;
      try {
        const bySymbol = new Map<string, PriceAlert[]>();
        for (const a of alerts) {
          const k = `${a.symbol}|${a.asset_class}`;
          bySymbol.set(k, [...(bySymbol.get(k) ?? []), a]);
        }
        const fired: string[] = [];
        for (const list of bySymbol.values()) {
          const { symbol, asset_class } = list[0];
          let bars: { ts: string; high: number; low: number; close: number }[] = [];
          let last: number | null = null;
          try {
            bars = (await api.ohlcv(symbol, asset_class as AssetClass, "1m", 5)).candles;
            last = bars.length ? bars[bars.length - 1].close : null;
          } catch {
            /* fall back to the quote */
          }
          try {
            last = (await api.quote(symbol, asset_class as AssetClass)).price;
          } catch {
            /* a hiccup: try again next round */
          }
          for (const a of list) {
            // Only what traded AFTER this alert was set counts. A bar that started before it counts
            // by its close only (its earlier wick may predate the alert).
            const since = a.created_at ?? 0;
            let hi = last;
            let lo = last;
            for (const c of bars) {
              const t = Date.parse(c.ts);
              if (t + 60_000 <= since) continue;          // ended before the alert existed
              const whole = t >= since;
              const h = whole ? c.high : c.close;
              const l = whole ? c.low : c.close;
              hi = hi == null ? h : Math.max(hi, h);
              lo = lo == null ? l : Math.min(lo, l);
            }
            if (hi == null || lo == null) continue;
            const hit = a.side === "above" ? hi >= a.price : lo <= a.price;
            if (hit) {
              fired.push(a.id);
              fire(a, last ?? a.price);
            }
          }
        }
        if (fired.length) setAlerts(alerts.filter((a) => !fired.includes(a.id)));
      } finally {
        busy = false;
      }
    };
    const id = window.setInterval(() => void check(), POLL_MS);
    return () => window.clearInterval(id);
  }, []);
}
