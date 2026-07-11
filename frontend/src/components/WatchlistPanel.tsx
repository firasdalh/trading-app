import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol } from "../format";
import type { AssetClass, WatchlistResponse } from "../types";

interface Props {
  currentSymbol: string;
  currentAsset: AssetClass;
  currentTimeframe: string;
  onSelect?: (it: { symbol: string; asset_class: string; timeframe: string }) => void;
}

// The watched pairs. The Hybrid auto-pilot scans these (the standalone auto-scanner toggle was
// removed — Hybrid is the one scanner now). Add/remove pairs and click one to open it on the chart.
export function WatchlistPanel({ currentSymbol, currentAsset, currentTimeframe, onSelect }: Props) {
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [busy, setBusy] = useState(false);
  // symbol (uppercase) -> instrument name, for hover tooltips. Fetched once per asset class.
  const [descs, setDescs] = useState<Record<string, string>>({});
  const fetchedAc = useRef<Set<string>>(new Set());

  const load = () => api.watchlist().then(setData).catch(() => {});

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  // Pull instrument names for the asset classes present in the watchlist (once each).
  useEffect(() => {
    if (!data) return;
    const need = [...new Set(data.items.map((i) => i.asset_class))].filter(
      (ac) => !fetchedAc.current.has(ac),
    );
    for (const ac of need) {
      fetchedAc.current.add(ac);
      api
        .symbols(ac as AssetClass)
        .then((r) =>
          setDescs((prev) => {
            const next = { ...prev };
            for (const [sym, name] of Object.entries(r.descriptions ?? {})) {
              next[sym.toUpperCase()] = name;
            }
            return next;
          }),
        )
        .catch(() => {});
    }
  }, [data]);

  const wrap = async (fn: () => Promise<WatchlistResponse>) => {
    setBusy(true);
    try {
      setData(await fn());
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold">Watchlist</div>
          <div className="text-xs text-neutral-500">
            Pairs the Hybrid auto-pilot scans. <span className="text-amber-400">★</span> = walk-forward
            validated core — add your own pairs anytime.
          </div>
        </div>
        <button
          disabled={busy}
          onClick={() => wrap(() => api.addWatch(currentSymbol, currentAsset, currentTimeframe))}
          className="btn bg-neutral-700 text-white hover:bg-neutral-600"
        >
          + Add {currentSymbol} ({currentTimeframe})
        </button>
      </div>

      {!data || data.items.length === 0 ? (
        <div className="text-sm text-neutral-500">
          No pairs watched. Add the current pair to start.
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {[...data.items]
            .sort(
              (a, b) =>
                Number(!!b.recommended) - Number(!!a.recommended) ||  // validated core first
                a.asset_class.localeCompare(b.asset_class) ||
                a.symbol.localeCompare(b.symbol) ||
                a.timeframe.localeCompare(b.timeframe),
            )
            .map((it) => {
              const active =
                it.symbol.toUpperCase() === currentSymbol.toUpperCase() &&
                it.timeframe === currentTimeframe;
              const name = descs[it.symbol.toUpperCase()];
              return (
                <span
                  key={it.id}
                  className={`flex items-center gap-2 rounded px-2 py-1 text-sm ${
                    active ? "bg-brand-600 text-white" : "bg-neutral-800"
                  }${it.recommended && !active ? " ring-1 ring-amber-500/50" : ""}`}
                >
                  {it.recommended && (
                    <span title="Walk-forward validated core pair" className="text-amber-400">★</span>
                  )}
                  <button
                    onClick={() => onSelect?.(it)}
                    className="font-medium hover:underline"
                    title={
                      name
                        ? `${name} — ${it.asset_class} · ${it.timeframe} (open on the chart)`
                        : `Open ${displaySymbol(it.symbol)} (${it.asset_class} · ${it.timeframe}) on the chart`
                    }
                  >
                    {displaySymbol(it.symbol)}
                  </button>
                  <span className={`text-xs ${active ? "text-blue-100" : "text-neutral-500"}`}>
                    {it.timeframe}
                  </span>
                  <button
                    disabled={busy}
                    onClick={() => wrap(() => api.removeWatch(it.id))}
                    className={`hover:text-bear ${active ? "text-blue-200" : "text-neutral-500"}`}
                    title="Remove from watchlist"
                  >
                    ✕
                  </button>
                </span>
              );
            })}
        </div>
      )}
    </div>
  );
}
