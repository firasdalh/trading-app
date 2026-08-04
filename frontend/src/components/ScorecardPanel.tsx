import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { ScorecardView, ScoreVerdict, SymbolScore } from "../types";

// Per-symbol report card built from CLOSED trades — the one place the system grades its own results
// instead of predicting. Everything here is arithmetic on trades that already happened, so unlike an
// entry filter there is nothing to overfit.
//
// The verdict comes from EXPECTANCY IN R (profit per unit of risk), never win rate: 35% wins with 3R
// winners is a good symbol, 60% wins with 0.3R winners is a slow bleed. Win rate is shown because
// it's the intuitive number, but it is deliberately not the judgement.

const VERDICT: Record<ScoreVerdict, { label: string; cls: string; icon: string; blurb: string }> = {
  proven: {
    label: "Proven", icon: "✓", cls: "bg-bull/15 text-bull",
    blurb: "Makes money by more than luck explains — this is where the edge is.",
  },
  watching: {
    label: "Watching", icon: "•", cls: "bg-neutral-700/60 text-neutral-400",
    blurb: "Judged, but too close to break-even to call either way.",
  },
  weak: {
    label: "Weak", icon: "!", cls: "bg-amber-500/15 text-amber-400",
    blurb: "Losing, but still inside what a normal bad run could explain.",
  },
  disable: {
    label: "Stop", icon: "✕", cls: "bg-bear/15 text-bear",
    blurb: "Loses more than luck explains. The evidence says stop trading it.",
  },
  learning: {
    label: "Learning", icon: "…", cls: "bg-neutral-700/40 text-neutral-500",
    blurb: "Not enough closed trades yet to judge.",
  },
};

const ORDER: ScoreVerdict[] = ["disable", "weak", "watching", "learning", "proven"];

