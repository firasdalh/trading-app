import { useMemo, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { SignalReview, SignalReviewEdit, SignalReviewSignal } from "../types";

/**
 * 👁 SIGNAL REVIEW (asked 2026-11-08: "keep track in a separate place in the journal ... the result of those
 * signals, to learn from it ... so I can enhance it in the strategy ... after I change I need a brief result of
 * those signals"). Every signal a strategy group on watch logged, judged as the trade it would have been - in at
 * its price, out at the strategy's own stop or target, whichever came first (backend signal_review.py) - and
 * split at each recorded change to the strategy, newest first, so a change is judged by the signals after it.
 * In the signals' fuchsia, like their pop-up and their 👁 markers on the chart.
 */

const POLL_MS = 60_000;

type Stat = { n: number; wins: number; losses: number; timeouts: number; open: number; r: number; done: number };

function stat(list: SignalReviewSignal[]): Stat {
  const s: Stat = { n: list.length, wins: 0, losses: 0, timeouts: 0, open: 0, r: 0, done: 0 };
  for (const x of list) {
    if (x.result === "win") s.wins++;
    else if (x.result === "loss") s.losses++;
    else if (x.result === "timeout") s.timeouts++;
    else if (x.result === "open") s.open++;
    if ((x.result === "win" || x.result === "loss" || x.result === "timeout") && x.r != null) {
      s.r += x.r;
      s.done++;
    }
  }
  return s;
}

const rText = (r: number) => `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(2)}R`;
const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
// A signal's prices in the decimals its own price is quoted in - an estimated level is a raw computed number.
const decimalsOf = (v: number | null | undefined): number | undefined => {
  if (v == null || !Number.isFinite(v)) return undefined;
  const t = String(v);
  const i = t.indexOf(".");
  return Math.min(5, Math.max(2, i < 0 ? 0 : t.length - i - 1));
};
const held = (from: string, to: string | null | undefined) => {
  if (!to) return null;
  const m = Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 60_000));
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};

/** "6 signals · 2 ✓ 4 ✗ · 33% · −2.40R" - the brief result of a set of signals. */
function Brief({ list, big = false }: { list: SignalReviewSignal[]; big?: boolean }) {
  const s = stat(list);
  if (!s.n) return <span className="text-neutral-500">no signals</span>;
  const wr = s.wins + s.losses ? Math.round((s.wins / (s.wins + s.losses)) * 100) : null;
  return (
    <span className={`flex flex-wrap items-baseline gap-x-2 tabular-nums ${big ? "text-sm" : "text-xs"}`}>
      <span className="text-neutral-300">{s.n} signal{s.n === 1 ? "" : "s"}</span>
      <span className="text-bull">{s.wins} ✓</span>
      <span className="text-bear">{s.losses} ✗</span>
      {s.timeouts > 0 && <span className="text-neutral-400" title="Still open a day later: judged where price was then">{s.timeouts} ⏱</span>}
      {s.open > 0 && <span className="text-neutral-500" title="Neither its stop nor its target yet">{s.open} open</span>}
      {wr != null && <span className="text-neutral-400">{wr}% won</span>}
      {s.done > 0 && <b className={s.r >= 0 ? "text-bull" : "text-bear"}>{rText(s.r)}</b>}
    </span>
  );
}

/** The brief result, and the same for each side - a BUY side and a SELL side can be very different signals. */
function BriefBySide({ list }: { list: SignalReviewSignal[] }) {
  const buys = list.filter((x) => x.side === "long");
  const sells = list.filter((x) => x.side === "short");
  return (
    <div className="space-y-0.5">
      <Brief list={list} big />
      <div className="flex flex-wrap gap-x-5 gap-y-0.5 pl-0.5">
        <span className="flex items-baseline gap-1.5"><span className="text-[11px] font-semibold text-bull">▲ BUY</span><Brief list={buys} /></span>
        <span className="flex items-baseline gap-1.5"><span className="text-[11px] font-semibold text-bear">▼ SELL</span><Brief list={sells} /></span>
      </div>
    </div>
  );
}

type Segment = { from: SignalReviewEdit | null; to: SignalReviewEdit | null; list: SignalReviewSignal[] };

