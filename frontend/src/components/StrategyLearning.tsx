import { useState } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import { localHour } from "../sessions";
import type { LearningLesson, LearningPart, StrategyLearning as Learning, WhatIfTable } from "../types";

/**
 * "What the strategy learned" (asked 2026-11-12: "make the strategy learn from itself, so it keeps getting better").
 * The backend studies the strategy's own closed trades every 2 minutes (app/agents/learning.py): what the market looked
 * like at each entry, what other targets / stops would have made. This card shows what it found - each lesson with its
 * numbers and how sure it is - and the change it would make. Nothing changes until the user presses Apply, only a
 * strong lesson can be applied, never the risk, and an applied one can be undone.
 */

const r1 = (v: number | null | undefined) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}R`);
const pct = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`);
const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";

/**
 * "opened 12:00-13:00 UTC" -> "opened 16:00-17:00 your clock (12:00-13:00 UTC)" in every text of the view. The board's
 * hour chips are drawn on the viewer's clock while the learner writes UTC: on 2026-11-20 the user read "12" in a lesson
 * and switched on the board's 12 - 08:00 UTC, not the hour the lesson meant. Display only.
 */
const HOURS_UTC = /\b(\d{2}):00-(\d{2}):00 UTC\b/g;
function onClock<T>(v: T): T {
  if (typeof v === "string") {
    return v.replace(HOURS_UTC, (all: string, a: string, b: string) => {
      const from = localHour(Number(a));
      return from === `${a}:00` ? all : `${from}-${localHour(Number(b))} your clock (${a}:00-${b}:00 UTC)`;
    }) as T;
  }
  if (Array.isArray(v)) return v.map(onClock) as T;
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, onClock(x)])) as T;
  return v;
}

function Sure({ p, strongT }: { p: { strength: string | null; t: number }; strongT: number }) {
  if (p.strength === "strong")
    return <span className="rounded bg-amber-500/20 px-1.5 text-[10px] font-bold uppercase text-amber-200" title={`t = ${p.t} (strong from ${strongT})`}>sure</span>;
  return (
    <span className="rounded bg-neutral-800 px-1.5 text-[10px] font-semibold uppercase text-neutral-400"
          title={`t = ${p.t}: it needs ${strongT} to be sure - more trades will tell`}>
      watching
    </span>
  );
}

function Numbers({ p }: { p: LearningLesson | LearningPart }) {
  return (
    <span className="text-[11px] tabular-nums text-neutral-400">
      {p.n} trades{p.won != null && <> · {pct(p.won)} won</>} · <b className={p.r_sum >= 0 ? "text-bull" : "text-bear"}>{r1(p.r_sum)}</b>
      {p.pnl != null && <> ({fmtUsd(p.pnl, { sign: true })})</>}
      {" · "}older half {r1(p.halves[0])}, newer half {r1(p.halves[1])}
      {p.rest && <> · the rest: {p.rest.n} trades {r1(p.rest.r_sum)}</>}
    </span>
  );
}

function LessonRow({ p, strongT, busy, blocked, act }: {
  p: LearningLesson; strongT: number; busy: boolean; blocked: string | null;
  act: (key: string, action: "apply" | "ignore" | "unignore" | "undo") => void;
}) {
  const a = p.action;
  const tone = p.kind === "weak" ? "border-bear/30" : p.kind === "sweet" ? "border-bull/30" : "border-sky-500/30";
  return (
    <li className={`rounded-md border ${tone} bg-neutral-950/40 px-2.5 py-1.5`}>
      <div className="flex flex-wrap items-center gap-2">
        <Sure p={p} strongT={strongT} />
        <span className="text-xs font-semibold text-neutral-200">{p.title}</span>
        {p.status === "applied" && <span className="rounded bg-bull/15 px-1.5 text-[10px] font-semibold text-bull">✓ applied</span>}
        {p.first_seen && <span className="text-[10px] text-neutral-500">sure since {when(p.first_seen)}</span>}
      </div>
      <div className="mt-0.5"><Numbers p={p} /></div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px]">
        {!a ? (
          <span className="text-neutral-500">No setting on the board says this yet - nothing to apply.</span>
        ) : a.done ? (
          <span className="text-bull" title={a.text}>✓ The strategy already does this.</span>
        ) : p.strength !== "strong" ? (
          <span className="text-neutral-500">Would: {a.text} - once it is sure.</span>
        ) : (
          <button
            onClick={() => act(p.key, "apply")}
            disabled={busy || !!blocked}
            title={blocked ?? "Changes the strategy now, as a save on the board would - recorded as an edit; Undo puts it back"}
            className="rounded border border-amber-500/60 bg-amber-500/15 px-2 py-0.5 font-semibold text-amber-200 hover:bg-amber-500/25 disabled:opacity-50"
          >
            Apply: {a.text}
          </button>
        )}
        {p.status !== "applied" && (
          <button onClick={() => act(p.key, "ignore")} disabled={busy} className="text-neutral-500 hover:text-neutral-300" title="Hide it; it stays under “ignored”">
            Ignore
          </button>
        )}
      </div>
    </li>
  );
}

