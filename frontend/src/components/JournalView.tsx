import { useEffect, useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import type { ReflectionReport } from "../types";

// Strip broker float noise (e.g. 160.03199999999998) without hard-coding decimals.
function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  if (!Number.isFinite(v)) return String(v);
  return Number(v.toPrecision(12)).toString();
}

// Close date/time in the user's local zone (matches the Exness journal column).
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString([], {
    year: "2-digit", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

// Journal: closed-trade log + the read-only Reflection agent's patterns and lessons.
export function JournalView() {
  const { data: trades } = usePolling(() => api.journalTrades(100), 8000, []);
  const [reflection, setReflection] = useState<ReflectionReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.reflectionLatest().then(setReflection).catch(() => {});
  }, []);

  const runReflect = async () => {
    setBusy(true);
    setError(null);
    try {
      setReflection(await api.reflect());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-4">
      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">Reflection / Journal agent</div>
          <button onClick={runReflect} disabled={busy} className="btn bg-blue-600 text-white hover:bg-blue-500">
            {busy ? "Reflecting…" : "Run reflection"}
          </button>
        </div>
        <p className="text-xs text-neutral-500">
          Read-only: the reflection agent reviews closed trades for patterns and lessons. It
          can never place or modify a trade.
        </p>
        {error && <div className="text-sm text-bear">{error}</div>}

        {reflection ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Tile label="Trades reviewed" value={`${reflection.trades_reviewed}`} />
              <Tile label="Win rate" value={`${(reflection.win_rate * 100).toFixed(0)}%`} />
              <Tile
                label="Net P&L"
                value={`${reflection.net_pnl >= 0 ? "+" : ""}${reflection.net_pnl.toFixed(0)}`}
                valueClass={reflection.net_pnl >= 0 ? "text-bull" : "text-bear"}
              />
              <Tile label="Profit factor" value={reflection.profit_factor == null ? "—" : reflection.profit_factor.toFixed(2)} />
            </div>
            <p className="text-sm text-neutral-300">{reflection.summary}</p>
            {reflection.patterns.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-neutral-400">Patterns</div>
                <ul className="ml-4 list-disc text-sm text-neutral-300">
                  {reflection.patterns.map((p, i) => <li key={i}>{p}</li>)}
                </ul>
              </div>
            )}
            {reflection.lessons.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-neutral-400">Lessons</div>
                <ul className="ml-4 list-disc text-sm text-warn">
                  {reflection.lessons.map((l, i) => <li key={i}>{l}</li>)}
                </ul>
              </div>
            )}
            <div className="text-xs text-neutral-600">
              Generated {new Date(reflection.generated_at).toLocaleString()}
            </div>
          </div>
        ) : (
          <div className="text-sm text-neutral-500">No reflection yet. Click “Run reflection”.</div>
        )}
      </div>

      <div className="card">
        <div className="mb-2 text-sm font-semibold">Closed trades ({trades?.length ?? 0})</div>
        {!trades || trades.length === 0 ? (
          <div className="text-sm text-neutral-500">No closed trades yet.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-neutral-400">
              <tr>
                <th className="py-1">Closed</th><th>Symbol</th><th>Side</th>
                <th className="text-right">Qty</th><th className="text-right">Entry</th>
                <th className="text-right">Exit</th><th className="text-right">Realized P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <tr key={t.id} className="border-t border-neutral-800">
                  <td className="py-1 whitespace-nowrap text-neutral-400">{fmtDate(t.closed_at)}</td>
                  <td>{t.symbol}</td>
                  <td className={t.direction === "long" ? "text-bull" : "text-bear"}>{t.direction}</td>
                  <td className="text-right tabular-nums">{t.qty}</td>
                  <td className="text-right tabular-nums">{fmtPrice(t.entry_price)}</td>
                  <td className="text-right tabular-nums">{fmtPrice(t.last_price)}</td>
                  <td className={`text-right tabular-nums ${(t.realized_pnl ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
                    {t.realized_pnl == null ? "—" : `${t.realized_pnl >= 0 ? "+" : ""}${t.realized_pnl.toFixed(2)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function Tile({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded bg-neutral-800/60 p-2">
      <div className="text-xs text-neutral-400">{label}</div>
      <div className={`text-lg tabular-nums ${valueClass ?? ""}`}>{value}</div>
    </div>
  );
}
