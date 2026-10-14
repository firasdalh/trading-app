import { fmtPrice } from "../format";
import type { TradeProposal } from "../types";

/** The trade you actually HOLD — its live levels, which a manual move or a trail can take far from the plan. */
export interface LiveLevels {
  direction: string;
  entry: number;
  stop: number | null;
  target: number | null;
  /** The stop it was opened with: R is measured against the risk taken at entry. null = unknown. */
  entryStop?: number | null;
}

interface Props {
  /** The read: its S/R levels and timeframe, and — when no trade is open — the plan's levels. */
  proposal: TradeProposal;
  /** Live price when we have one (the position's mark or the live quote); else the entry-TF last close. */
  livePrice?: number | null;
  /** When a trade is open on this pair, the path is drawn for IT rather than for the plan. */
  position?: LiveLevels | null;
}

interface Level {
  price: number;
  tf: string;
  kind: "support" | "resistance";
}

/**
 * Collect every S/R level the technical read carries, tagged with the timeframe it came from.
 * Higher timeframes are listed first so the de-dupe below keeps the more significant one.
 */
function allLevels(proposal: TradeProposal): Level[] {
  const RANK: Record<string, number> = { "1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "4h": 6, "1d": 7 };
  const out: Level[] = [];
  for (const tf of proposal.technical?.timeframes ?? []) {
    for (const p of tf.support_levels ?? []) out.push({ price: p, tf: tf.timeframe, kind: "support" });
    for (const p of tf.resistance_levels ?? []) out.push({ price: p, tf: tf.timeframe, kind: "resistance" });
  }
  return out
    .filter((l) => Number.isFinite(l.price) && l.price > 0)
    .sort((a, b) => (RANK[b.tf] ?? 0) - (RANK[a.tf] ?? 0));
}

/**
 * Two levels 0.15 ATR apart are the same wall seen on two timeframes, and listing both makes a
 * clean path look crowded. Keep the first (highest timeframe, from the sort above).
 */
function dedupe(levels: Level[], atr: number): Level[] {
  const kept: Level[] = [];
  for (const l of levels) {
    if (!kept.some((k) => Math.abs(k.price - l.price) < 0.15 * atr)) kept.push(l);
  }
  return kept;
}

/**
 * PATH TO TARGET — the part of a setup that the entry/stop/target numbers do not show: what price
 * has to get THROUGH to pay you, and where it would land if it backs off first.
 *
 * The three questions it answers, which the checklist above cannot:
 *   1. How far is each barrier, measured in the market's OWN units (ATR) rather than price? "2.3 ATR
 *      to target" is a reality check; "2,445 points" is not.
 *   2. What is in the way? A resistance between here and the target is where a long stalls, so this
 *      is the honest answer to "is a pullback coming" — not a forecast, a list of the places one
 *      would start from.
 *   3. What is the reward:risk FROM HERE? Once price has moved, the planned R:R is history. A long
 *      that has already run has a worse deal left than the one you approved, and this says so.
 */
export function TradePath({ proposal, livePrice, position }: Props) {
  const live = position != null;
  const entry = live ? position.entry : proposal.entry;
  const stop = live ? position.stop : proposal.stop_loss;
  const target = live ? position.target : proposal.take_profit;
  const direction = live ? position.direction : proposal.direction;
  if (entry == null || stop == null || target == null) return null;
  if (direction !== "long" && direction !== "short") return null;

  const tf0 =
    proposal.technical?.timeframes.find((t) => t.timeframe === proposal.timeframe)
    ?? proposal.technical?.timeframes[0];
  const atr = (tf0?.indicators?.["atr14"] as number | undefined) ?? null;
  const now = livePrice ?? (tf0?.indicators?.["last_close"] as number | undefined) ?? entry;

  const isLong = direction === "long";
  const risk = Math.abs(entry - stop);
  if (risk <= 0) return null;
  // R for a live trade is the risk taken at ENTRY (the stop it opened with), like everywhere else on
  // the desk; a widened stop must not make a dip "cost" fewer R.
  const entryRisk =
    live && position.entryStop != null && Math.abs(entry - position.entryStop) > 0
      ? Math.abs(entry - position.entryStop)
      : risk;

  // Distances FROM HERE (not from the planned entry) — the numbers that are still actionable.
  const toTarget = isLong ? target - now : now - target;
  const toStop = isLong ? now - stop : stop - now;
  const rrHere = toStop > 0 ? toTarget / toStop : null;
  const rrPlan = Math.abs(target - entry) / risk;

  // Progress along the plan: 0% at the stop, 100% at the target.
  const span = isLong ? target - stop : stop - target;
  const pct = (v: number) => Math.max(0, Math.min(100, ((isLong ? v - stop : stop - v) / span) * 100));
  const nowPct = pct(now);
  const entryPct = pct(entry);

  // Barriers the trade still has to clear, and the level a pullback would most likely find.
  const lv = atr && atr > 0 ? dedupe(allLevels(proposal), atr) : [];
  const walls = lv
    .filter((l) => (isLong ? l.price > now && l.price < target : l.price < now && l.price > target))
    .sort((a, b) => (isLong ? a.price - b.price : b.price - a.price))
    .slice(0, 3);
  const floors = lv
    .filter((l) => (isLong ? l.price < now && l.price > stop : l.price > now && l.price < stop))
    .sort((a, b) => (isLong ? b.price - a.price : a.price - b.price));
  const floor = floors[0] ?? null;

  const inAtr = (d: number) => (atr && atr > 0 ? `${(d / atr).toFixed(2)} ATR` : "—");
  const inPct = (d: number) => `${((d / now) * 100).toFixed(2)}%`;
  // Distance expressed in average bars — the cheapest sanity check on whether a target is reachable
  // inside the horizon you are actually trading.
  const barsToTarget = atr && atr > 0 ? toTarget / atr : null;

  const reached = toTarget <= 0;
  const blown = toStop <= 0;
  // Green once price has travelled the way the trade needs it to. Spelled out rather than folded
  // into the marker's className, where it read `nowPct >= entryPct === isLong` — correct, but it
  // looks like a typo.
  const inProfit = isLong ? now >= entry : now <= entry;

  return (
    <div className="rounded-md border border-neutral-800 p-3">
      <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
          Path to target
          {live && (
            <span className="ml-1.5 font-normal normal-case tracking-normal text-neutral-500">
              · your live stop and target
            </span>
          )}
        </span>
        <span className="text-xs text-neutral-500">
          {barsToTarget != null && !reached && (
            <>
              <span className="tabular-nums text-neutral-300">{barsToTarget.toFixed(1)} ATR</span> to go
              {" · "}≈{Math.max(1, Math.round(barsToTarget))} average {proposal.timeframe} bars
            </>
          )}
          {reached && <span className="text-bull">target reached</span>}
        </span>
      </div>

      {/* Stop ——— entry ——— now ——— target, to scale. One glance says how much of the plan is done
          and which side of the entry price is sitting on. */}
      <div className="relative mt-1 h-2 rounded-full bg-neutral-800">
        <div
          className={`absolute inset-y-0 left-0 rounded-l-full ${blown ? "bg-bear/50" : "bg-bear/25"}`}
          style={{ width: `${entryPct}%` }}
          title="Between your stop and your entry — this stretch is the risk you accepted"
        />
        <div
          className="absolute inset-y-0 rounded-r-full bg-bull/25"
          style={{ left: `${entryPct}%`, right: 0 }}
          title="Between your entry and your target — this stretch is the reward"
        />
        {/* entry tick */}
        <div
          className="absolute -top-0.5 h-3 w-px bg-neutral-400"
          style={{ left: `${entryPct}%` }}
          title={`Entry ${fmtPrice(entry)}`}
        />
        {/* live price marker */}
        <div
          className={`absolute -top-1 h-4 w-1 rounded-full ${inProfit ? "bg-bull" : "bg-bear"}`}
          style={{ left: `calc(${nowPct}% - 2px)` }}
          title={`Now ${fmtPrice(now)}`}
        />
      </div>
      <div className="mt-1 flex justify-between text-[11px] tabular-nums text-neutral-500">
        <span className="text-bear">stop {fmtPrice(stop)}</span>
        <span className="text-neutral-300">now {fmtPrice(now)}</span>
        <span className="text-bull">target {fmtPrice(target)}</span>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
        <Cell
          label="To stop"
          value={blown ? "past it" : inAtr(toStop)}
          sub={blown ? "price is through your stop" : inPct(toStop)}
          tone="text-bear"
        />
        <Cell
          label="To target"
          value={reached ? "reached" : inAtr(toTarget)}
          sub={reached ? "at or beyond target" : inPct(toTarget)}
          tone="text-bull"
        />
        <Cell
          label="R:R from here"
          value={rrHere != null && rrHere > 0 ? `${rrHere.toFixed(2)} : 1` : "—"}
          sub={
            rrHere != null && rrHere > 0
              ? live
                // Live levels: "planned" would be wrong once the stop or target has moved — this is
                // simply the same stop and target measured from the entry price.
                ? `${rrPlan.toFixed(2)} from your entry`
                : rrHere >= rrPlan
                  ? `better than the ${rrPlan.toFixed(2)} you planned`
                  : `was ${rrPlan.toFixed(2)} at entry`
              : undefined
          }
          tone={rrHere != null && rrHere >= 1.5 ? "text-bull" : rrHere != null && rrHere < 1 ? "text-bear" : ""}
        />
      </div>

      {/* The pullback question, answered with levels rather than a forecast. */}
      {atr != null && atr > 0 && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
              In the way {walls.length > 0 && `· ${walls.length}`}
            </div>
            {walls.length === 0 ? (
              <div className="text-xs text-neutral-500">
                Clear run — no mapped {isLong ? "resistance" : "support"} between here and the target.
              </div>
            ) : (
              <ul className="space-y-1">
                {walls.map((w, i) => (
                  <li key={`${w.tf}-${w.price}`} className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="tabular-nums text-neutral-200">{fmtPrice(w.price)}</span>
                    <span className="text-neutral-500">
                      {w.tf.toUpperCase()} {w.kind}
                      {i === 0 && <span className="text-warn"> · first test</span>}
                    </span>
                    <span className="tabular-nums text-neutral-400">
                      {inAtr(Math.abs(w.price - now))}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
              If it pulls back
            </div>
            {floor == null ? (
              <div className="text-xs text-neutral-500">
                No mapped {isLong ? "support" : "resistance"} between here and your stop — a pullback
                has nothing to lean on before the stop.
              </div>
            ) : (
              <div className="text-xs">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="tabular-nums text-neutral-200">{fmtPrice(floor.price)}</span>
                  <span className="text-neutral-500">
                    {floor.tf.toUpperCase()} {floor.kind}
                  </span>
                  <span className="tabular-nums text-neutral-400">
                    {inAtr(Math.abs(now - floor.price))}
                  </span>
                </div>
                <div className="mt-1 text-neutral-500">
                  A dip to here is{" "}
                  <span className="text-neutral-300">
                    {(Math.abs(now - floor.price) / entryRisk).toFixed(2)}R
                  </span>{" "}
                  and still {isLong ? "above" : "below"} your stop — normal, not a failure.
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Cell({ label, value, sub, tone = "" }: {
  label: string; value: string; sub?: string; tone?: string;
}) {
  return (
    <div className="rounded bg-neutral-900/50 px-2.5 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`text-sm font-semibold tabular-nums ${tone}`}>{value}</div>
      {sub && <div className="text-[11px] text-neutral-500">{sub}</div>}
    </div>
  );
}