/** A lesson it is not sure of yet, on one line: what, how many trades, the R in each half, and what it would change. */
function WatchRow({ p, strongT, busy, act }: {
  p: LearningLesson; strongT: number; busy: boolean;
  act: (key: string, action: "apply" | "ignore" | "unignore" | "undo") => void;
}) {
  const dot = p.kind === "weak" ? "bg-bear" : p.kind === "sweet" ? "bg-bull" : "bg-sky-400";
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 text-[11px] text-neutral-400"
        title={`t = ${p.t}: it needs ${strongT} to be sure${p.action ? ` · would: ${p.action.text}` : " · no setting on the board says this yet"}`}>
      <span className={`inline-block h-1.5 w-1.5 shrink-0 translate-y-[-1px] rounded-full ${dot}`} />
      <span className="text-neutral-300">{p.title}</span>
      <span className="tabular-nums">
        {p.n} trades{p.won != null && <> · {pct(p.won)} won</>} · <b className={p.r_sum >= 0 ? "text-bull/90" : "text-bear/90"}>{r1(p.r_sum)}</b>
        {" "}(halves {r1(p.halves[0])} / {r1(p.halves[1])})
      </span>
      {p.action && !p.action.done && <span className="text-neutral-500">· would change the rules</span>}
      {p.action?.done && <span className="text-bull/80">· already in the rules</span>}
      <button onClick={() => act(p.key, "ignore")} disabled={busy} className="text-neutral-600 hover:text-neutral-300">ignore</button>
    </li>
  );
}