/** The signals split at each RECORDED change (an edit that saved what it changed), newest first. */
function segments(signals: SignalReviewSignal[], edits: SignalReviewEdit[]): Segment[] {
  const marks = edits.filter((e) => e.changes && e.changes.length > 0).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const out: Segment[] = [];
  for (let i = 0; i <= marks.length; i++) {
    const from = marks[i] ?? null;              // this segment starts at this change (null: the oldest)
    const to = i > 0 ? marks[i - 1] : null;     // and runs to the next newer one (null: now)
    const lo = from ? Date.parse(from.at) : -Infinity;
    const hi = to ? Date.parse(to.at) : Infinity;
    out.push({ from, to, list: signals.filter((x) => Date.parse(x.at) > lo && Date.parse(x.at) <= hi) });
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
          <button onClick={() => setAll((v) => !v)} className="text-fuchsia-300/80 hover:text-fuchsia-200">
            {all ? "show less ▴" : `+ ${lines.length - 4} more ▾`}
          </button>
        </li>
      )}
    </ul>
  );
}

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
  const [days, setDays] = useState(30);
  const [side, setSide] = useState<"all" | "long" | "short">("all");
  const { data, error } = usePolling<SignalReview>(() => api.signalReview(days), POLL_MS, [days]);
  const signals = useMemo(() => (data?.signals ?? []).filter((x) => side === "all" || x.side === side), [data, side]);
  const segs = useMemo(() => segments(signals, data?.edits ?? []), [signals, data]);
  const unrecorded = (data?.edits ?? []).filter((e) => e.changes == null).length;

  return (
    <div className="card border-fuchsia-500/30">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-sm font-semibold"><span className="text-fuchsia-300">👁 Signal review</span> — what each watch signal would have done if traded</span>
        <span
          className="cursor-help text-[11px] text-neutral-500 underline decoration-dotted"
          title={`Each signal a strategy group on watch logged, replayed as the trade: in at its price (the ask for a buy, the bid for a sell), out at the strategy's own stop or target - whichever price reached first in the broker's history (a sell leaves on the ask). Both inside one candle counts as the stop. Still open ${data?.horizon_h ?? 24} h later: judged where price was then (⏱). The strategy's close rules are not replayed - a real trade could have left earlier. Signals logged before 2026-11-08 have their levels estimated (≈) from the strategy's current stop and target settings.`}
        >
          how it is judged
        </span>
        <span className="ml-auto flex items-center gap-1 text-xs">
          {(["all", "long", "short"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setSide(k)}
              className={`rounded px-2 py-0.5 ${side === k ? "bg-fuchsia-500/20 text-fuchsia-200" : "text-neutral-400 hover:bg-neutral-800"}`}
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
        </span>
      </div>

      {error && !data && <div className="text-xs text-amber-300">Can't read the signals: {error}</div>}
      {!data && !error && <div className="text-xs text-neutral-500">Replaying the signals on the broker's history…</div>}
      {data && (
        <>
          <div className="space-y-2">
            {segs.map((g, i) => {
              if (!g.list.length && i > 0) return null;
              const newest = i === 0;
              const title = g.from == null
                ? (segs.length === 1 ? `All signals, last ${days} days` : `Before your change on ${when(segs[segs.length - 2]?.from?.at)}`)
                : newest ? `Since your change on ${when(g.from.at)}` : `After your change on ${when(g.from.at)}, until the next`;
              return (
                <div key={g.from?.id ?? "base"} className={`rounded-md border px-2.5 py-2 ${newest ? "border-fuchsia-500/40 bg-fuchsia-500/[0.05]" : "border-neutral-800"}`}>
                  <div className="mb-1 text-xs font-semibold text-neutral-200">{title}</div>
                  <BriefBySide list={g.list} />
                  {g.from && <Changes edit={g.from} />}
                </div>
              );
            })}
          </div>
          {!(data.edits ?? []).some((e) => e.changes && e.changes.length > 0) && (
            <div className="mt-1.5 text-[11px] text-neutral-500">
              From now on each time you save the strategy, what you changed is recorded and the results split there - so you
              see how the signals did after it. {unrecorded > 0 && `(${unrecorded} earlier saves recorded no details.)`}
            </div>
          )}

          {signals.length > 0 ? (
            <div className="mt-3 max-h-[26rem] overflow-auto">
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
            <div className="mt-2 text-xs text-neutral-500">No watch signals in the last {days} days.</div>
          )}
        </>
      )}
    </div>
  );
}
