import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import type { CalibrationBucket, GroupStat, JournalBreakdown, PeriodBreakdown, ReflectionReport } from "../types";
import { ResultTag, SessionStatsCard, SessionTag } from "./SessionStats";
import { SignalReviewCard } from "./SignalReview";
import { FoldButton, FoldProvider, useFold, type Fold } from "./JournalFold";
import { PerformanceCard } from "./PerformanceCharts";
import { srcLabel, srcColor } from "./sourceMeta";

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

// "5m ago" / "3h ago" / "4d ago" — how old the stored reflection snapshot is.
function sinceText(iso: string): string {
  const safe = /[Zz]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`;
  const mins = Math.max(0, Math.round((Date.now() - Date.parse(safe)) / 60000));
  if (!Number.isFinite(mins)) return "";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  return hrs < 48 ? `${hrs}h ago` : `${Math.round(hrs / 24)}d ago`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The Journal's sections, in page order. Each folds to its title line (▾ by the title) and can be hidden from the
// page (the "Sections" strip) - asked 2026-11-11: "the option to hide or collapse some area". Kept in this browser.
const SECTIONS = [
  { id: "today", name: "Today's review" },
  { id: "reflection", name: "Reflection" },
  { id: "performance", name: "Performance" },
  { id: "sessions", name: "Sessions" },
  { id: "signals", name: "Signal review" },
  { id: "breakdown", name: "Breakdown" },
  { id: "calibration", name: "Score vs result" },
  { id: "trades", name: "Closed trades" },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];

// Journal: closed-trade log + the read-only Reflection agent's patterns and lessons.
export function JournalView() {
  const [bump, setBump] = useState(0);
  // The sections hidden from the page, and those folded to their title line. The reflection report is long: it
  // starts folded, as it always did.
  const [hidden, setHidden] = useLocalStorage<string[]>("ta.journal.hidden", []);
  const [folded, setFolded] = useLocalStorage<string[]>("ta.journal.folded", ["reflection"]);
  const foldOf = (id: SectionId): Fold => ({
    folded: folded.includes(id),
    toggle: () => setFolded((f) => (f.includes(id) ? f.filter((x) => x !== id) : [...f, id])),
  });
  const section = (id: SectionId, node: ReactNode) =>
    hidden.includes(id) ? null : <FoldProvider value={foldOf(id)}>{node}</FoldProvider>;
  const { data: trades } = usePolling(() => api.journalTrades(100), 8000, [bump]);
  // The backend tags each row with the session it opened in (2026-11-10). An older one sends no `session` at all:
  // then the column stays away rather than reading "—" all the way down.
  const withSessions = !!trades?.some((t) => t.session !== undefined);
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
          `recovered P&L for ${r.pnl_recovered} closed trade${r.pnl_recovered === 1 ? "" : "s"}.` +
          (r.pnl_corrected
            ? ` Corrected ${r.pnl_corrected} trade${r.pnl_corrected === 1 ? "" : "s"} whose stored result was not its own - now the broker's figure.`
            : ""),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  // The reflection report's fold is its section's (folded by default: the report is long).
  const open = !folded.includes("reflection");
  const setOpen = (v: boolean) =>
    setFolded((f) => (v ? f.filter((x) => x !== "reflection") : f.includes("reflection") ? f : [...f, "reflection"]));

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
    // Full width with the desk's own gutter (p-3 / gap-3): a centred max-w-7xl column made the
    // Journal look like a different app from the edge-to-edge desk you switch from.
    <div className="space-y-3 p-3">
      {/* Which sections show, and folding them all at once (2026-11-11: "the option to hide or collapse some area"). */}
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <span
          className="cursor-help text-neutral-500"
          title="Click a section to hide it from this page, and again to bring it back. ▾ by a section's title folds it to that one line. Kept in this browser."
        >
          Sections
        </span>
        {SECTIONS.map((s) => {
          const on = !hidden.includes(s.id);
          return (
            <button
              key={s.id}
              type="button"
              aria-pressed={on}
              onClick={() => setHidden((h) => (on ? [...h, s.id] : h.filter((x) => x !== s.id)))}
              className={`rounded-full border px-2 py-0.5 transition ${
                on
                  ? "border-neutral-600 bg-neutral-800/60 text-neutral-200 hover:border-neutral-400"
                  : "border-dashed border-neutral-700 text-neutral-500 line-through hover:text-neutral-300"
              }`}
              title={on ? `Hide ${s.name} from this page` : `Show ${s.name} again`}
            >
              {s.name}
            </button>
          );
        })}
        <span className="ml-auto flex items-center gap-3 text-neutral-500">
          <button type="button" onClick={() => setFolded(SECTIONS.map((s) => s.id))} className="hover:text-neutral-200" title="Fold every section to its title line">
            ▸ Fold all
          </button>
          <button type="button" onClick={() => setFolded([])} className="hover:text-neutral-200" title="Open every section">
            ▾ Open all
          </button>
        </span>
      </div>

      {section("today", <DailyReview />)}
      {section("reflection", (
      <div className="card space-y-3">
        <div className="flex items-center justify-between gap-2">
          <button
            onClick={() => setOpen(!open)}
            className="flex items-center gap-2 text-left text-sm font-semibold hover:text-white"
            title={open ? "Collapse the report" : "Expand the report"}
          >
            <span className="text-neutral-500">{open ? "▾" : "▸"}</span>
            Reflection / Journal agent
            {/* The scope goes first. These are a SNAPSHOT from the last reflection run, over its own
                window and judged in dollars, so they will not match the live Performance (R) card
                below — without the scope that read as two contradicting win rates. */}
            {!open && reflection && (
              <span
                className="ml-1 text-xs font-normal text-neutral-500"
                title={
                  "Snapshot from the last reflection run: the most recent closed trades with a booked $ " +
                  "result, judged in dollars. Performance (R) below is live and measured in R, so its win " +
                  "rate and profit factor can differ."
                }
              >
                · last {plural(reflection.trades_reviewed, "trade")} · {sinceText(reflection.generated_at)}
                {" "}· win {(reflection.win_rate * 100).toFixed(0)}% · PF{" "}
                {reflection.profit_factor == null ? "—" : reflection.profit_factor.toFixed(2)} · net{" "}
                <span className={reflection.net_pnl >= 0 ? "text-bull" : "text-bear"}>
                  {fmtUsd(reflection.net_pnl, { sign: true })}
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

        {/* Full width now, so the prose keeps a reading measure (the tiles above still span). */}
        {reflection ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Tile label="Trades reviewed" value={`${reflection.trades_reviewed}`} />
              <Tile label="Win rate" value={`${(reflection.win_rate * 100).toFixed(0)}%`} />
              <Tile
                label="Net P&L"
                value={fmtUsd(reflection.net_pnl, { sign: true })}
                valueClass={reflection.net_pnl >= 0 ? "text-bull" : "text-bear"}
              />
              <Tile label="Profit factor" value={reflection.profit_factor == null ? "—" : reflection.profit_factor.toFixed(2)} />
            </div>
            <p className="prose-measure text-sm text-neutral-300">{reflection.summary}</p>
            {reflection.patterns.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-neutral-400">Patterns</div>
                <ul className="prose-measure ml-4 list-disc text-sm text-neutral-300">
                  {reflection.patterns.map((p, i) => <li key={i}>{p}</li>)}
                </ul>
              </div>
            )}
            {reflection.lessons.length > 0 && (
              <div>
                <div className="text-xs font-semibold text-neutral-400">Lessons</div>
                <ul className="prose-measure ml-4 list-disc text-sm text-warn">
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
      ))}

      {/* Drawn (2026-11-11, "enhance the performance visualization"): the equity curve, P&L by day, each trade's result. */}
      {section("performance", <PerformanceCard perf={perf} bump={bump} />)}

      {/* Which session pays: the closed trades by the session each one opened in (2026-11-10). */}
      {section("sessions", <SessionStatsCard />)}

      {/* What each watch signal would have done if traded, split at each change to the strategy (2026-11-08). */}
      {section("signals", <SignalReviewCard />)}

      {section("breakdown", <BreakdownCard breakdown={breakdown} />)}

      {section("calibration", <CalibrationCard calib={calib} />)}

      {section("trades", (
      <div className="card">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-1 text-sm font-semibold">
            <FoldButton name="Closed trades" />
            Closed trades ({trades?.length ?? 0})
          </span>
          <div className="flex items-center gap-3">
            <button
              onClick={repairJournal}
              disabled={busy}
              className="text-xs text-neutral-500 hover:text-brand-400"
              title="Repair app-tracked rows: label legacy 'Unknown' trades, recover missing P&L from broker history, and give every row its own trade's result as the broker booked it (what the strategy's daily target, the loss cooldown and the scorecard read). Your broker's history is never touched."
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
        {folded.includes("trades") ? null : !trades || trades.length === 0 ? (
          <div className="text-sm text-neutral-500">No closed trades yet.</div>
        ) : (
          <div className="max-h-[28rem] overflow-auto">
            <table className="w-full whitespace-nowrap text-sm">
              <thead className="sticky top-0 bg-neutral-900 text-left text-xs text-neutral-400">
                <tr>
                  <th className="py-1 pr-3">Closed</th>{withSessions && <th className="pr-3">Session</th>}<th className="pr-3">Symbol</th>
                  <th className="pr-3">Side</th><th className="pr-3">Source</th>
                  <th className="pr-3 text-right">Qty</th><th className="pr-3 text-right">Entry</th>
                  <th className="pr-3 text-right">Exit</th><th className="pr-3">Result</th><th className="text-right">Realized P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-t border-neutral-800">
                    <td className="py-1 pr-3 text-neutral-400">{fmtDate(t.closed_at)}</td>
                    {withSessions && <td className="pr-3"><SessionTag session={t.session} openedAt={t.opened_at} /></td>}
                    <td className="pr-3">{t.symbol}</td>
                    <td className={`pr-3 ${t.direction === "long" ? "text-bull" : "text-bear"}`}>{t.direction}</td>
                    <td className={`pr-3 text-xs ${srcColor(t.source ?? "unknown")}`}>{srcLabel(t.source ?? "unknown")}</td>
                    <td className="pr-3 text-right tabular-nums">{t.qty}</td>
                    <td className="pr-3 text-right tabular-nums">{fmtPrice(t.entry_price)}</td>
                    <td className="pr-3 text-right tabular-nums">{fmtPrice(t.last_price)}</td>
                    <td className="pr-3"><ResultTag pnl={t.realized_pnl} /></td>
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
      ))}
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
  const fold = useFold();
  const [period, setPeriod] = useState<"daily" | "weekly" | "monthly">("daily");
  // Whose trades By pair shows - "By pair (all-time) - I want it also for the strategy" (2026-11-11). Kept per browser.
  const [pairWho, setPairWho] = useLocalStorage<string>("ta.journal.pairWho", "all");
  if (!breakdown || breakdown.by_source.length === 0) return null;
  const buckets: PeriodBreakdown[] = breakdown[period];
  // Its choices: every source, the most trades first. An older backend sends no per-source split: no choices.
  const bySrc = breakdown.by_pair_source;
  const choices: [string, number][] = [
    ["all", breakdown.by_source.reduce((n, g) => n + g.trades, 0)],
    ...[...breakdown.by_source].sort((a, b) => b.trades - a.trades).map((g): [string, number] => [g.label, g.trades]),
  ];
  const who = bySrc && pairWho !== "all" && bySrc[pairWho] ? pairWho : "all";
  const pairRows = who === "all" || !bySrc ? breakdown.by_pair : bySrc[who];
  return (
    <div className="card space-y-4">
      <div>
        <div className="flex items-center gap-1 text-sm font-semibold">
          <FoldButton name="Breakdown" />
          Breakdown — who's making money
        </div>
        {!fold?.folded && (
          <p className="text-xs text-neutral-500">
            Your broker's history: every trade on this account, your phone's included - never the demo account's. Who
            opened each one is MT5's own record of it; R only where the app recorded the risk taken.
          </p>
        )}
      </div>

      {!fold?.folded && (<>
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
                    <span className="text-neutral-400">{plural(b.total.trades, "trade")} · {pctText(b.total.win_rate)} win</span>
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
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-neutral-500">By pair (all-time)</span>
          {bySrc && (
            <div className="ml-auto flex flex-wrap gap-1" role="group" aria-label="Whose trades, by pair">
              {choices.map(([k, n]) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setPairWho(k)}
                  className={`rounded px-2 py-0.5 text-xs ${who === k ? "bg-neutral-700 text-white" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
                  title={k === "all" ? "Every closed trade, by pair" : `Only the trades opened by ${srcLabel(k)}, by pair`}
                >
                  <span className={k === "all" || who === k ? "" : srcColor(k)}>{k === "all" ? "All" : srcLabel(k)}</span>{" "}
                  <span className="tabular-nums text-neutral-500">{n}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="max-h-[20rem] overflow-auto pr-1">
          <StatTable firstCol="Pair" rows={pairRows} />
        </div>
      </div>
      </>)}
    </div>
  );
}

// Confidence calibration: per-bucket realized win rate vs the confidence the engine assigned.
// Green = the bucket wins at least as often as its midpoint implies (calibrated/underconfident);
// red = it wins much less often (overconfident — the score is inflating those setups).
function CalibrationCard({ calib }: { calib: CalibrationBucket[] | null }) {
  const fold = useFold();
  const withTrades = (calib ?? []).filter((b) => b.trades > 0);
  const total = withTrades.reduce((n, b) => n + b.trades, 0);
  return (
    <div className="card">
      <div className="mb-1 flex items-center gap-1 text-sm font-semibold">
        <FoldButton name="Score vs result" />
        Score bucket vs result
      </div>
      {!fold?.folded && (<>
      <p className="mb-2 text-xs text-neutral-500">
        Do higher-scored setups actually make more? Judge by Avg R, not win rate: the score is a
        checklist, not a win probability, and with ~3R targets a sound engine wins only 35–45%.
        Manual and strategy trades carry no score and are left out. Needs ≈20+ trades per bucket.
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
              // Win rate is shown plainly: comparing it to the bucket's "%" painted every bucket red
              // for an engine that targets 3R (full review 2026-10-20). Avg R carries the colour.
              void bucketMid;
              const wrClass = "text-neutral-300";
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
      </>)}
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


/**
 * Today, in one card: the trades closed today with your own reason and tags, the totals, the longest
 * losing run, and avg R per tag once a tag has 10+ trades. The manual book could not learn before -
 * nothing recorded WHY a trade was taken (full review 2026-10-20).
 */
function DailyReview() {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.journalReview>> | null>(null);
  useEffect(() => {
    void api.journalReview().then(setData).catch(() => setData(null));
  }, []);
  const fold = useFold();
  if (!data) return null;
  const r = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}R`);
  return (
    <div className="card">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <span className="flex items-center gap-1 text-sm font-semibold">
          <FoldButton name="Today's review" />
          <span title="The broker's day - from 00:00 UTC, as the header's DAY P&L: every trade closed on the account, your phone's too, in the broker's $">
            Today&apos;s review · {data.date}
          </span>
        </span>
        <span className="text-xs text-neutral-400">
          {data.trades.length} closed · <span className={data.net_pnl >= 0 ? "text-bull" : "text-bear"}>
            {data.net_pnl >= 0 ? "+" : "-"}${Math.abs(data.net_pnl).toFixed(2)}</span> · {r(data.total_r)}
          {data.longest_losing_run >= 3 && (
            <span className="ml-2 font-semibold text-warn">{data.longest_losing_run} losses in a row today</span>
          )}
        </span>
      </div>
      {!fold?.folded && (<>
      {data.trades.length === 0 ? (
        <div className="text-xs text-neutral-500">No trades closed today.</div>
      ) : (
        <ul className="space-y-0.5 text-xs">
          {data.trades.map((t, i) => (
            <li key={i} className="flex flex-wrap gap-x-2 text-neutral-300">
              <span className="font-medium">{t.symbol}</span>
              <span className="text-neutral-500">{t.direction} · <span className={srcColor(t.source ?? "manual")}>{srcLabel(t.source ?? "manual")}</span></span>
              <span className={(t.r ?? t.pnl) >= 0 ? "text-bull" : "text-bear"}>{r(t.r)}</span>
              {t.tags.length > 0 && <span className="text-neutral-500">[{t.tags.join(", ")}]</span>}
              {t.note && <span className="italic text-neutral-500">“{t.note}”</span>}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 text-[11px] text-neutral-500">
        {data.by_tag.length > 0 ? (
          <>By your tags (all time): {data.by_tag.map((b) => `${b.tag} ${b.trades} trades ${r(b.avg_r)}`).join(" · ")}</>
        ) : (
          <>Tag your chart trades (plan / with-trend / revenge …) - after 10 trades per tag this shows which
            kind of trade actually pays.{data.tags_below_10.length > 0 && ` Collecting: ${data.tags_below_10.join(", ")}.`}</>
        )}
      </div>
      </>)}
    </div>
  );
}