export function ScorecardPanel() {
  const [card, setCard] = useState<ScorecardView | null>(null);
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState<number | undefined>(undefined);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setCard(await api.scorecard(days));
    } catch {
      /* leave the previous card up; the next load will correct it */
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!card) return null;

  const judged = card.scores.filter((s) => s.verdict !== "learning");
  const counts = ORDER.map((v) => ({ v, n: card.scores.filter((s) => s.verdict === v).length }));

  return (
    <div className="card">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full flex-wrap items-baseline gap-x-2 gap-y-0.5 text-left"
        title={open ? "Hide the scorecard" : "Show the per-symbol scorecard"}
      >
        <span className="text-neutral-500">{open ? "▾" : "▸"}</span>
        <span className="whitespace-nowrap text-sm font-semibold">📋 Symbol scorecard</span>
        <span className="text-xs text-neutral-500">
          how each pair's closed trades actually turned out · judged after {card.min_trades} trades
        </span>
        {card.warnings.length > 0 && (
          <span className="rounded bg-bear/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-bear">
            {card.warnings.length} to stop
          </span>
        )}
        {!card.auto_disable && (
          <span className="rounded bg-neutral-700/60 px-1.5 py-0.5 text-[10px] font-semibold text-neutral-400">
            warn only
          </span>
        )}
      </button>

      {open && (
        <>
          {card.warnings.length > 0 && (
            <div className="mt-2 space-y-1 rounded-lg border border-bear/30 bg-bear/10 p-2">
              <div className="text-xs font-semibold text-bear">The evidence says stop trading these</div>
              {card.warnings.map((w) => (
                <div key={w} className="text-[11px] leading-snug text-neutral-300">{w}</div>
              ))}
              <div className="text-[11px] text-neutral-500">
                Nothing has been switched off — turn these pairs off in the watchlist yourself, or
                enable auto-disable in settings.
              </div>
            </div>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
            {counts.filter((c) => c.n > 0).map(({ v, n }) => (
              <span key={v} className={`rounded px-1.5 py-0.5 font-semibold ${VERDICT[v].cls}`}>
                {VERDICT[v].icon} {n} {VERDICT[v].label.toLowerCase()}
              </span>
            ))}
            <span className="ml-auto flex items-center gap-1 text-neutral-500">
              <span>window</span>
              {([undefined, 30, 90] as const).map((d) => (
                <button
                  key={String(d)}
                  onClick={() => setDays(d)}
                  className={`rounded px-1.5 py-0.5 ${
                    days === d ? "bg-neutral-700 text-neutral-200" : "text-neutral-500 hover:text-neutral-300"
                  }`}
                >
                  {d ? `${d}d` : "all"}
                </button>
              ))}
              <button
                onClick={() => void load()}
                disabled={loading}
                className="rounded px-1.5 py-0.5 text-neutral-500 hover:text-neutral-300 disabled:opacity-50"
              >
                {loading ? "…" : "↻"}
              </button>
            </span>
          </div>

          {judged.length === 0 && (
            <div className="mt-2 rounded bg-neutral-800/50 px-2 py-1.5 text-[11px] leading-snug text-neutral-400">
              No pair has {card.min_trades} closed trades yet, so nothing is judged. This is the
              threshold doing its job — condemning a pair on a short losing run mistakes a normal bad
              patch for a broken edge.
            </div>
          )}

          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[34rem] text-xs">
              <thead className="text-[10px] uppercase tracking-wide text-neutral-500">
                <tr className="border-b border-neutral-800">
                  <th className="py-1 text-left font-medium">Verdict</th>
                  <th className="py-1 text-left font-medium">Symbol</th>
                  <th className="py-1 text-right font-medium">Trades</th>
                  <th className="py-1 text-right font-medium" title="How often it won. NOT what the verdict is based on.">
                    Win %
                  </th>
                  <th className="py-1 text-right font-medium" title="Average profit per unit of risk — what the verdict IS based on.">
                    Per trade
                  </th>
                  <th className="py-1 text-right font-medium">P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {card.scores.map((s) => (
                  <Row key={s.symbol} s={s} />
                ))}
              </tbody>
            </table>
          </div>

          <p className="mt-2 text-[11px] leading-snug text-neutral-500">
            Judged on <strong>profit per unit of risk</strong>, not win rate — a pair winning 35% of
            the time with big winners beats one winning 60% with tiny ones. A verdict only appears
            once there are {card.min_trades}+ closed trades <em>and</em> the result is far enough from
            break-even that luck is an unlikely explanation.
          </p>
        </>
      )}
    </div>
  );
}

function Row({ s }: { s: SymbolScore }) {
  const v = VERDICT[s.verdict];
  return (
    <tr className="border-b border-neutral-800/50 last:border-0" title={s.reason}>
      <td className="py-1">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${v.cls}`}>
          {v.icon} {v.label}
        </span>
      </td>
      <td className="py-1">
        <span className={s.enabled ? "text-neutral-200" : "text-neutral-500 line-through"}>
          {s.symbol}
        </span>
        {!s.enabled && <span className="ml-1 text-[10px] text-neutral-600">off</span>}
      </td>
      <td className="py-1 text-right tabular-nums text-neutral-400">{s.trades}</td>
      <td className="py-1 text-right tabular-nums text-neutral-400">{s.win_rate.toFixed(0)}%</td>
      <td
        className={`py-1 text-right tabular-nums font-semibold ${
          s.expectancy_r > 0 ? "text-bull" : s.expectancy_r < 0 ? "text-bear" : "text-neutral-400"
        }`}
      >
        {s.expectancy_r > 0 ? "+" : ""}
        {s.expectancy_r.toFixed(2)}R
      </td>
      <td
        className={`py-1 text-right tabular-nums ${
          s.total_pnl > 0 ? "text-bull" : s.total_pnl < 0 ? "text-bear" : "text-neutral-400"
        }`}
      >
        {s.total_pnl > 0 ? "+" : ""}${s.total_pnl.toFixed(0)}
      </td>
    </tr>
  );
}