function WhatIfGrid({ w }: { w: WhatIfTable }) {
  const key = (t: number, k: number) => `t${t}|s${k}`;
  const best = w.cells[w.best];
  const cur = w.current ? w.cells[w.current] : null;
  return (
    <div className="space-y-1">
      <div className="overflow-x-auto">
        <table className="text-[11px] tabular-nums">
          <thead>
            <tr className="text-neutral-500">
              <th className="pr-2 text-left font-normal">stop ↓ · target →</th>
              {w.targets.map((t) => <th key={t} className="px-1.5 text-right font-normal">{t}R</th>)}
            </tr>
          </thead>
          <tbody>
            {w.stops.map((k) => (
              <tr key={k}>
                <td className="pr-2 text-neutral-500">{k === 1 ? "as now" : `${k}× as wide`}</td>
                {w.targets.map((t) => {
                  const id = key(t, k);
                  const c = w.cells[id];
                  const isCur = id === w.current;
                  const isBest = id === w.best;
                  return (
                    <td key={t} title={`${c.n} trades · ${pct(c.won)} won · held ~${c.hold_min ?? "?"} min`}
                        className={`px-1.5 text-right ${isCur ? "rounded outline outline-1 outline-amber-400" : ""} ${
                          isBest ? "font-bold text-bull" : c.r >= 0 ? "text-neutral-300" : "text-bear/80"}`}>
                      {r1(c.r)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[11px] leading-relaxed text-neutral-500">
        Each of {w.n} entries replayed on the broker's prices with that target and stop, then taken in order as the strategy could
        have: one trade per pair, {w.rules.cooldown_min} min after a close, {w.rules.loss_cooldown_min} min on the same side after
        a loss, {w.rules.max_open || "no"} open at most - a longer hold loses the entries that came meanwhile. Your setting
        now (framed): {cur ? `${r1(cur.r)} over ${cur.n} trades` : "not on the grid"}; what really happened: {r1(w.actual_r)}.
        {w.gain && w.best !== w.current && (
          <> Best: {w.best.replace("t", "target ").replace("|s", "R, stop ×")} at {r1(best.r)} - {w.gain.halves.every((x) => x > 0)
            ? `better in both halves (${r1(w.gain.halves[0])}, ${r1(w.gain.halves[1])})`
            : `but not in both halves (${r1(w.gain.halves[0])}, ${r1(w.gain.halves[1])}): luck so far`}.</>
        )}
      </p>
    </div>
  );
}

export function StrategyLearning({ strategyId, dirty }: { strategyId: number; dirty: boolean }) {
  const [tick, setTick] = useState(0);
  const { data, error } = usePolling(() => api.strategyLearning(strategyId), 60_000, [strategyId, tick]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [showIgnored, setShowIgnored] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [moreWatching, setMoreWatching] = useState(false);
  const [view, setView] = useState<Learning | null>(null);
  const raw = view && view.strategy_id === strategyId && data && view.now >= data.now ? view : data;
  const L = raw ? onClock(raw) : raw;

  if (error && !L) {
    return /404|not found/i.test(error) ? null : (
      <section className="rounded-lg border border-neutral-800 bg-neutral-900/30 p-2.5 text-xs text-neutral-500">
        🧠 What the strategy learned: unavailable ({error})
      </section>
    );
  }
  if (!L) return null;

  const act = async (key: string, action: "apply" | "ignore" | "unignore" | "undo") => {
    setBusy(true);
    setMsg(null);
    try {
      const out = await api.strategyLearningAct(strategyId, key, action);
      setView(out.learning);
      setTick((t) => t + 1);
      setMsg({ tone: "ok", text: action === "apply" ? "Applied - the strategy uses it from its next look. Undo below puts it back."
        : action === "undo" ? "Undone - the strategy is back as it was." : action === "ignore" ? "Ignored." : "Shown again." });
    } catch (e) {
      setMsg({ tone: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const strong = L.lessons.filter((p) => p.strength === "strong");
  const watching = L.lessons.filter((p) => p.strength !== "strong");
  const v = L.version;
  const reading = L.with_fp < L.trades;
  const blocked = dirty ? "Save or undo your edits on the board first - Apply changes the saved strategy" : null;

  return (
    <section className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-900/30 p-2.5">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-black tracking-wide text-neutral-200">🧠 What the strategy learned</h3>
        <span className="text-[11px] text-neutral-500">
          it studies its own closed trades every 2 min · nothing changes until you press Apply, and never the risk
        </span>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-400">
        <span>
          <b className="text-neutral-200">{L.trades}</b> trades studied{L.since && <> since {when(L.since)}</>}
          {L.total && <> · <b className={L.total.r_sum >= 0 ? "text-bull" : "text-bear"}>{r1(L.total.r_sum)}</b> ({fmtUsd(L.total.pnl, { sign: true })}) · {pct(L.total.won)} won</>}
        </span>
        {reading && <span className="text-sky-300">reading their history from the broker: {L.with_fp} / {L.trades}</span>}
        <span title="A version of the strategy needs about 50 trades before its results say much; every edit starts a new one">
          this version: <b className="text-neutral-200">{v.trades}</b> trade{v.trades === 1 ? "" : "s"}
          {v.since && <> since your last edit ({when(v.since)})</>}
          {v.trades > 0 && <> · {r1(v.r_sum)} ({fmtUsd(v.pnl, { sign: true })})</>}
          {v.trades < v.fair_after && <span className="text-neutral-500"> - judge it after ~{v.fair_after}</span>}
          <span className="ml-1.5 inline-block h-1.5 w-16 overflow-hidden rounded bg-neutral-800 align-middle">
            <span className="block h-full bg-amber-500/70" style={{ width: `${Math.min(100, (100 * v.trades) / v.fair_after)}%` }} />
          </span>
        </span>
      </div>

      {L.with_r < L.min_total ? (
        <p className="text-xs text-neutral-500">It starts judging at {L.min_total} closed trades - {L.with_r} so far.</p>
      ) : (
        <>
          {strong.length === 0 && (
            <p className="text-xs text-neutral-400">
              Nothing is sure enough yet to change the strategy. A lesson needs at least {L.min_trades} trades on each side, the same
              verdict in the older and the newer half, and a gap luck rarely makes{watching.length ? " - what it is watching:" : "."}
            </p>
          )}
          {strong.length > 0 && (
            <ul className="space-y-1.5">
              {strong.map((p) => (
                <LessonRow key={p.key} p={p} strongT={L.strong_t} busy={busy} blocked={blocked} act={(k, a) => void act(k, a)} />
              ))}
            </ul>
          )}
          {watching.length > 0 && (
            <div>
              {strong.length > 0 && (
                <h4 className="mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">Watching - not sure yet</h4>
              )}
              <ul className="space-y-0.5">
                {(moreWatching ? watching : watching.slice(0, 4)).map((p) => (
                  <WatchRow key={p.key} p={p} strongT={L.strong_t} busy={busy} act={(k, a) => void act(k, a)} />
                ))}
              </ul>
              {watching.length > 4 && (
                <button onClick={() => setMoreWatching((m) => !m)} className="mt-0.5 text-[11px] text-sky-300 hover:text-sky-200">
                  {moreWatching ? "show fewer" : `show ${watching.length - 4} more`}
                </button>
              )}
            </div>
          )}
        </>
      )}

      {L.applied.length > 0 && (
        <div className="space-y-1">
          <h4 className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">Applied</h4>
          {L.applied.map((a) => (
            <div key={a.key} className="flex flex-wrap items-center gap-2 rounded border border-neutral-800 px-2 py-1 text-[11px] text-neutral-400">
              <span className="text-neutral-200">{a.title}</span>
              <span>{when(a.at)}</span>
              {a.changes && <span className="text-neutral-500">{a.changes.join(" · ")}</span>}
              <span className="tabular-nums">
                since: {a.since.n} trade{a.since.n === 1 ? "" : "s"}
                {a.since.avg_r != null && <> · {r1(a.since.avg_r)} a trade</>}
                {a.before.avg_r != null && <> (before: {r1(a.before.avg_r)})</>}
              </span>
              <button onClick={() => void act(a.key, "undo")} disabled={busy || !!blocked} title={blocked ?? "Put the strategy back as it was before this lesson"}
                      className="ml-auto text-neutral-400 hover:text-bear disabled:opacity-50">
                Undo
              </button>
            </div>
          ))}
        </div>
      )}

      {L.whatif && (
        <details>
          <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Targets and stop sizes, replayed ({L.whatif.n} trades)
          </summary>
          <div className="mt-1"><WhatIfGrid w={L.whatif} /></div>
        </details>
      )}

      {L.parts.length > 0 && (
        <details open={showAll} onToggle={(e) => setShowAll((e.target as HTMLDetailsElement).open)}>
          <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Everything it checked ({L.parts.length})
          </summary>
          {showAll && (
            <div className="mt-1 overflow-x-auto">
              <table className="text-[11px] tabular-nums">
                <thead>
                  <tr className="text-neutral-500">
                    <th className="pr-2 text-left font-normal">when the entry had…</th>
                    <th className="px-1.5 text-right font-normal">trades</th>
                    <th className="px-1.5 text-right font-normal">won</th>
                    <th className="px-1.5 text-right font-normal">R</th>
                    <th className="px-1.5 text-right font-normal">older / newer half</th>
                    <th className="px-1.5 text-right font-normal">t</th>
                  </tr>
                </thead>
                <tbody>
                  {L.parts.map((p) => (
                    <tr key={p.key} className={p.kind === "weak" ? "text-bear/90" : p.kind === "sweet" ? "text-bull/90" : "text-neutral-400"}>
                      <td className="pr-2">{p.label}</td>
                      <td className="px-1.5 text-right">{p.n}</td>
                      <td className="px-1.5 text-right">{pct(p.won)}</td>
                      <td className="px-1.5 text-right">{r1(p.r_sum)}</td>
                      <td className="px-1.5 text-right">{r1(p.halves[0])} / {r1(p.halves[1])}</td>
                      <td className="px-1.5 text-right">{p.t.toFixed(1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </details>
      )}

      {L.ignored.length > 0 && (
        <div className="text-[11px] text-neutral-500">
          <button onClick={() => setShowIgnored((s) => !s)} className="hover:text-neutral-300">
            {L.ignored.length} ignored {showIgnored ? "▾" : "▸"}
          </button>
          {showIgnored && (
            <ul className="mt-1 space-y-0.5">
              {L.ignored.map((p) => (
                <li key={p.key} className="flex flex-wrap items-center gap-2">
                  <span className="text-neutral-400">{p.title}</span>
                  <button onClick={() => void act(p.key, "unignore")} disabled={busy} className="text-sky-300 hover:text-sky-200">Show again</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {msg && <div className={`rounded border px-2 py-1 text-[11px] ${msg.tone === "ok" ? "border-bull/40 bg-bull/10 text-bull" : "border-bear/40 bg-bear/10 text-bear"}`}>{msg.text}</div>}
    </section>
  );
}
