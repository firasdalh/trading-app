import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { CalibrationBucket, JournalStats, ReflectionReport } from "../types";

// Midpoint of a "70-80%" bucket label, used to judge whether the realized win rate matches the
// confidence the engine assigned (the whole point of calibration).
function bucketMid(label: string): number {
  const m = label.match(/(\d+)-(\d+)/);
  if (!m) return 0;
  return (Number(m[1]) + Number(m[2])) / 200; // -> 0..1
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
  const { data: calib } = usePolling(() => api.journalCalibration(), 10000, []);
  const { data: perf } = usePolling(() => api.journalStats(), 10000, []);
  const [reflection, setReflection] = useState<ReflectionReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);  // the report is long — collapsed by default

  useEffect(() => {
    api.reflectionLatest().then(setReflection).catch(() => {});
  }, []);

  const runReflect = async () => {
    setBusy(true);
    setError(null);
    try {
      setReflection(await api.reflect());
      setOpen(true);  // show the fresh result
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-4">
      <div className="card space-y-3">
        <div className="flex items-center justify-between gap-2">
          <button
            onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-2 text-sm font-semibold hover:text-white"
            title={open ? "Collapse the report" : "Expand the report"}
          >
            <span className="text-neutral-500">{open ? "▾" : "▸"}</span>
            Reflection / Journal agent
            {!open && reflection && (
              <span className="ml-1 text-xs font-normal text-neutral-500">
                · win {(reflection.win_rate * 100).toFixed(0)}% · PF{" "}
                {reflection.profit_factor == null ? "—" : reflection.profit_factor.toFixed(2)} · net{" "}
                <span className={reflection.net_pnl >= 0 ? "text-bull" : "text-bear"}>
                  {reflection.net_pnl >= 0 ? "+" : ""}{reflection.net_pnl.toFixed(0)}
                </span>
              </span>
            )}
          </button>
          <button onClick={runReflect} disabled={busy} className="btn bg-blue-600 text-white hover:bg-blue-500">
            {busy ? "Reflecting…" : "Run reflection"}
          </button>
        </div>
        {error && <div className="text-sm text-bear">{error}</div>}

        {open && (
          <>
        <p className="text-xs text-neutral-500">
          Read-only: the reflection agent reviews closed trades for patterns and lessons. It
          can never place or modify a trade.
        </p>

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
          </>
        )}
      </div>

      <PerformanceCard perf={perf} />

      <CalibrationCard calib={calib} />

      <div className="card">
        <div className="mb-2 text-sm font-semibold">Closed trades ({trades?.length ?? 0})</div>
        {!trades || trades.length === 0 ? (
          <div className="text-sm text-neutral-500">No closed trades yet.</div>
        ) : (
          <div className="max-h-[28rem] overflow-auto">
            <table className="w-full whitespace-nowrap text-sm">
              <thead className="sticky top-0 bg-neutral-900 text-left text-xs text-neutral-400">
                <tr>
                  <th className="py-1 pr-3">Closed</th><th className="pr-3">Symbol</th><th className="pr-3">Side</th>
                  <th className="pr-3 text-right">Qty</th><th className="pr-3 text-right">Entry</th>
                  <th className="pr-3 text-right">Exit</th><th className="text-right">Realized P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-t border-neutral-800">
                    <td className="py-1 pr-3 text-neutral-400">{fmtDate(t.closed_at)}</td>
                    <td className="pr-3">{t.symbol}</td>
                    <td className={`pr-3 ${t.direction === "long" ? "text-bull" : "text-bear"}`}>{t.direction}</td>
                    <td className="pr-3 text-right tabular-nums">{t.qty}</td>
                    <td className="pr-3 text-right tabular-nums">{fmtPrice(t.entry_price)}</td>
                    <td className="pr-3 text-right tabular-nums">{fmtPrice(t.last_price)}</td>
                    <td className={`text-right tabular-nums ${(t.realized_pnl ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
                      {t.realized_pnl == null ? "—" : fmtUsd(t.realized_pnl, { sign: true })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// Performance in R: expectancy (the edge), win rate, profit factor, and the worst R-drawdown —
// the numbers a desk reviews. R = realized P&L / risk taken, so it's instrument-agnostic.
function PerformanceCard({ perf }: { perf: JournalStats | null }) {
  if (!perf || perf.trades === 0) return null;
  const exp = perf.expectancy_r;
  return (
    <div className="card">
      <div className="mb-1 text-sm font-semibold">Performance (R)</div>
      <p className="mb-2 text-xs text-neutral-500">
        R = profit ÷ risk taken. Expectancy is your edge per trade; positive means the system makes
        money over time. Over {perf.trades} closed trade{perf.trades === 1 ? "" : "s"} with recorded risk.
      </p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="Expectancy"
              value={exp == null ? "—" : `${exp >= 0 ? "+" : ""}${exp.toFixed(2)}R`}
              valueClass={(exp ?? 0) >= 0 ? "text-bull" : "text-bear"} />
        <Tile label="Win rate" value={perf.win_rate == null ? "—" : `${(perf.win_rate * 100).toFixed(0)}%`} />
        <Tile label="Avg win" value={perf.avg_win_r == null ? "—" : `+${perf.avg_win_r.toFixed(2)}R`}
              valueClass="text-bull" />
        <Tile label="Avg loss" value={perf.avg_loss_r == null ? "—" : `${perf.avg_loss_r.toFixed(2)}R`}
              valueClass="text-bear" />
        <Tile label="Profit factor" value={perf.profit_factor == null ? "—" : perf.profit_factor.toFixed(2)} />
        <Tile label="Max drawdown"
              value={perf.max_drawdown_r == null ? "—" : `-${perf.max_drawdown_r.toFixed(2)}R`}
              valueClass="text-bear" />
      </div>
    </div>
  );
}

// Confidence calibration: per-bucket realized win rate vs the confidence the engine assigned.
// Green = the bucket wins at least as often as its midpoint implies (calibrated/underconfident);
// red = it wins much less often (overconfident — the score is inflating those setups).
function CalibrationCard({ calib }: { calib: CalibrationBucket[] | null }) {
  const withTrades = (calib ?? []).filter((b) => b.trades > 0);
  const total = withTrades.reduce((n, b) => n + b.trades, 0);
  return (
    <div className="card">
      <div className="mb-1 text-sm font-semibold">Confidence calibration</div>
      <p className="mb-2 text-xs text-neutral-500">
        Does a “70%” setup actually win ~70%? Realized win rate per confidence bucket. Needs a
        decent sample (≈20+ trades per bucket) before it’s trustworthy.
      </p>
      {total === 0 ? (
        <div className="text-sm text-neutral-500">
          No closed trades carry a recorded confidence yet — this fills in as new trades close.
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-neutral-400">
            <tr>
              <th className="py-1 pr-3">Confidence</th>
              <th className="pr-3 text-right">Trades</th>
              <th className="pr-3 text-right">Win rate</th>
              <th className="text-right">Avg R</th>
            </tr>
          </thead>
          <tbody>
            {withTrades.map((b) => {
              const wr = b.win_rate ?? 0;
              const mid = bucketMid(b.bucket);
              const wrClass = wr >= mid ? "text-bull" : wr >= mid - 0.1 ? "text-warn" : "text-bear";
              return (
                <tr key={b.bucket} className="border-t border-neutral-800">
                  <td className="py-1 pr-3">{b.bucket}</td>
                  <td className="pr-3 text-right tabular-nums">{b.trades}</td>
                  <td className={`pr-3 text-right tabular-nums ${wrClass}`}>
                    {b.win_rate == null ? "—" : `${(b.win_rate * 100).toFixed(0)}%`}
                  </td>
                  <td className={`text-right tabular-nums ${(b.avg_r ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
                    {b.avg_r == null ? "—" : `${b.avg_r >= 0 ? "+" : ""}${b.avg_r.toFixed(2)}R`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
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
