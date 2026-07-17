import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ShadowScorecard, ShadowSide } from "../types";

function pct(v: number | null): string {
  return v == null ? "—" : `${(v * 100).toFixed(0)}%`;
}
function r(v: number | null): string {
  return v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}R`;
}

function Row({ label, s, highlight }: { label: string; s: ShadowSide; highlight?: boolean }) {
  const expTone =
    s.expectancy_r == null ? "text-neutral-500" : s.expectancy_r > 0 ? "text-bull" : "text-bear";
  return (
    <tr className={highlight ? "bg-violet-950/20" : ""}>
      <td className="py-1 pr-2 font-medium text-neutral-200">{label}</td>
      <td className="py-1 pr-2 text-center tabular-nums text-neutral-300">{s.directional}</td>
      <td className="py-1 pr-2 text-center tabular-nums text-neutral-300">{pct(s.win_rate)}</td>
      <td className={`py-1 pr-2 text-center font-semibold tabular-nums ${expTone}`}>{r(s.expectancy_r)}</td>
      <td className="py-1 text-center tabular-nums text-neutral-400">
        {s.stand_aside}
        {s.stand_aside_missed > 0 && <span className="text-amber-400"> ({s.stand_aside_missed}✗)</span>}
      </td>
    </tr>
  );
}

/** AI-vs-deterministic head-to-head over graded shadow decisions. Proof, not a trade surface. */
export function ShadowScorecardPanel() {
  const [card, setCard] = useState<ShadowScorecard | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setBusy(true);
    try {
      setCard(await api.shadowScorecard());
    } catch {
      /* ignore */
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    void load();
  }, []);

  const empty = card && card.evaluated === 0;
  const aiEdge =
    card && card.ai.expectancy_r != null && card.deterministic.expectancy_r != null
      ? card.ai.expectancy_r - card.deterministic.expectancy_r
      : null;

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 p-3 text-xs">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-semibold text-violet-300">🧪 Shadow scorecard — AI vs deterministic</span>
        <button
          onClick={load}
          disabled={busy}
          className="rounded bg-neutral-800 px-2 py-0.5 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50"
          title="Grades any decisions whose horizon has passed, then refreshes"
        >
          {busy ? "…" : "↻"}
        </button>
      </div>

      {!card ? (
        <div className="text-neutral-500">Loading…</div>
      ) : empty ? (
        <div className="text-neutral-500">
          No graded decisions yet. Run analyses with 🤖 AI decides ON; each one is logged and graded
          automatically once ~48 bars of price action have played out.
          {card.pending && <span className="text-neutral-400"> (some are pending — waiting on candles).</span>}
        </div>
      ) : (
        <>
          <table className="w-full">
            <thead>
              <tr className="text-[10px] uppercase tracking-wide text-neutral-500">
                <th className="pb-1 text-left">Decider</th>
                <th className="pb-1 text-center">Trades</th>
                <th className="pb-1 text-center">Win%</th>
                <th className="pb-1 text-center">Exp R</th>
                <th className="pb-1 text-center">Stood aside</th>
              </tr>
            </thead>
            <tbody>
              <Row label="🤖 AI" s={card.ai} highlight />
              <Row label="⚙️ Deterministic" s={card.deterministic} />
            </tbody>
          </table>
          {/* Absolute read first — is EITHER decider actually making money? (the relative edge below
              is secondary; "less bad" is still losing). */}
          {(() => {
            const a = card.ai.expectancy_r, d = card.deterministic.expectancy_r;
            if (a != null && a > 0)
              return <div className="mt-2 rounded bg-bull/10 px-2 py-1 text-bull">✓ The AI is net-positive ({r(a)}/trade) over {card.ai.directional} graded trades.</div>;
            if (a != null && d != null && a < 0 && d < 0)
              return <div className="mt-2 rounded bg-bear/10 px-2 py-1 text-bear">⚠ Both deciders are net-negative in this sample (AI {r(a)}, deterministic {r(d)}) — no profitable edge yet. The proven lever is fewer, higher-quality trades (trend-only mode).</div>;
            return null;
          })()}
          <div className="mt-2 border-t border-neutral-800 pt-1.5 text-[11px]">
            {aiEdge == null ? (
              <span className="text-neutral-500">Not enough directional trades on both sides yet to compare.</span>
            ) : aiEdge > 0.02 ? (
              <span className="text-bull">AI is ahead by {r(aiEdge)} per trade so far.</span>
            ) : aiEdge < -0.02 ? (
              <span className="text-bear">Deterministic is ahead by {r(-aiEdge)} per trade so far.</span>
            ) : (
              <span className="text-neutral-400">AI and deterministic are roughly even so far.</span>
            )}
            <span className="text-neutral-600">
              {" "}
              · {card.evaluated} graded{card.pending ? " · more pending" : ""}. Small samples — read directionally.
            </span>
          </div>
        </>
      )}
    </div>
  );
}
