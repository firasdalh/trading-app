import { useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import { SESSIONS, sessionAtHour } from "../sessions";
import type { EquityTrade, RiskState, SignalReview, SignalReviewEdit, SignalReviewSignal, StrategyView } from "../types";
import { FoldButton, useFold } from "./JournalFold";

/**
 * 👁 SIGNAL REVIEW (asked 2026-11-08: "keep track in a separate place in the journal ... the result of those
 * signals, to learn from it ... so I can enhance it in the strategy ... after I change I need a brief result of
 * those signals"). Every signal a strategy group on watch logged, judged as the trade it would have been - in at
 * its price, out at the strategy's own stop or target, whichever came first (backend signal_review.py) - and
 * split at each recorded change to the strategy, newest first, so a change is judged by the signals after it.
 * In the signals' fuchsia, like their pop-up and their 👁 markers on the chart.
 *
 * SUMMARY (asked 2026-11-19: "a summary review of the signal review - if it was open, the win rate, how much money
 * it made, how much it lost, and other important review, organised"): the whole window in tiles (win rate, net,
 * money won / lost, profit factor, per signal, worst drawdown), a line of facts (runs, hold times, near misses,
 * best / worst day) and where it works - by side, session, pair, day. A signal's money = its R × "1R", which is
 * the strategy's risk now (its risk % × today's starting equity) unless typed over. All computed here.
 *
 * ⚡ STRATEGY REVIEW (asked the same day: "the same board for the working strategy, a summary like this"): the
 * same summary and the same split at each change, on the live strategy's REAL closed trades - the broker's $ and
 * R from the journal (GET /api/journal/equity). Its trades are listed in Closed trades, so no list here.
 */

const POLL_MS = 60_000;
const SHOWN_CHANGES = 3;

// The signal review is big (every signal replayed, ~1.5 MB for 30 days): both cards read it, once per minute.
const reviewCache = new Map<number, { at: number; p: Promise<SignalReview> }>();
function fetchReview(days: number): Promise<SignalReview> {
  const hit = reviewCache.get(days);
  if (hit && Date.now() - hit.at < POLL_MS - 5_000) return hit.p;
  const p = api.signalReview(days);
  reviewCache.set(days, { at: Date.now(), p });
  p.catch(() => reviewCache.delete(days));
  return p;
}

/** A result the review adds up: a watch signal replayed, or a real trade of the strategy. */
type Outcome = {
  id: string;
  at: string;                 // the signal / the trade's open
  exit_at: string | null;
  day: string;                // its UTC day: a signal's own, a trade's close (the day its money landed)
  side: "long" | "short" | null;
  symbol: string | null;
  result: SignalReviewSignal["result"];
  r: number | null;
  usd: number | null;         // a trade's real $; a signal's R × 1R
  mfe_r?: number | null;
  mae_r?: number | null;
  estimated?: boolean;
  group?: string;
};

const isDone = (x: Outcome) =>
  (x.result === "win" || x.result === "loss" || x.result === "timeout") && (x.r != null || x.usd != null);

const fromSignal = (x: SignalReviewSignal, perR: number | null): Outcome => ({
  id: `s${x.id}`, at: x.at, exit_at: x.exit_at ?? null, day: x.at.slice(0, 10), side: x.side, symbol: x.symbol,
  result: x.result, r: x.r ?? null, usd: x.r != null && perR != null ? x.r * perR : null,
  mfe_r: x.mfe_r, mae_r: x.mae_r, estimated: x.levels === "estimated",
  group: `${x.name ?? "Strategy"} · group ${x.group ?? "?"}`,
});

const fromTrade = (t: EquityTrade, i: number): Outcome => {
  const d = (t.direction || "").toLowerCase();
  return {
    id: `t${i}`, at: t.opened_at ?? t.closed_at, exit_at: t.closed_at, day: new Date(t.closed_at).toISOString().slice(0, 10),
    side: d === "long" ? "long" : d === "short" ? "short" : null, symbol: t.symbol,
    result: t.pnl > 0 ? "win" : "loss", r: t.r, usd: t.pnl,
  };
};

const rText = (r: number) => `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(2)}R`;
const r1 = (r: number) => `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(1)}R`;
// "+$1,234" / "−$56": whole dollars.
const usd0 = (v: number) => `${v < 0 ? "−" : "+"}$${Math.round(Math.abs(v)).toLocaleString()}`;
// The same, with cents under $10 - an average per trade of +$0.04 is not "+$0".
const usdFine = (v: number) => (Math.abs(v) < 10 ? `${v < 0 ? "−" : "+"}$${Math.abs(v).toFixed(2)}` : usd0(v));
const tone = (v: number) => (v > 0 ? "text-bull" : v < 0 ? "text-bear" : "text-neutral-300");
const pctOf = (a: number, b: number) => (b > 0 ? `${Math.round((a / b) * 100)}%` : "—");
const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;
const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
// A signal's prices in the decimals its own price is quoted in - an estimated level is a raw computed number.
const decimalsOf = (v: number | null | undefined): number | undefined => {
  if (v == null || !Number.isFinite(v)) return undefined;
  const t = String(v);
  const i = t.indexOf(".");
  return Math.min(5, Math.max(2, i < 0 ? 0 : t.length - i - 1));
};
const minutesText = (m: number) => (m < 60 ? `${Math.round(m)}m` : `${Math.floor(m / 60)}h ${String(Math.round(m % 60)).padStart(2, "0")}m`);
const held = (from: string, to: string | null | undefined) => {
  if (!to) return null;
  return minutesText(Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 60_000)));
};
// "Thu 19 Nov" - a UTC day (the account's day: the header's Day P&L and the daily target run 00:00-00:00 UTC).
const dayName = (key: string) =>
  new Date(`${key}T00:00:00Z`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

const HEAD = "text-[11px] font-semibold uppercase tracking-wide text-neutral-500";

// --------------------------------------------------------------------------- //
//  The brief result of a set (one per change)
// --------------------------------------------------------------------------- //

type Stat = { n: number; wins: number; losses: number; timeouts: number; open: number; r: number; rN: number; usd: number; usdN: number };

function stat(list: Outcome[]): Stat {
  const s: Stat = { n: list.length, wins: 0, losses: 0, timeouts: 0, open: 0, r: 0, rN: 0, usd: 0, usdN: 0 };
  for (const x of list) {
    if (x.result === "win") s.wins++;
    else if (x.result === "loss") s.losses++;
    else if (x.result === "timeout") s.timeouts++;
    else if (x.result === "open") s.open++;
    if (isDone(x)) {
      if (x.r != null) { s.r += x.r; s.rN++; }
      if (x.usd != null) { s.usd += x.usd; s.usdN++; }
    }
  }
  return s;
}

/** "6 signals · 2 ✓ 4 ✗ · 33% won · −2.40R · −$151" - the brief result of a set. */
function Brief({ list, noun, big = false }: { list: Outcome[]; noun: string; big?: boolean }) {
  const s = stat(list);
  if (!s.n) return <span className="text-neutral-500">no {noun}s</span>;
  const wr = s.wins + s.losses ? Math.round((s.wins / (s.wins + s.losses)) * 100) : null;
  return (
    <span className={`flex flex-wrap items-baseline gap-x-2 tabular-nums ${big ? "text-sm" : "text-xs"}`}>
      <span className="text-neutral-300">{plural(s.n, noun)}</span>
      <span className="text-bull">{s.wins} ✓</span>
      <span className="text-bear">{s.losses} ✗</span>
      {s.timeouts > 0 && <span className="text-neutral-400" title="Still open a day later: judged where price was then">{s.timeouts} ⏱</span>}
      {s.open > 0 && <span className="text-neutral-500" title="Neither its stop nor its target yet">{s.open} open</span>}
      {wr != null && <span className="text-neutral-400">{wr}% won</span>}
      {s.rN > 0 && <b className={tone(s.r)}>{rText(s.r)}</b>}
      {s.usdN > 0 && <b className={tone(s.usd)}>{usd0(s.usd)}</b>}
    </span>
  );
}

/** The brief result, and the same for each side - a BUY side and a SELL side can be very different. */
function BriefBySide({ list, noun }: { list: Outcome[]; noun: string }) {
  const buys = list.filter((x) => x.side === "long");
  const sells = list.filter((x) => x.side === "short");
  return (
    <div className="space-y-0.5">
      <Brief list={list} noun={noun} big />
      <div className="flex flex-wrap gap-x-5 gap-y-0.5 pl-0.5">
        <span className="flex items-baseline gap-1.5"><span className="text-[11px] font-semibold text-bull">▲ BUY</span><Brief list={buys} noun={noun} /></span>
        <span className="flex items-baseline gap-1.5"><span className="text-[11px] font-semibold text-bear">▼ SELL</span><Brief list={sells} noun={noun} /></span>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  The summary
// --------------------------------------------------------------------------- //

/** One line of a breakdown: a side, a session, a pair or a day. */
type Part = { key: string; name: string; dot?: string; n: number; wins: number; losses: number; r: number; usd: number };

function parts(list: Outcome[], keyOf: (x: Outcome) => string, nameOf: (k: string) => string,
               dotOf?: (k: string) => string | undefined): Part[] {
  const m = new Map<string, Part>();
  for (const x of list) {
    if (!isDone(x)) continue;
    const k = keyOf(x);
    let p = m.get(k);
    if (!p) {
      p = { key: k, name: nameOf(k), dot: dotOf?.(k), n: 0, wins: 0, losses: 0, r: 0, usd: 0 };
      m.set(k, p);
    }
    p.n++;
    p.r += x.r ?? 0;
    p.usd += x.usd ?? 0;
    if (x.result === "win") p.wins++;
    else if (x.result === "loss") p.losses++;
  }
  return [...m.values()];
}

type Sum = {
  n: number; done: number; open: number; timeouts: number; unknown: number; estimated: number;
  wins: number; losses: number;
  money: boolean;                 // every finished one has $: amounts in $, else in R
  r: number; usd: number; won: number; lost: number; pos: number; neg: number;
  ddR: number; ddUsd: number; winRun: number; lossRun: number;
  holdWin: number | null; holdLoss: number | null;
  paths: boolean; nearMiss: number; scareWin: number;
  bestOne: number | null; worstOne: number | null;
  first: string | null;
};

function summarize(list: Outcome[]): Sum {
  const done = list.filter(isDone);
  const money = done.length > 0 && done.every((x) => x.usd != null);
  const val = (x: Outcome) => (money ? (x.usd as number) : (x.r ?? 0));
  const s: Sum = {
    n: list.length, done: done.length, open: 0, timeouts: 0, unknown: 0, estimated: 0, wins: 0, losses: 0, money,
    r: 0, usd: 0, won: 0, lost: 0, pos: 0, neg: 0, ddR: 0, ddUsd: 0, winRun: 0, lossRun: 0, holdWin: null, holdLoss: null,
    paths: done.some((x) => x.mfe_r != null), nearMiss: 0, scareWin: 0, bestOne: null, worstOne: null, first: null,
  };
  const holds: Record<"win" | "loss", number[]> = { win: [], loss: [] };
  for (const x of list) {
    if (x.result === "open") s.open++;
    else if (x.result === "unknown") s.unknown++;
    if (x.estimated) s.estimated++;
    if (!s.first || Date.parse(x.at) < Date.parse(s.first)) s.first = x.at;
  }
  for (const x of done) {
    const v = val(x);
    s.r += x.r ?? 0;
    s.usd += x.usd ?? 0;
    if (v > 0) { s.won += v; s.pos++; } else if (v < 0) { s.lost -= v; s.neg++; }
    s.bestOne = s.bestOne == null ? v : Math.max(s.bestOne, v);
    s.worstOne = s.worstOne == null ? v : Math.min(s.worstOne, v);
    if (x.result === "timeout") s.timeouts++;
    if (x.result === "win") {
      s.wins++;
      if ((x.mae_r ?? 0) >= 0.5) s.scareWin++;
    } else if (x.result === "loss") {
      s.losses++;
      if ((x.mfe_r ?? 0) >= 0.5) s.nearMiss++;
    }
    if ((x.result === "win" || x.result === "loss") && x.exit_at) {
      holds[x.result].push(Math.max(0, (Date.parse(x.exit_at) - Date.parse(x.at)) / 60_000));
    }
  }
  const mean = (a: number[]) => (a.length ? a.reduce((p, q) => p + q, 0) / a.length : null);
  s.holdWin = mean(holds.win);
  s.holdLoss = mean(holds.loss);
  // The worst fall from a high, every one taken, counted when its result landed.
  let cr = 0, pr = 0, cu = 0, pu = 0;
  for (const x of [...done].sort((a, b) => Date.parse(a.exit_at ?? a.at) - Date.parse(b.exit_at ?? b.at))) {
    cr += x.r ?? 0; pr = Math.max(pr, cr); s.ddR = Math.min(s.ddR, cr - pr);
    cu += x.usd ?? 0; pu = Math.max(pu, cu); s.ddUsd = Math.min(s.ddUsd, cu - pu);
  }
  // The longest runs of wins / losses in a row, in the order they came.
  let w = 0, l = 0;
  for (const x of [...done].sort((a, b) => Date.parse(a.at) - Date.parse(b.at))) {
    const v = val(x);
    if (v > 0) { w++; l = 0; } else if (v < 0) { l++; w = 0; } else { w = 0; l = 0; }
    s.winRun = Math.max(s.winRun, w);
    s.lossRun = Math.max(s.lossRun, l);
  }
  return s;
}

function Tile({ label, value, sub, cls, title }: { label: string; value: string; sub?: string; cls?: string; title?: string }) {
  return (
    <div className="min-w-0 rounded-md bg-neutral-800/50 px-2.5 py-1.5" title={title}>
      <div className="truncate text-[11px] text-neutral-400">{label}</div>
      <div className={`truncate text-lg font-semibold leading-tight tabular-nums ${cls ?? "text-neutral-100"}`}>{value}</div>
      {sub && <div className="truncate text-[11px] tabular-nums text-neutral-500">{sub}</div>}
    </div>
  );
}

/** Left of the middle line for a loss, right of it for a gain - its length against the biggest in the table. */
function Bar({ v, max }: { v: number; max: number }) {
  const w = `${Math.max(4, Math.min(100, (Math.abs(v) / max) * 100))}%`;
  return (
    <div className="flex h-2 w-16 items-center">
      <div className="flex h-full flex-1 justify-end">{v < 0 && <div className="h-full rounded-l-sm bg-bear/70" style={{ width: w }} />}</div>
      <div className="h-3 w-px bg-neutral-600" />
      <div className="flex h-full flex-1">{v > 0 && <div className="h-full rounded-r-sm bg-bull/70" style={{ width: w }} />}</div>
    </div>
  );
}

function PartTable({ title, note, rows, money, noun, usdHead, scroll = false }: {
  title: string; note?: string; rows: Part[]; money: boolean; noun: string; usdHead: string; scroll?: boolean;
}) {
  const val = (p: Part) => (money ? p.usd : p.r);
  const max = Math.max(1e-9, ...rows.map((p) => Math.abs(val(p))));
  return (
    <div className="min-w-0">
      <div className="mb-0.5 flex items-baseline gap-2">
        <span className={HEAD}>{title}</span>
        {note && <span className="text-[11px] text-neutral-600">{note}</span>}
      </div>
      {rows.length === 0 ? (
        <div className="text-[11px] text-neutral-600">nothing finished yet</div>
      ) : (
        <div className={scroll ? "max-h-56 overflow-auto pr-1" : ""}>
          <table className="w-full whitespace-nowrap text-xs">
            <thead className="sticky top-0 bg-neutral-900 text-left text-[11px] text-neutral-500">
              <tr>
                <th className="pb-0.5 pr-2 font-normal" />
                <th className="pr-2 text-right font-normal">{noun[0].toUpperCase() + noun.slice(1)}s</th>
                <th className="pr-2 text-right font-normal">Won</th>
                <th className="pr-2 text-right font-normal">R</th>
                {money && <th className="pr-2 text-right font-normal">{usdHead}</th>}
                <th className="w-16 font-normal" />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const wr = p.wins + p.losses ? Math.round((p.wins / (p.wins + p.losses)) * 100) : null;
                return (
                  <tr
                    key={p.key}
                    className="border-t border-neutral-800/70 hover:bg-neutral-800/30"
                    title={`${p.name}: ${plural(p.n, noun)} · ${p.wins} won · ${p.losses} lost${wr != null ? ` (${wr}%)` : ""} · ${rText(p.r)}${money ? ` · ${usd0(p.usd)}` : ""}`}
                  >
                    <td className="max-w-[9rem] truncate py-0.5 pr-2 text-neutral-200">
                      {p.dot && <span className="mr-1.5 inline-block h-2 w-2 rounded-full" style={{ background: `rgb(${p.dot})` }} />}
                      {p.name}
                    </td>
                    <td className="pr-2 text-right tabular-nums text-neutral-300">{p.n}</td>
                    <td className="pr-2 text-right tabular-nums text-neutral-300">{wr != null ? `${wr}%` : "—"}</td>
                    <td className={`pr-2 text-right tabular-nums ${tone(p.r)}`}>{r1(p.r)}</td>
                    {money && <td className={`pr-2 text-right font-semibold tabular-nums ${tone(p.usd)}`}>{usd0(p.usd)}</td>}
                    <td className="w-16"><Bar v={val(p)} max={max} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Summary({ list, noun, about, control, netLabel, netTitle, usdHead, dayNote }: {
  list: Outcome[];
  noun: string;            // "signal" / "trade"
  about: string;           // what the window is, after "last N days"
  control?: ReactNode;     // the right end of the title line (the signals' 1R)
  netLabel: string;
  netTitle: string;
  usdHead: string;         // the $ column's heading
  dayNote: string;
}) {
  const s = useMemo(() => summarize(list), [list]);
  const bySide = useMemo(() => parts(list, (x) => x.side ?? "?", (k) => (k === "long" ? "▲ BUY" : k === "short" ? "▼ SELL" : "?"))
    .sort((a, b) => (a.key === "long" ? -1 : b.key === "long" ? 1 : 0)), [list]);
  const bySession = useMemo(() => {
    const ps = parts(list, (x) => sessionAtHour(new Date(x.at).getUTCHours()).key,
      (k) => SESSIONS.find((q) => q.key === k)?.name ?? k, (k) => SESSIONS.find((q) => q.key === k)?.rgb);
    const order = SESSIONS.map((q) => q.key as string);
    return ps.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  }, [list]);
  const byGroup = useMemo(() => parts(list, (x) => x.group ?? "", (k) => k).filter((p) => p.key !== ""), [list]);
  const v = (p: Part) => (s.money ? p.usd : p.r);
  const byPair = useMemo(() => parts(list, (x) => x.symbol ?? "?", (k) => (k === "?" ? "?" : displaySymbol(k)))
    .sort((a, b) => (s.money ? b.usd - a.usd : b.r - a.r)), [list, s.money]);
  const byDay = useMemo(() => parts(list, (x) => x.day, dayName).sort((a, b) => (a.key < b.key ? 1 : -1)), [list]);
  const best = byDay.length ? byDay.reduce((a, b) => (v(b) > v(a) ? b : a)) : null;
  const worst = byDay.length ? byDay.reduce((a, b) => (v(b) < v(a) ? b : a)) : null;
  // An amount in $ when every one has its $, else in R; R under it when $ leads.
  const amt = (x: number) => (s.money ? usd0(x) : r1(x));
  const decided = s.wins + s.losses;
  // The win rate it needs to pay: the average loss over (the average win + the average loss).
  const avgWin = s.pos ? s.won / s.pos : null;
  const avgLoss = s.neg ? s.lost / s.neg : null;
  const breakeven = avgWin != null && avgLoss != null ? avgLoss / (avgWin + avgLoss) : null;
  const pf = s.lost > 0 ? (s.won / s.lost).toFixed(2) : s.won > 0 ? "∞" : "—";
  const net = s.money ? s.usd : s.r;
  const nounCap = noun[0].toUpperCase() + noun.slice(1);

  return (
    <div className="rounded-md border border-neutral-700/60 bg-neutral-950/30 px-2.5 py-2">
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={HEAD}>Summary</span>
        <span className="text-[11px] text-neutral-500">{about}{s.first ? ` · first ${noun} ${when(s.first)}` : ""}</span>
        {control}
      </div>

      {s.done === 0 ? (
        <div className="text-xs text-neutral-500">No finished {noun}s in this window{s.open ? ` - ${s.open} still open` : ""}.</div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-8">
            <Tile label={`${nounCap}s`} value={String(s.n)}
                  sub={[`${s.done} finished`, s.open ? `${s.open} open` : "", s.timeouts ? `${s.timeouts} ⏱` : "", s.unknown ? `${s.unknown} ?` : ""].filter(Boolean).join(" · ")}
                  title={noun === "signal" ? "Finished = reached its target or its stop, or judged a day later (⏱). Open = neither yet. ? = could not be judged." : "Closed trades opened in this window."} />
            <Tile label="Win rate" value={pctOf(s.wins, decided)}
                  sub={`${s.wins} ✓ · ${s.losses} ✗${breakeven != null ? ` · pays from ${Math.round(breakeven * 100)}%` : ""}`}
                  cls={breakeven == null || !decided ? "text-neutral-100" : s.wins / decided >= breakeven ? "text-bull" : "text-bear"}
                  title={`Of the ${noun}s that won or lost.${avgWin != null && avgLoss != null && breakeven != null ? ` Wins of ${amt(avgWin)} and losses of ${amt(-avgLoss)} on average: it needs ${(breakeven * 100).toFixed(1)}% to break even.` : ""}`} />
            <Tile label={netLabel} value={amt(net)} sub={s.money ? r1(s.r) : undefined} cls={tone(net)} title={netTitle} />
            <Tile label="Money won" value={amt(s.won)} sub={`${s.pos} in profit${avgWin != null ? ` · ${amt(avgWin)} each` : ""}`} cls="text-bull"
                  title={`Every ${noun} that ended in profit${noun === "signal" ? " - the target, or a ⏱ above its entry" : ""}.`} />
            <Tile label="Money lost" value={amt(-s.lost)} sub={`${s.neg} at a loss${avgLoss != null ? ` · ${amt(-avgLoss)} each` : ""}`} cls="text-bear"
                  title={`Every ${noun} that ended at a loss${noun === "signal" ? " - the stop, or a ⏱ below its entry" : ""}.`} />
            <Tile label="Profit factor" value={pf} sub="won ÷ lost · above 1 pays" cls={s.lost > 0 && s.won / s.lost >= 1 ? "text-bull" : "text-bear"} />
            <Tile label={`Per ${noun}`} value={s.money ? usdFine(net / s.done) : r1(net / s.done)} sub={s.money ? `${rText(s.r / s.done)} on average` : "on average"} cls={tone(net)} />
            <Tile label="Worst drawdown" value={amt(s.money ? s.ddUsd : s.ddR)} sub={`${s.money ? `${r1(s.ddR)} · ` : ""}high to low`}
                  cls={(s.money ? s.ddUsd : s.ddR) < 0 ? "text-bear" : "text-neutral-300"}
                  title="The deepest fall from a high, each counted when its result came." />
          </div>

          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-neutral-400">
            <span>Longest run: <b className="text-bull">{s.winRun} wins</b> · <b className="text-bear">{s.lossRun} losses</b> in a row</span>
            {(s.holdWin != null || s.holdLoss != null) && (
              <span>Held on average: wins ~<b className="text-neutral-200">{s.holdWin != null ? minutesText(s.holdWin) : "—"}</b> · losses ~<b className="text-neutral-200">{s.holdLoss != null ? minutesText(s.holdLoss) : "—"}</b></span>
            )}
            {noun === "trade" && s.bestOne != null && s.worstOne != null && (
              <span>Biggest win <b className="text-bull">{amt(s.bestOne)}</b> · biggest loss <b className="text-bear">{amt(s.worstOne)}</b></span>
            )}
            {s.paths && (
              <span title="Losses that went at least +0.5R their way before the stop.">
                Lost after being +0.5R up: <b className="text-neutral-200">{s.nearMiss}</b> of {s.losses} ({pctOf(s.nearMiss, s.losses)})
              </span>
            )}
            {s.paths && (
              <span title="Wins that went at least 0.5R against them before the target.">
                Won after being −0.5R down: <b className="text-neutral-200">{s.scareWin}</b> of {s.wins} ({pctOf(s.scareWin, s.wins)})
              </span>
            )}
            {best && v(best) > 0 && <span>Best day: <b className="text-bull">{best.name} {amt(v(best))}</b></span>}
            {worst && v(worst) < 0 && <span>Worst day: <b className="text-bear">{worst.name} {amt(v(worst))}</b></span>}
            {s.estimated > 0 && <span className="text-neutral-500">≈ {s.estimated} with estimated levels</span>}
          </div>

          <div className="mt-3 grid gap-x-6 gap-y-3 lg:grid-cols-3">
            <div className="space-y-3">
              <PartTable title="By side" rows={bySide} money={s.money} noun={noun} usdHead={usdHead} />
              <PartTable title="By session" note={`${noun === "trade" ? "when it opened" : "when the signal came"} · UTC hours`} rows={bySession} money={s.money} noun={noun} usdHead={usdHead} />
              {byGroup.length > 1 && <PartTable title="By group" rows={byGroup.sort((a, b) => v(b) - v(a))} money={s.money} noun={noun} usdHead={usdHead} />}
            </div>
            <PartTable title="By pair" note="best first" rows={byPair} money={s.money} noun={noun} usdHead={usdHead} scroll />
            <PartTable title="Day by day" note={dayNote} rows={byDay} money={s.money} noun={noun} usdHead={usdHead} scroll />
          </div>
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  After each change
// --------------------------------------------------------------------------- //

type Segment = { from: SignalReviewEdit | null; to: SignalReviewEdit | null; list: Outcome[] };

/** The results split at each RECORDED change (an edit that saved what it changed), newest first. */
function segments(items: Outcome[], edits: SignalReviewEdit[]): Segment[] {
  const marks = edits.filter((e) => e.changes && e.changes.length > 0).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const out: Segment[] = [];
  for (let i = 0; i <= marks.length; i++) {
    const from = marks[i] ?? null;              // this segment starts at this change (null: the oldest)
    const to = i > 0 ? marks[i - 1] : null;     // and runs to the next newer one (null: now)
    const lo = from ? Date.parse(from.at) : -Infinity;
    const hi = to ? Date.parse(to.at) : Infinity;
    out.push({ from, to, list: items.filter((x) => Date.parse(x.at) > lo && Date.parse(x.at) <= hi) });
  }
  return out;
}

function Changes({ edit }: { edit: SignalReviewEdit }) {
  const [all, setAll] = useState(false);
  const lines = edit.changes ?? [];
  const shown = all ? lines : lines.slice(0, 4);
  return (
    <ul className="mt-1 space-y-0.5 text-[11px] text-neutral-400">
      {shown.map((l, i) => <li key={i} className="truncate" title={l}>· {l}</li>)}
      {lines.length > 4 && (
        <li>
          <button onClick={() => setAll((v) => !v)} className="text-neutral-400 hover:text-neutral-200">
            {all ? "show less ▴" : `+ ${lines.length - 4} more ▾`}
          </button>
        </li>
      )}
    </ul>
  );
}

function ChangesList({ items, edits, noun, days, accent }: {
  items: Outcome[]; edits: SignalReviewEdit[]; noun: string; days: number; accent: string;
}) {
  const [all, setAll] = useState(false);
  const segs = useMemo(() => segments(items, edits), [items, edits]);
  const shown = useMemo(() => segs.filter((g, i) => g.list.length > 0 || i === 0), [segs]);
  const visible = all ? shown : shown.slice(0, SHOWN_CHANGES);
  return (
    <>
      <div className="mb-1 mt-4 flex items-baseline gap-2">
        <span className={HEAD}>After each change</span>
        <span className="text-[11px] text-neutral-500">newest first - how the {noun}s did after each save of the strategy</span>
      </div>
      <div className="space-y-2">
        {visible.map((g, i) => {
          const newest = i === 0;
          const title = g.from == null
            ? (segs.length === 1 ? `All ${noun}s, last ${days} days` : `Before your change on ${when(segs[segs.length - 2]?.from?.at)}`)
            : newest ? `Since your change on ${when(g.from.at)}` : `After your change on ${when(g.from.at)}, until the next`;
          return (
            <div key={g.from?.id ?? "base"} className={`rounded-md border px-2.5 py-2 ${newest ? accent : "border-neutral-800"}`}>
              <div className="mb-1 text-xs font-semibold text-neutral-200">{title}</div>
              <BriefBySide list={g.list} noun={noun} />
              {g.from && <Changes edit={g.from} />}
            </div>
          );
        })}
      </div>
      {shown.length > SHOWN_CHANGES && (
        <button type="button" onClick={() => setAll((v) => !v)} className="mt-1.5 text-[11px] text-neutral-400 hover:text-neutral-200">
          {all ? `show the last ${SHOWN_CHANGES} only ▴` : `show all ${shown.length} changes ▾`}
        </button>
      )}
      {!edits.some((e) => e.changes && e.changes.length > 0) && (
        <div className="mt-1.5 text-[11px] text-neutral-500">
          Each time you save the strategy, what you changed is recorded and the results split there - so you see how
          the {noun}s did after it.
        </div>
      )}
    </>
  );
}

/** All / ▲ BUY / ▼ SELL and how far back - the right end of a card's title line. */
function Filters({ side, setSide, days, setDays, on }: {
  side: "all" | "long" | "short"; setSide: (v: "all" | "long" | "short") => void; days: number; setDays: (v: number) => void; on: string;
}) {
  return (
    <>
      {(["all", "long", "short"] as const).map((k) => (
        <button
          key={k}
          onClick={() => setSide(k)}
          className={`rounded px-2 py-0.5 ${side === k ? on : "text-neutral-400 hover:bg-neutral-800"}`}
        >
          {k === "all" ? "All" : k === "long" ? "▲ BUY" : "▼ SELL"}
        </button>
      ))}
      <select
        value={days}
        onChange={(e) => setDays(Number(e.target.value))}
        className="ml-1 rounded bg-neutral-800 px-1.5 py-0.5 text-xs text-neutral-300"
        title="How far back"
      >
        {[7, 30, 90].map((d) => <option key={d} value={d}>{d} days</option>)}
      </select>
    </>
  );
}

const sideText = (side: "all" | "long" | "short") => (side === "all" ? "buys and sells" : side === "long" ? "buys only" : "sells only");

// --------------------------------------------------------------------------- //
//  👁 Signal review
// --------------------------------------------------------------------------- //

const RESULT: Record<string, { text: string; cls: string }> = {
  win: { text: "✓ target", cls: "text-bull" },
  loss: { text: "✗ stop", cls: "text-bear" },
  timeout: { text: "⏱ a day on", cls: "text-neutral-300" },
  open: { text: "… open", cls: "text-neutral-500" },
  unknown: { text: "? not judged", cls: "text-neutral-600" },
};

function Row({ s }: { s: SignalReviewSignal }) {
  const [open, setOpen] = useState(false);
  const long = s.side === "long";
  const res = RESULT[s.result] ?? RESULT.unknown;
  const est = s.levels === "estimated";
  const dec = decimalsOf(s.price);
  return (
    <>
      <tr className="cursor-pointer border-t border-neutral-800/70 hover:bg-neutral-800/30" onClick={() => setOpen((v) => !v)}>
        <td className="py-1 pr-3 tabular-nums text-neutral-400">{when(s.at)}</td>
        <td className="pr-3 text-neutral-200">{s.symbol ? displaySymbol(s.symbol) : "—"}</td>
        <td className="pr-3 font-semibold text-fuchsia-300">👁 {long ? "▲ BUY" : "▼ SELL"}</td>
        <td className="pr-3 text-right tabular-nums">{s.price != null ? fmtPrice(s.price, dec) : "—"}</td>
        <td className="pr-3 text-right tabular-nums text-neutral-400" title={est ? "Estimated: logged before signals kept their levels - from the strategy's current stop and target settings and the ATR then" : "As the strategy would have placed them"}>
          {s.stop != null ? `${est ? "≈ " : ""}${fmtPrice(s.stop, dec)}` : "—"}
        </td>
        <td className="pr-3 text-right tabular-nums text-neutral-400">{s.target != null ? `${est ? "≈ " : ""}${fmtPrice(s.target, dec)}` : "—"}</td>
        <td className={`pr-3 ${res.cls}`} title={s.same_bar ? "Its stop and target were both reached inside one candle - counted as the stop" : s.why ?? undefined}>
          {res.text}{s.same_bar ? " *" : ""}
        </td>
        <td className={`pr-3 text-right tabular-nums ${s.r == null ? "text-neutral-600" : s.r >= 0 ? "text-bull" : "text-bear"}`}>{s.r != null ? rText(s.r) : "—"}</td>
        <td className="pr-3 text-right tabular-nums text-neutral-500" title="How far it went for it / against it on the way, in R">
          {s.mfe_r != null ? `+${s.mfe_r.toFixed(2)} / −${(s.mae_r ?? 0).toFixed(2)}` : "—"}
        </td>
        <td className="text-right tabular-nums text-neutral-500">{held(s.at, s.exit_at) ?? "—"}</td>
      </tr>
      {open && (
        <tr className="bg-neutral-950/50">
          <td colSpan={10} className="px-2 py-1.5 text-[11px] text-neutral-400">
            <div className="mb-0.5 text-neutral-300">
              {s.name ?? "Strategy"} · group {s.group ?? "?"} · watch only{s.exit_at ? ` · out ${when(s.exit_at)}` : ""}
              {s.exit_price != null ? ` at ${fmtPrice(s.exit_price, dec)}` : ""}
            </div>
            {s.rules.length > 0 ? (
              <ul className="space-y-0.5">
                {s.rules.map((r, i) => <li key={i}><span className="text-bull">✓</span> {r}</li>)}
              </ul>
            ) : <div>No rules logged.</div>}
          </td>
        </tr>
      )}
    </>
  );
}

export function SignalReviewCard() {
  const fold = useFold();
  const [days, setDays] = useState(30);
  const [side, setSide] = useState<"all" | "long" | "short">("all");
  // 1R in dollars for the summary's money: typed by you (kept per browser), or null = the strategy's risk now.
  const [perRSet, setPerRSet] = useLocalStorage<number | null>("ta.signalReview.perR", null);
  const { data, error } = usePolling<SignalReview>(() => fetchReview(days), POLL_MS, [days]);
  const { data: strategies } = usePolling<StrategyView[]>(() => api.strategies(), POLL_MS, []);
  const { data: risk } = usePolling<RiskState>(() => api.riskState(), POLL_MS, []);
  const signals = useMemo(() => (data?.signals ?? []).filter((x) => side === "all" || x.side === side), [data, side]);

  // The strategy most of these signals came from: its risk per trade now = its risk % of today's starting equity.
  const auto = useMemo(() => {
    const count = new Map<number, number>();
    for (const x of data?.signals ?? []) if (x.strategy_id != null) count.set(x.strategy_id, (count.get(x.strategy_id) ?? 0) + 1);
    const sid = [...count.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
    const cfg = (strategies ?? []).find((q) => q.id === sid);
    const eq = risk?.starting_equity;
    if (!cfg || !eq || !(cfg.risk_pct > 0)) return { value: null as number | null, why: "your strategy's risk now (not read yet)" };
    const v = eq * cfg.risk_pct;
    return { value: v, why: `your strategy's risk now: ${(cfg.risk_pct * 100).toFixed(1)}% of today's starting equity $${Math.round(eq).toLocaleString()} = $${Math.round(v)}` };
  }, [data, strategies, risk]);
  const perR = perRSet ?? auto.value;
  const items = useMemo(() => signals.map((x) => fromSignal(x, perR)), [signals, perR]);

  const perRControl = (
    <label
      className="ml-auto flex items-center gap-1 text-[11px] text-neutral-400"
      title={`The money is each signal's R × this. Left empty it is ${auto.why}. Type your own to see it at another size.`}
    >
      1R =
      <span className="text-neutral-500">$</span>
      <input
        type="number"
        min={1}
        step={1}
        value={perRSet ?? ""}
        placeholder={auto.value != null ? String(Math.round(auto.value)) : "—"}
        onChange={(e) => {
          const v = Number(e.target.value);
          setPerRSet(e.target.value === "" || !(v > 0) ? null : v);
        }}
        className="w-16 rounded bg-neutral-800 px-1.5 py-0.5 text-right text-xs tabular-nums text-neutral-200 placeholder:text-neutral-400"
      />
      {perRSet == null ? (
        <span className="text-neutral-500">your risk now</span>
      ) : (
        <button type="button" onClick={() => setPerRSet(null)} className="text-fuchsia-300/80 hover:text-fuchsia-200">↺ my risk now</button>
      )}
    </label>
  );

  return (
    <div className="card border-fuchsia-500/30">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="flex items-center gap-1 text-sm font-semibold">
          <FoldButton name="Signal review" />
          <span><span className="text-fuchsia-300">👁 Signal review</span> — what each watch signal would have done if traded</span>
        </span>
        <span
          className="cursor-help text-[11px] text-neutral-500 underline decoration-dotted"
          title={`Each signal a strategy group on watch logged, replayed as the trade: in at its price (the ask for a buy, the bid for a sell), out at the strategy's own stop or target - whichever price reached first in the broker's history (a sell leaves on the ask). Both inside one candle counts as the stop. Still open ${data?.horizon_h ?? 24} h later: judged where price was then (⏱). The strategy's close rules are not replayed - a real trade could have left earlier. Signals logged before 2026-11-08 have their levels estimated (≈) from the strategy's current stop and target settings.`}
        >
          how it is judged
        </span>
        <span className="ml-auto flex items-center gap-1 text-xs">
          <Filters side={side} setSide={setSide} days={days} setDays={setDays} on="bg-fuchsia-500/20 text-fuchsia-200" />
        </span>
      </div>

      {!fold?.folded && (<>
      {error && !data && <div className="text-xs text-amber-300">Can't read the signals: {error}</div>}
      {!data && !error && <div className="text-xs text-neutral-500">Replaying the signals on the broker's history…</div>}
      {data && (
        <>
          <Summary
            list={items}
            noun="signal"
            about={`last ${days} days · ${sideText(side)} · every signal counted as if it had been traded`}
            control={perRControl}
            netLabel="Net if traded"
            netTitle="Every finished signal taken at the strategy's own stop and target. Its close rules and the follow-the-target are not replayed, and the desk's limits (trades open at once, loss pause) are not applied."
            usdHead="If traded"
            dayNote="UTC days · newest first"
          />

          <ChangesList items={items} edits={data.edits ?? []} noun="signal" days={days} accent="border-fuchsia-500/40 bg-fuchsia-500/[0.05]" />

          <div className="mb-1 mt-4 flex items-baseline gap-2">
            <span className={HEAD}>Every signal</span>
            <span className="text-[11px] text-neutral-500">click a row for the rules that held</span>
          </div>
          {signals.length > 0 ? (
            <div className="max-h-[26rem] overflow-auto">
              <table className="w-full whitespace-nowrap text-xs">
                <thead className="sticky top-0 bg-neutral-900 text-left text-[11px] text-neutral-500">
                  <tr>
                    <th className="pb-1 pr-3 font-normal">When</th>
                    <th className="pr-3 font-normal">Pair</th>
                    <th className="pr-3 font-normal">Signal</th>
                    <th className="pr-3 text-right font-normal">Price</th>
                    <th className="pr-3 text-right font-normal">Stop</th>
                    <th className="pr-3 text-right font-normal">Target</th>
                    <th className="pr-3 font-normal">Result</th>
                    <th className="pr-3 text-right font-normal">R</th>
                    <th className="pr-3 text-right font-normal">Best / worst</th>
                    <th className="text-right font-normal">Held</th>
                  </tr>
                </thead>
                <tbody>
                  {signals.map((s) => <Row key={s.id} s={s} />)}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-xs text-neutral-500">No watch signals in the last {days} days.</div>
          )}
        </>
      )}
      </>)}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  ⚡ Strategy review - the live strategy's real trades
// --------------------------------------------------------------------------- //

export function StrategyReviewCard() {
  const fold = useFold();
  const [days, setDays] = useState(30);
  const [side, setSide] = useState<"all" | "long" | "short">("all");
  // Which strategy: picked by you (kept per browser), or null = the live one.
  const [pick, setPick] = useLocalStorage<number | null>("ta.strategyReview.id", null);
  const { data: trades, error } = usePolling<EquityTrade[]>(() => api.journalEquity(false), POLL_MS, []);
  const { data: strategies } = usePolling<StrategyView[]>(() => api.strategies(), POLL_MS, []);
  const { data: review } = usePolling<SignalReview>(() => fetchReview(days), POLL_MS, [days]);

  // The strategies that have closed trades, the most first.
  const withTrades = useMemo(() => {
    const count = new Map<number, number>();
    for (const t of trades ?? []) {
      const m = /^strategy:(\d+)$/.exec(t.source);
      if (m) count.set(Number(m[1]), (count.get(Number(m[1])) ?? 0) + 1);
    }
    return [...count.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id);
  }, [trades]);
  const live = (strategies ?? []).find((q) => q.live && q.enabled) ?? (strategies ?? []).find((q) => q.live);
  const sid = pick != null && withTrades.includes(pick) ? pick : live?.id ?? withTrades[0] ?? null;
  const cfg = (strategies ?? []).find((q) => q.id === sid);
  const nameOf = (id: number) => (strategies ?? []).find((q) => q.id === id)?.name ?? `Strategy ${id}`;

  const items = useMemo(() => {
    const since = Date.now() - days * 86_400_000;
    return (trades ?? [])
      .filter((t) => t.source === `strategy:${sid}` && Date.parse(t.opened_at ?? t.closed_at) >= since)
      .map(fromTrade)
      .filter((x) => side === "all" || x.side === side);
  }, [trades, sid, days, side]);
  const edits = useMemo(() => (review?.edits ?? []).filter((e) => e.strategy_id === sid), [review, sid]);

  return (
    <div className="card border-amber-500/30">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="flex items-center gap-1 text-sm font-semibold">
          <FoldButton name="Strategy review" />
          <span><span className="text-amber-300">⚡ Strategy review</span> — how the strategy's real trades did</span>
        </span>
        {cfg && (
          <span className="flex items-center gap-1.5 text-[11px] text-neutral-400">
            {cfg.name}
            {cfg.live && <span className="rounded bg-bear/20 px-1 font-semibold text-red-300">LIVE</span>}
            {!cfg.enabled && <span className="rounded bg-neutral-800 px-1 text-neutral-400">stopped</span>}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1 text-xs">
          {withTrades.length > 1 && withTrades.map((id) => (
            <button
              key={id}
              onClick={() => setPick(id)}
              className={`rounded px-2 py-0.5 ${id === sid ? "bg-amber-500/20 text-amber-200" : "text-neutral-400 hover:bg-neutral-800"}`}
              title={nameOf(id)}
            >
              {nameOf(id)}
            </button>
          ))}
          {withTrades.length > 1 && <span aria-hidden="true" className="mx-1 h-4 w-px bg-neutral-700" />}
          <Filters side={side} setSide={setSide} days={days} setDays={setDays} on="bg-amber-500/20 text-amber-200" />
        </span>
      </div>

      {!fold?.folded && (<>
      {error && !trades && <div className="text-xs text-amber-300">Can't read the trades: {error}</div>}
      {!trades && !error && <div className="text-xs text-neutral-500">Reading the trades…</div>}
      {trades && sid == null && <div className="text-xs text-neutral-500">No strategy has closed a trade yet.</div>}
      {trades && sid != null && (
        <>
          <Summary
            list={items}
            noun="trade"
            about={`last ${days} days · ${sideText(side)} · the real closed trades: the broker's $ (swap and commission in)`}
            netLabel="Net P&L"
            netTitle="The trades' real result at the broker, swap and commission in. R where the app holds the risk the trade took."
            usdHead="P&L"
            dayNote="UTC days it closed · newest first"
          />
          {review ? (
            <ChangesList items={items} edits={edits} noun="trade" days={days} accent="border-amber-500/40 bg-amber-500/[0.05]" />
          ) : (
            <div className="mt-3 text-[11px] text-neutral-500">Reading the strategy's changes…</div>
          )}
          <div className="mt-2 text-[11px] text-neutral-600">Every trade is listed in Closed trades, below.</div>
        </>
      )}
      </>)}
    </div>
  );
}
