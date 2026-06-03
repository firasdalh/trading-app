import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AssetClass, ExecutionMode, WatchlistResponse } from "../types";

interface Props {
  currentSymbol: string;
  currentAsset: AssetClass;
  currentTimeframe: string;
  mode: ExecutionMode | undefined;
}

// Autonomous scanner control: the watched pairs, an on/off toggle, the scan interval, and
// last-scan time. The AI analyzes these pairs on a schedule and acts per execution mode.
export function WatchlistPanel({ currentSymbol, currentAsset, currentTimeframe, mode }: Props) {
  const [data, setData] = useState<WatchlistResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.watchlist().then(setData).catch(() => {});

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const wrap = async (fn: () => Promise<WatchlistResponse>) => {
    setBusy(true);
    try {
      setData(await fn());
    } finally {
      setBusy(false);
    }
  };

  const autoOpens = mode === "B_AUTO_PAPER" || mode === "C_AUTO_LIVE";

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold">Auto-scan watchlist</div>
        <label className="flex items-center gap-2 text-sm">
          <span className={data?.scan_enabled ? "text-bull" : "text-neutral-400"}>
            {data?.scan_enabled ? "ON" : "OFF"}
          </span>
          <button
            disabled={busy}
            onClick={() => wrap(() => api.setScanConfig({ enabled: !data?.scan_enabled }))}
            className={`relative h-5 w-10 rounded-full transition-colors ${
              data?.scan_enabled ? "bg-bull" : "bg-neutral-700"
            }`}
          >
            <span
              className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
                data?.scan_enabled ? "left-5" : "left-0.5"
              }`}
            />
          </button>
        </label>
      </div>

      {/* What the scanner will do, given the current mode */}
      <div
        className={`rounded-md border px-2 py-1.5 text-xs ${
          mode === "C_AUTO_LIVE"
            ? "border-bear/50 bg-bear/10 text-bear"
            : autoOpens
              ? "border-warn/50 bg-warn/10 text-warn"
              : "border-neutral-700 text-neutral-400"
        }`}
      >
        {mode === "C_AUTO_LIVE"
          ? "Mode C: scanner AUTO-OPENS live trades (real money) and the monitor auto-closes."
          : mode === "B_AUTO_PAPER"
            ? "Mode B: scanner auto-opens PAPER trades; the monitor auto-closes at stop/target."
            : "Mode A: scanner queues proposals for your approval. Switch to Mode B/C in Settings to auto-open."}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label className="flex items-center gap-1">
          <span className="text-xs text-neutral-400">every</span>
          <input
            type="number"
            min={20}
            max={3600}
            value={data?.interval_seconds ?? 120}
            onChange={(e) =>
              setData((d) => (d ? { ...d, interval_seconds: Number(e.target.value) } : d))
            }
            onBlur={(e) => wrap(() => api.setScanConfig({ interval_seconds: Number(e.target.value) }))}
            className="w-20 rounded bg-neutral-800 px-2 py-1"
          />
          <span className="text-xs text-neutral-400">s</span>
        </label>
        <button
          disabled={busy}
          onClick={() => wrap(() => api.addWatch(currentSymbol, currentAsset, currentTimeframe))}
          className="btn bg-neutral-700 text-white hover:bg-neutral-600"
        >
          + Add {currentSymbol} ({currentTimeframe})
        </button>
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await api.scanNow();
              await load();
            } finally {
              setBusy(false);
            }
          }}
          className="btn bg-blue-600 text-white hover:bg-blue-500"
        >
          Scan now
        </button>
        {data?.last_scan_at && (
          <span className="text-xs text-neutral-500">
            last scan {new Date(data.last_scan_at).toLocaleTimeString()}
          </span>
        )}
      </div>

      {!data || data.items.length === 0 ? (
        <div className="text-sm text-neutral-500">
          No pairs watched. Add the current pair to start.
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {data.items.map((it) => (
            <span
              key={it.id}
              className="flex items-center gap-2 rounded bg-neutral-800 px-2 py-1 text-sm"
            >
              {it.symbol}
              <span className="text-xs text-neutral-500">{it.timeframe}</span>
              <button
                disabled={busy}
                onClick={() => wrap(() => api.removeWatch(it.id))}
                className="text-neutral-500 hover:text-bear"
                title="Remove"
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
