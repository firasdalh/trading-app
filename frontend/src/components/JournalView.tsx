import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { CalibrationBucket, GroupStat, JournalBreakdown, JournalStats, PeriodBreakdown, ReflectionReport } from "../types";

// A friendly label + colour for each trade source (who opened it).
const SOURCE_META: Record<string, { label: string; color: string }> = {
  ai: { label: "AI decision", color: "text-violet-300" },
  rsi_over: { label: "RSI-Over", color: "text-sky-300" },
  armed: { label: "Armed break", color: "text-amber-300" },
  hybrid: { label: "Hybrid", color: "text-emerald-300" },
  manual: { label: "Manual", color: "text-neutral-200" },
  deterministic: { label: "Deterministic", color: "text-blue-300" },
  analysis: { label: "Analysis (legacy)", color: "text-indigo-300" },
  supertrend: { label: "SuperTrend", color: "text-teal-300" },
  unknown: { label: "Unknown", color: "text-neutral-500" },
};
const srcLabel = (s: string) => SOURCE_META[s]?.label ?? s;
const srcColor = (s: string) => SOURCE_META[s]?.color ?? "text-neutral-300";

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
  const [bump, setBump] = useState(0);
  const { data: trades } = usePolling(() => api.journalTrades(100), 8000, [bump]);
  const { data: calib } = usePolling(() => api.journalCalibration(), 10000, [bump]);
  const { data: perf } = usePolling(() => api.journalStats(), 10000, [bump]);
  const { data: breakdown } = usePolling(() => api.journalBreakdown(), 10000, [bump]);
  const [reflection, setReflection] = useState<ReflectionReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resetJournal = async () => {
    if (!window.confirm(
      "Start a FRESH journal from now?\n\nThe trade log, performance, and calibration will only " +
        "count trades that close from this moment on. Nothing is deleted — your broker's full " +
        "history is untouched, and you can restore the full view anytime.",
    )) return;
    setBusy(true);
    setError(null);
    try {
      await api.resetJournal();
      setBump((b) => b + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const restoreJournal = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.restoreJournal();
      setBump((b) => b + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const repairJournal = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.backfillJournal();
      setBump((b) => b + 1);
      setError(
        `Repaired: labelled ${r.sources_labelled} legacy source${r.sources_labelled === 1 ? "" : "s"}, ` +
          `recovered P&L for ${r.pnl_recovered} closed trade${r.pnl_recovered === 1 ? "" : "s"}.`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
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
          <button onClick={runReflect} disabled={busy} className="btn btn-primary">
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

      <BreakdownCard breakdown={breakdown} />

      <CalibrationCard calib={calib} />

      <div className="card">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-sm font-semibold">Closed trades ({trades?.length ?? 0})</span>
          <div className="flex items-center gap-3">
            <button
              onClick={repairJournal}
              disabled={busy}
              className="text-xs text-neutral-500 hover:text-brand-400"
              title="Repair app-tracked rows: label legacy 'Unknown' trades and recover missing P&L from broker history (for the by-source stats). Non-destructive."
            >
              Repair ⟳
            </button>
            <button
              onClick={restoreJournal}
              disabled={busy}
              className="text-xs text-neutral-500 hover:text-neutral-300"
              title="Show the full broker history again (undo a previous reset)."
            >
              Show all
            </button>
            <button
              onClick={resetJournal}
              disabled={busy}
              className="text-xs text-neutral-400 hover:text-bear"
              title="Start a fresh journal from now — the log + stats only count trades from this point on. Your broker's full history is NOT deleted."
            >
              Start fresh ↺
            </button>
          </div>
        </div>
        {!trades || trades.length === 0 ? (
          <div className="text-sm text-neutral-500">No closed trades yet.</div>
        ) : (
          <div className="max-h-[28rem] overflow-auto">
            <table className="w-full whitespace-nowrap text-sm">
              <thead className="sticky top-0 bg-neutral-900 text-left text-xs text-neutral-400">
                <tr>
                  <th className="py-1 pr-3">Closed</th><th className="pr-3">Symbol</th><th className="pr-3">Side</th>
                  <th className="pr-3">Source</th>
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
                    <td className={`pr-3 text-xs ${srcColor(t.source ?? "unknown")}`}>{srcLabel(t.source ?? "unknown")}</td>
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

// ---- Breakdown: who's making money (by source / pair / time) ----
const pctText = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const rText = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}R`);

function StatTable({ rows, firstCol, nameFn, nameCls }: {
  rows: GroupStat[];
  firstCol: string;
  nameFn?: (l: string) => string;
  nameCls?: (l: string) => string;
}) {
  return (
    <table className="w-full whitespace-nowrap text-sm">
      <thead className="text-left text-xs text-neutral-400">
        <tr>
          <th className="py-1 pr-3">{firstCol}</th>
          <th className="pr-3 text-right">Trades</th>
          <th className="pr-3 text-right">Win rate</th>
          <th className="pr-3 text-right">Net P&amp;L</th>
          <th className="pr-3 text-right">Avg R</th>
          <th className="text-right">Total R</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((g) => (
          <tr key={g.label} className="border-t border-neutral-800">
            <td className={`py-1 pr-3 ${nameCls ? nameCls(g.label) : ""}`}>{nameFn ? nameFn(g.label) : g.label}</td>
            <td className="pr-3 text-right tabular-nums">{g.trades}</td>
            <td className="pr-3 text-right tabular-nums">
              {pctText(g.win_rate)} <span className="text-neutral-600">({g.wins}/{g.trades})</span>
            </td>
            <td className={`pr-3 text-right tabular-nums ${g.net_pnl >= 0 ? "text-bull" : "text-bear"}`}>
              {fmtUsd(g.net_pnl, { sign: true })}
            </td>
            <td className={`pr-3 text-right tabular-nums ${(g.avg_r ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>{rText(g.avg_r)}</td>
            <td className={`text-right tabular-nums ${(g.total_r ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>{rText(g.total_r)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function BreakdownCard({ breakdown }: { breakdown: JournalBreakdown | null }) {
  const [period, setPeriod] = useState<"daily" | "weekly" | "monthly">("daily");
  if (!breakdown || breakdown.by_source.length === 0) return null;
  const buckets: PeriodBreakdown[] = breakdown[period];
  return (
    <div className="card space-y-4">
      <div className="text-sm font-semibold">Breakdown — who's making money</div>

      <div>
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">By source (all-time)</div>
        <StatTable firstCol="Source" rows={breakdown.by_source} nameFn={srcLabel} nameCls={srcColor} />
      </div>

      <div>
        <div className="mb-2 flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-neutral-500">Over time</span>
          <div className="flex gap-1">
            {(["daily", "weekly", "monthly"] as const).map((p) => (
              <button key={p} onClick={() => setPeriod(p)}
                className={`rounded px-2 py-0.5 text-xs capitalize ${period === p ? "bg-neutral-700 text-white" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}>
                {p}
              </button>
            ))}
          </div>
        </div>
        {buckets.length === 0 ? (
          <div className="text-xs text-neutral-500">No closed trades yet.</div>
        ) : (
          <div className="max-h-[24rem] space-y-2 overflow-auto pr-1">
            {buckets.map((b) => (
              <div key={b.period} className="rounded-md border border-neutral-800 bg-neutral-900/40 px-3 py-2">
                <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 text-sm">
                  <span className="font-semibold">{b.period}</span>
                  <span className="flex items-center gap-3 text-xs">
                    <span className="text-neutral-400">{b.total.trades} trades · {pctText(b.total.win_rate)} win</span>
                    <span className={b.total.net_pnl >= 0 ? "text-bull" : "text-bear"}>{fmtUsd(b.total.net_pnl, { sign: true })}</span>
                    <span className={(b.total.total_r ?? 0) >= 0 ? "text-bull" : "text-bear"}>{rText(b.total.total_r)}</span>
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {b.sources.map((s) => (
                    <span key={s.label} className="rounded border border-neutral-700 px-1.5 py-0.5 text-[11px] tabular-nums">
                      <span className={srcColor(s.label)}>{srcLabel(s.label)}</span>{" "}
                      <span className="text-neutral-500">{s.wins}/{s.trades}</span>{" "}
                      <span className={s.net_pnl >= 0 ? "text-bull" : "text-bear"}>{fmtUsd(s.net_pnl, { sign: true })}</span>
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">By pair (all-time)</div>
        <div className="max-h-[20rem] overflow-auto pr-1">
          <StatTable firstCol="Pair" rows={breakdown.by_pair} />
        </div>
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
