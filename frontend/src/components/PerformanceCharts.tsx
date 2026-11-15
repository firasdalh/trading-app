import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import type { JournalStats } from "../types";
import { FoldButton, useFold } from "./JournalFold";
import { srcColor, srcLabel } from "./sourceMeta";

/**
 * The Journal's Performance card, drawn (asked 2026-11-11: "enhance the performance visualization"). It was six
 * numbers in R over the app's own trades; now, on your broker's history - every closed trade on this account, the
 * phone's too, in $ as the broker booked them:
 *  - the headline numbers (net, win rate, profit factor, average win vs loss, drawdown, best / worst day, R);
 *  - the equity curve - your P&L added up trade by trade - with its drawdown from the peak underneath; with All
 *    picked it also draws who made it (Strategy 1 and Manual on their own lines);
 *  - P&L by day, on the broker's day (the header's DAY P&L counts the same day);
 *  - each trade's result, binned: how big the wins are next to the losses.
 * One row of choices above it all scopes everything: whose trades, and over how long.
 * Data: GET /api/journal/equity (every trade, oldest first). Until the backend has it: the newest 500 of
 * /api/journal/trades (no R).
 */

// Line colours: a validated set for this dark surface (OKLCH L 0.48-0.67, CVD dE >= 8.4, contrast >= 3:1). Colour
// follows WHO, never rank: All is blue, a strategy orange, Manual aqua, anything else yellow, then violet.
const COL = {
  all: "#3987e5", strategy: "#d95926", manual: "#199e70", other: "#c98500", other2: "#9085e9",
  context: "#6b6b66", grid: "#232323", zero: "#4a4a4a", bull: "#22c55e", bear: "#ef4444", ring: "#121212",
};
function colorFor(who: string, taken: string[] = []): string {
  const c = who === "all" ? COL.all : who.startsWith("strategy:") ? COL.strategy : who === "manual" ? COL.manual : COL.other;
  return taken.includes(c) && c === COL.other ? COL.other2 : c;
}

const DAY = 86_400_000;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const asUtc = (s: string) => (/[Zz]|[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`);
const dayStart = (ms: number) => Math.floor(ms / DAY) * DAY;
const dayKey = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const utcDayText = (ms: number) => {
  const d = new Date(ms);
  return `${WD[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]}`;
};
const localText = (ms: number) =>
  new Date(ms).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
// 00:00 UTC on this computer's clock ("04:00" in Dubai): where the broker's day starts.
const utcMidnightHere = () =>
  new Date(Date.UTC(2026, 0, 1)).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

// "+$1,234" / "−$56": whole dollars for labels; "+$1.2k" on an axis.
const usd0 = (v: number) => `${v < 0 ? "−" : v > 0 ? "+" : ""}$${Math.round(Math.abs(v)).toLocaleString()}`;
const axUsd = (v: number) => {
  const a = Math.abs(v);
  const body = a >= 1000 ? `${(a / 1000).toFixed(a % 1000 === 0 ? 0 : 1)}k` : a >= 10 || a === 0 ? a.toFixed(0) : a.toFixed(1);
  return `${v < 0 ? "−" : v > 0 ? "+" : ""}$${body}`;
};
const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "text-neutral-200" : v > 0 ? "text-bull" : "text-bear");

function niceStep(span: number, target: number): number {
  const raw = Math.max(span, 1e-9) / Math.max(1, target);
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}
function niceTicks(lo: number, hi: number, target: number, minStep = 0): number[] {
  const top = hi - lo < 1e-9 ? lo + 1 : hi;
  const s = Math.max(minStep, niceStep(top - lo, target));
  const a = Math.floor(lo / s) * s;
  const b = Math.ceil(top / s) * s;
  const out: number[] = [];
  for (let k = 0, v = a; v <= b + s * 1e-6 && k < 50; k++, v = a + k * s) out.push(Math.round(v / s) * s);
  return out;
}

// A bar from the baseline y0 to its data end y1: 4px rounded at the data end, square on the baseline.
function barPath(x: number, w: number, y0: number, y1: number): string {
  const h = Math.abs(y1 - y0);
  if (h < 0.5 || w <= 0) return "";
  const r = Math.min(4, w / 2, h);
  const s = y1 < y0 ? 1 : -1;     // up: the round corners are below the data end; down: above it
  return `M${x},${y0}V${y1 + s * r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 + s * r}V${y0}Z`;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

// ------------------------------------------------------------------ the data
interface Pt { t: number; pnl: number; src: string; sym: string; dir: string; r: number | null }

async function loadTrades(): Promise<{ pts: Pt[]; capped: boolean }> {
  try {
    const rows = await api.journalEquity();
    const pts = rows.map((r) => ({ t: Date.parse(asUtc(r.closed_at)), pnl: r.pnl, src: r.source || "unknown",
                                   sym: r.symbol, dir: r.direction, r: r.r ?? null }));
    return { pts: pts.filter((p) => Number.isFinite(p.t)).sort((a, b) => a.t - b.t), capped: false };
  } catch (e) {
    if (!(e instanceof Error) || !e.message.startsWith("404")) throw e;
    // A backend without /equity yet: the trade log's newest 500 (no R there).
    const rows = await api.journalTrades(500);
    const pts: Pt[] = [];
    for (const r of rows) {
      if (!r.closed_at || r.realized_pnl == null) continue;
      pts.push({ t: Date.parse(asUtc(r.closed_at)), pnl: r.realized_pnl, src: r.source ?? "unknown", sym: r.symbol,
                 dir: r.direction, r: null });
    }
    return { pts: pts.filter((p) => Number.isFinite(p.t)).sort((a, b) => a.t - b.t), capped: rows.length >= 500 };
  }
}

// t / v: the running sum, from 0 at t0 - one step per closed trade; pts[i - 1] is the trade of step i.
interface Curve { key: string; name: string; color: string; t: number[]; v: number[]; pts: Pt[] }
function curveOf(key: string, name: string, color: string, pts: Pt[], t0: number): Curve {
  const t = [t0];
  const v = [0];
  let c = 0;
  for (const p of pts) {
    c += p.pnl;
    t.push(p.t);
    v.push(c);
  }
  return { key, name, color, t, v, pts };
}
// The last point at or before x.
function idxAt(t: number[], x: number): number {
  let lo = 0;
  let hi = t.length - 1;
  if (x <= t[0]) return 0;
  while (lo < hi) {
    const m = (lo + hi + 1) >> 1;
    if (t[m] <= x) lo = m;
    else hi = m - 1;
  }
  return lo;
}

interface Day { t: number; net: number; n: number; wins: number; bySrc: Map<string, { net: number; n: number }> }
function daysOf(pts: Pt[], t0: number, t1: number): Day[] {
  const out: Day[] = [];
  for (let d = dayStart(t0); d <= dayStart(t1); d += DAY) out.push({ t: d, net: 0, n: 0, wins: 0, bySrc: new Map() });
  const at = new Map(out.map((d, i) => [dayKey(d.t), i]));
  for (const p of pts) {
    const i = at.get(dayKey(p.t));
    if (i == null) continue;
    const d = out[i];
    d.net += p.pnl;
    d.n += 1;
    if (p.pnl > 0) d.wins += 1;
    const s = d.bySrc.get(p.src) ?? { net: 0, n: 0 };
    s.net += p.pnl;
    s.n += 1;
    d.bySrc.set(p.src, s);
  }
  return out;
}

function statsOf(pts: Pt[]) {
  let wins = 0, losses = 0, gw = 0, gl = 0, net = 0;
  for (const p of pts) {
    net += p.pnl;
    if (p.pnl > 0) { wins += 1; gw += p.pnl; }
    else if (p.pnl < 0) { losses += 1; gl -= p.pnl; }
  }
  const rs = pts.filter((p) => p.r != null).map((p) => p.r as number);
  const n = pts.length;
  return {
    n, wins, losses, net,
    winRate: n ? wins / n : null,
    pf: gl > 0 ? gw / gl : null,
    avgWin: wins ? gw / wins : null,
    avgLoss: losses ? -gl / losses : null,
    nR: rs.length,
    expR: rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : null,
  };
}

// ------------------------------------------------------------------ the card
const RANGES = [
  { id: "7d", label: "7 days", days: 7 },
  { id: "30d", label: "30 days", days: 30 },
  { id: "all", label: "All time", days: 0 },
] as const;
const MAX_DAYS = 120;

export function PerformanceCard({ perf, bump = 0 }: { perf: JournalStats | null; bump?: number }) {
  const fold = useFold();
  const { data } = usePolling(loadTrades, 30_000, [bump]);
  const [who, setWho] = useLocalStorage<string>("ta.journal.perfWho", "all");
  const [range, setRange] = useLocalStorage<string>("ta.journal.perfRange", "all");
  const now = Date.now();
  const all = data?.pts ?? [];
  const rng = RANGES.find((r) => r.id === range) ?? RANGES[2];
  const t0 = rng.days ? dayStart(now) - (rng.days - 1) * DAY : all.length ? dayStart(all[0].t) : dayStart(now);
  const inRange = useMemo(() => all.filter((p) => p.t >= t0), [all, t0]);
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const p of inRange) m.set(p.src, (m.get(p.src) ?? 0) + 1);
    return m;
  }, [inRange]);
  const pick = who === "all" || counts.has(who) || all.some((p) => p.src === who) ? who : "all";
  const mine = useMemo(() => (pick === "all" ? inRange : inRange.filter((p) => p.src === pick)), [inRange, pick]);
  const st = statsOf(mine);

  const view = useMemo(() => {
    const main = curveOf(pick, pick === "all" ? "All trades" : srcLabel(pick), colorFor(pick), mine, t0);
    const others: Curve[] = [];
    if (pick === "all") {
      // Who made it: the biggest sources on their own lines (2 at most, each 10%+ of the trades).
      const big = [...counts.entries()].filter(([, n]) => n >= Math.max(5, 0.1 * inRange.length))
        .sort((a, b) => b[1] - a[1]).slice(0, 2);
      const taken: string[] = [COL.all];
      for (const [src] of big) {
        const c = colorFor(src, taken);
        taken.push(c);
        others.push(curveOf(src, srcLabel(src), c, inRange.filter((p) => p.src === src), t0));
      }
    }
    const context = pick === "all" ? null : curveOf("account", "Whole account", COL.context, inRange, t0);
    let peak = 0;
    const dd = main.v.map((v) => { peak = Math.max(peak, v); return v - peak; });
    const nDays = (dayStart(now) - dayStart(t0)) / DAY + 1;
    const dStart = nDays > MAX_DAYS ? dayStart(now) - (MAX_DAYS - 1) * DAY : t0;
    const days = daysOf(mine, dStart, now);
    return { main, others, context, dd, days, cut: dStart > t0 };
  }, [pick, mine, inRange, counts, t0, now]);

  const traded = view.days.filter((d) => d.n > 0);
  const best = traded.reduce<Day | null>((b, d) => (b == null || d.net > b.net ? d : b), null);
  const worst = traded.reduce<Day | null>((b, d) => (b == null || d.net < b.net ? d : b), null);
  const green = traded.filter((d) => d.net > 0).length;
  const maxDd = Math.min(0, ...view.dd);
  const nowDd = view.dd[view.dd.length - 1] ?? 0;
  // R needs the risk the app recorded: the /equity series carries it. Without it (an older backend) the old card's
  // own figure stands in, for the whole account over all time only - the only slice it was ever over.
  const noR = !all.some((p) => p.r != null);
  const expR = !noR ? st.expR : pick === "all" && rng.id === "all" ? perf?.expectancy_r ?? null : null;
  const nR = !noR ? st.nR : pick === "all" && rng.id === "all" ? perf?.trades ?? 0 : 0;

  const chips: [string, number][] = [
    ["all", inRange.length],
    ...[...counts.entries()].sort((a, b) => b[1] - a[1]),
  ];
  if (pick !== "all" && !counts.has(pick)) chips.push([pick, 0]);

  if (!data) return null;
  return (
    <div className="card space-y-3">
      <div>
        <div className="flex items-center gap-1 text-sm font-semibold">
          <FoldButton name="Performance" />
          Performance
          {fold?.folded && st.n > 0 && (
            <span className="ml-1 text-xs font-normal text-neutral-500">
              · {rng.label.toLowerCase()} · {pick === "all" ? "all trades" : srcLabel(pick)} · {st.n} trades ·{" "}
              <span className={tone(st.net)}>{fmtUsd(st.net, { sign: true })}</span>
            </span>
          )}
        </div>
        {!fold?.folded && (
          <p className="text-xs text-neutral-500">
            Your broker's history: every closed trade on this account, your phone's included, in $ as the broker booked it
            (swap and commission in). Days are the broker's - from 00:00 UTC ({utcMidnightHere()} your time), the day the
            header's DAY P&amp;L counts. Every number here is also in Breakdown and the trade log below.
            {data.capped && " (The newest 500 trades - the backend update that sends them all is on its way.)"}
          </p>
        )}
      </div>

      {!fold?.folded && (<>
        {/* One row of choices; it scopes every number and chart below. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
          <div className="flex flex-wrap gap-1" role="group" aria-label="Whose trades">
            {chips.map(([k, n]) => (
              <button
                key={k}
                type="button"
                aria-pressed={pick === k}
                onClick={() => setWho(k)}
                className={`flex items-center gap-1.5 rounded px-2 py-0.5 text-xs ${pick === k ? "bg-neutral-700 text-white" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
                title={k === "all" ? "Every closed trade on the account" : `Only the trades ${srcLabel(k)} opened`}
              >
                <span className="h-0.5 w-3 rounded" style={{ background: colorFor(k) }} />
                <span className={k === "all" || pick === k ? "" : srcColor(k)}>{k === "all" ? "All" : srcLabel(k)}</span>
                <span className="tabular-nums text-neutral-500">{n}</span>
              </button>
            ))}
          </div>
          <div className="flex gap-1" role="group" aria-label="Over how long">
            {RANGES.map((r) => (
              <button
                key={r.id}
                type="button"
                aria-pressed={rng.id === r.id}
                onClick={() => setRange(r.id)}
                className={`rounded px-2 py-0.5 text-xs ${rng.id === r.id ? "bg-neutral-700 text-white" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {st.n === 0 ? (
          <div className="text-sm text-neutral-500">
            No closed trade {pick === "all" ? "" : `by ${srcLabel(pick)} `}in the last {rng.days || "-"} days.
          </div>
        ) : (<>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
            <Kpi label="Net P&L" value={fmtUsd(st.net, { sign: true })} valueClass={tone(st.net)}
                 sub={`${st.n} trade${st.n === 1 ? "" : "s"} over ${traded.length} day${traded.length === 1 ? "" : "s"}`} />
            <Kpi label="Win rate" value={pct(st.winRate)} sub={`${st.wins} won · ${st.losses} lost`} />
            <Kpi label="Profit factor" value={st.pf == null ? (st.wins ? "∞" : "—") : st.pf.toFixed(2)}
                 valueClass={st.pf == null ? "" : st.pf >= 1 ? "text-bull" : "text-bear"}
                 sub="$ won ÷ $ lost" title="Every dollar the winners made, divided by every dollar the losers lost. Above 1 = it makes money." />
            <Kpi label="Average win / loss"
                 value={<><span className="text-bull">{st.avgWin == null ? "—" : usd0(st.avgWin)}</span>
                   <span className="text-neutral-500"> / </span>
                   <span className="text-bear">{st.avgLoss == null ? "—" : usd0(st.avgLoss)}</span></>}
                 sub={st.avgWin != null && st.avgLoss != null ? `a win is ${(st.avgWin / -st.avgLoss).toFixed(2)}× a loss` : undefined}
                 title="What a winning trade makes on average, next to what a losing one loses. With a high win rate and a small ratio, a few losses take back many wins." />
            <Kpi label="Max drawdown" value={maxDd < 0 ? usd0(maxDd) : "$0"} valueClass={maxDd < 0 ? "text-bear" : ""}
                 sub={nowDd < 0 ? `${usd0(nowDd)} below its peak now` : "at its peak now"}
                 title="The deepest fall of the equity curve from a high, over the time picked." />
            <Kpi label="Best / worst day"
                 value={<><span className={tone(best?.net)}>{best ? usd0(best.net) : "—"}</span>
                   <span className="text-neutral-500"> / </span>
                   <span className={tone(worst?.net)}>{worst ? usd0(worst.net) : "—"}</span></>}
                 sub={`${green} of ${traded.length} day${traded.length === 1 ? "" : "s"} green`}
                 title={best && worst ? `Best: ${utcDayText(best.t)} · worst: ${utcDayText(worst.t)} (the broker's day)` : undefined} />
            <Kpi label="Expectancy" value={expR == null ? "—" : `${expR >= 0 ? "+" : ""}${expR.toFixed(2)}R`}
                 valueClass={expR == null ? "" : expR >= 0 ? "text-bull" : "text-bear"}
                 sub={nR ? `per trade · ${nR} with a recorded risk` : noR ? "comes with the backend update" : "no trade here has a recorded risk"}
                 title="R = profit ÷ the risk taken; the average R per trade is the edge. Only for the trades the app placed - it records their risk; a phone trade has none." />
          </div>

          <div className="grid gap-3 lg:grid-cols-3">
            <Panel
              className="lg:col-span-2"
              title="Equity curve"
              note="your P&L added up, trade by trade - and how far it fell from its high"
              legend={[view.main, ...view.others, ...(view.context ? [view.context] : [])].length > 1
                ? [view.main, ...view.others, ...(view.context ? [view.context] : [])].map((c) => ({ c, last: c.v[c.v.length - 1] }))
                : null}
            >
              <EquityPanel main={view.main} others={view.others} context={view.context} dd={view.dd} t0={t0} t1={now} />
            </Panel>
            <Panel title="Each trade's result" note="how many trades ended where - wins vs losses">
              <HistPanel pts={mine} avgWin={st.avgWin} avgLoss={st.avgLoss} />
            </Panel>
          </div>

          <Panel title="P&L by day" note={`the broker's day${view.cut ? ` - the last ${MAX_DAYS}` : ""}; hover a day for its trades`}>
            <DailyPanel days={view.days} showSrc={pick === "all"} />
          </Panel>
        </>)}
      </>)}
    </div>
  );
}

function Kpi({ label, value, sub, valueClass, title }: {
  label: string; value: ReactNode; sub?: string; valueClass?: string; title?: string;
}) {
  return (
    <div className={`rounded-lg bg-neutral-800/50 px-3 py-2 ${title ? "cursor-help" : ""}`} title={title}>
      <div className="text-[11px] text-neutral-400">{label}</div>
      <div className={`whitespace-nowrap text-lg font-semibold ${valueClass ?? "text-neutral-100"}`}>{value}</div>
      {sub && <div className="truncate text-[11px] text-neutral-500">{sub}</div>}
    </div>
  );
}

function Panel({ title, note, legend, className, children }: {
  title: string; note?: string; className?: string; children: ReactNode;
  legend?: { c: Curve; last: number }[] | null;
}) {
  return (
    <section className={`min-w-0 rounded-lg border border-neutral-800 bg-neutral-950/40 p-3 ${className ?? ""}`}>
      <div className="mb-1 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-xs font-semibold text-neutral-200">{title}</span>
        {note && <span className="text-[11px] text-neutral-500">{note}</span>}
        {legend && (
          <span className="ml-auto flex flex-wrap gap-x-3 gap-y-0.5 text-[11px]">
            {legend.map(({ c, last }) => (
              <span key={c.key} className="flex items-center gap-1.5 text-neutral-400">
                <span className="h-0.5 w-3 rounded" style={{ background: c.color }} />
                {c.name}
                <span className="font-semibold text-neutral-200">{usd0(last)}</span>
              </span>
            ))}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function Tip({ x, w, children }: { x: number; w: number; children: ReactNode }) {
  const TW = 232;
  const left = x + 14 + TW > w ? Math.max(0, x - 14 - TW) : x + 14;
  return (
    <div
      className="pointer-events-none absolute top-1 z-20 rounded-md border border-neutral-700 bg-neutral-950/95 px-2.5 py-2 text-xs text-neutral-300 shadow-pop"
      style={{ left, width: TW }}
    >
      {children}
    </div>
  );
}
const Key = ({ color }: { color: string }) => <span className="inline-block h-0.5 w-3 shrink-0 rounded" style={{ background: color }} />;

// ------------------------------------------------------------------ equity curve + drawdown
function EquityPanel({ main, others, context, dd, t0, t1 }: {
  main: Curve; others: Curve[]; context: Curve | null; dd: number[]; t0: number; t1: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const lines = [...(context ? [context] : []), ...others, main];
  const W = Math.max(320, width);
  const showEnds = lines.length > 1;
  const M = { l: 52, r: showEnds ? 92 : 14, t: 8 };
  const PH = 196, GAP = 24, DH = 58, AX = 24;
  const H = M.t + PH + GAP + DH + AX;
  const PW = W - M.l - M.r;
  const vals = lines.flatMap((c) => c.v);
  const yt = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals), 4);
  const [ylo, yhi] = [yt[0], yt[yt.length - 1]];
  const span = Math.max(1, t1 - t0);
  const x = (t: number) => M.l + ((t - t0) / span) * PW;
  const y = (v: number) => M.t + (1 - (v - ylo) / Math.max(1e-9, yhi - ylo)) * PH;
  const ddLo = Math.min(0, ...dd);
  const dt = ddLo < 0 ? niceTicks(ddLo, 0, 2) : [0];
  const ddBottom = dt[0];
  const D0 = M.t + PH + GAP;
  const y2 = (v: number) => D0 + (ddBottom < 0 ? (v / ddBottom) * DH : 0);
  const path = (c: Curve) => `${c.t.map((t, i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(c.v[i]).toFixed(1)}`).join("")}L${x(t1).toFixed(1)},${y(c.v[c.v.length - 1]).toFixed(1)}`;
  const ddPath = `${main.t.map((t, i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y2(dd[i]).toFixed(1)}`).join("")}L${x(t1).toFixed(1)},${y2(dd[dd.length - 1]).toFixed(1)}`;
  const ddArea = `${ddPath}L${x(t1).toFixed(1)},${D0}L${x(t0).toFixed(1)},${D0}Z`;
  const washArea = `${path(main)}L${x(t1).toFixed(1)},${y(0).toFixed(1)}L${x(t0).toFixed(1)},${y(0).toFixed(1)}Z`;
  const troughI = dd.indexOf(ddLo);

  // Day ticks on the broker's day, far enough apart for their labels.
  const nDays = span / DAY;
  const step = [1, 2, 3, 7, 14, 30, 61].find((s) => (PW / nDays) * s >= 58) ?? 91;
  const xTicks: number[] = [];
  for (let d = dayStart(t0) + (dayStart(t0) < t0 ? DAY : 0); d <= t1; d += step * DAY) xTicks.push(d);

  // Direct labels at the right end, unless two would collide (the legend carries the names then).
  const ends = showEnds ? lines.filter((c) => c !== context).map((c) => ({ c, yy: y(c.v[c.v.length - 1]) })) : [];
  const sorted = [...ends].sort((a, b) => a.yy - b.yy);
  const collide = sorted.some((e, i) => i > 0 && e.yy - sorted[i - 1].yy < 13);

  const move = (e: PointerEvent<SVGSVGElement>) => {
    const r = svgRef.current?.getBoundingClientRect();
    if (!r) return;
    const tm = t0 + ((e.clientX - r.left - M.l) / PW) * span;
    let i = idxAt(main.t, tm);
    if (i < main.t.length - 1 && Math.abs(main.t[i + 1] - tm) < Math.abs(main.t[i] - tm)) i += 1;
    setHover(i);
  };
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    const last = main.t.length - 1;
    if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? last) - 1));
    else if (e.key === "ArrowRight") setHover((h) => Math.min(last, (h ?? 0) + 1));
    else if (e.key === "Home") setHover(0);
    else if (e.key === "End") setHover(last);
    else return;
    e.preventDefault();
  };
  const hi = hover != null && hover < main.t.length ? hover : null;
  const ht = hi != null ? main.t[hi] : 0;
  const lastV = main.v[main.v.length - 1];

  return (
    <div
      ref={ref}
      className="relative outline-none focus-visible:ring-1 focus-visible:ring-neutral-500"
      tabIndex={0}
      onKeyDown={key}
      onBlur={() => setHover(null)}
      aria-label={`Equity curve, ${main.name}: ${usd0(lastV)} over ${main.t.length - 1} trades; deepest drawdown ${usd0(ddLo)}. Arrow keys step through the trades.`}
    >
      {width > 0 && (
        <svg ref={svgRef} width={W} height={H} className="block select-none" role="img"
             aria-label={`Equity curve: ${usd0(lastV)}`} onPointerMove={move} onPointerLeave={() => setHover(null)}>
          {yt.map((v) => (
            <g key={`y${v}`}>
              <line x1={M.l} x2={W - M.r} y1={y(v)} y2={y(v)} stroke={v === 0 ? COL.zero : COL.grid} strokeWidth={1} />
              <text x={M.l - 6} y={y(v) + 3.5} textAnchor="end" className="fill-neutral-500 text-[10px] tabular-nums">{axUsd(v)}</text>
            </g>
          ))}
          {xTicks.map((d) => (
            <text key={`x${d}`} x={x(d)} y={H - 6} textAnchor="middle" className="fill-neutral-500 text-[10px]">
              {new Date(d).getUTCDate()} {MON[new Date(d).getUTCMonth()]}
            </text>
          ))}
          {others.length === 0 && <path d={washArea} fill={main.color} opacity={0.1} />}
          {lines.map((c) => (
            <path key={c.key} d={path(c)} fill="none" stroke={c.color} strokeWidth={c === context ? 1.5 : 2}
                  strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {showEnds && !collide && ends.map(({ c, yy }) => (
            <text key={`e${c.key}`} x={W - M.r + 6} y={yy + 3.5} className="fill-neutral-300 text-[10px] font-semibold">
              {usd0(c.v[c.v.length - 1])}
              <tspan className="fill-neutral-500 font-normal"> {c.name.length > 10 ? `${c.name.slice(0, 9)}…` : c.name}</tspan>
            </text>
          ))}

          {/* the drawdown from the high, underneath */}
          <text x={M.l} y={D0 - 8} className="fill-neutral-500 text-[10px]">Drawdown from its high</text>
          <line x1={M.l} x2={W - M.r} y1={D0} y2={D0} stroke={COL.zero} strokeWidth={1} />
          {ddBottom < 0 && <line x1={M.l} x2={W - M.r} y1={y2(ddBottom)} y2={y2(ddBottom)} stroke={COL.grid} strokeWidth={1} />}
          {ddBottom < 0 && (
            <text x={M.l - 6} y={y2(ddBottom) + 3.5} textAnchor="end" className="fill-neutral-500 text-[10px] tabular-nums">{axUsd(ddBottom)}</text>
          )}
          <path d={ddArea} fill={COL.bear} opacity={0.16} />
          <path d={ddPath} fill="none" stroke={COL.bear} strokeWidth={1.5} strokeLinejoin="round" />
          {ddLo < 0 && troughI >= 0 && (
            <text x={Math.min(x(main.t[troughI]) + 6, W - M.r - 70)} y={Math.min(y2(ddLo) + 12, H - AX)} className="fill-neutral-300 text-[10px]">
              deepest {usd0(ddLo)}
            </text>
          )}

          {hi != null && (
            <g pointerEvents="none">
              <line x1={x(ht)} x2={x(ht)} y1={M.t} y2={D0 + DH} stroke="#737373" strokeWidth={1} />
              {lines.map((c) => {
                const v = c.v[idxAt(c.t, ht)];
                return <circle key={`h${c.key}`} cx={x(ht)} cy={y(v)} r={4} fill={c.color} stroke={COL.ring} strokeWidth={2} />;
              })}
              <circle cx={x(ht)} cy={y2(dd[hi])} r={3.5} fill={COL.bear} stroke={COL.ring} strokeWidth={2} />
            </g>
          )}
        </svg>
      )}
      {width === 0 && <div style={{ height: H }} />}
      {hi != null && (
        <Tip x={x(ht)} w={W}>
          <div className="mb-1 text-[11px] text-neutral-500">{hi === 0 ? `Start - ${utcDayText(t0)}` : localText(ht)}</div>
          {hi > 0 && <EquityTipTrade main={main} hi={hi} />}
          <div className="mt-1 space-y-0.5">
            {[main, ...others, ...(context ? [context] : [])].map((c) => (
              <div key={`t${c.key}`} className="flex items-center gap-1.5">
                <Key color={c.color} />
                <span className="font-semibold text-neutral-100">{usd0(c.v[idxAt(c.t, ht)])}</span>
                <span className="truncate text-neutral-500">{c.name}</span>
              </div>
            ))}
            <div className="flex items-center gap-1.5">
              <Key color={COL.bear} />
              <span className="font-semibold text-neutral-100">{dd[hi] < 0 ? usd0(dd[hi]) : "$0"}</span>
              <span className="text-neutral-500">below its high</span>
            </div>
          </div>
        </Tip>
      )}
    </div>
  );
}

// The trade a point of the curve is: which pair, who opened it, and what it made.
function EquityTipTrade({ main, hi }: { main: Curve; hi: number }) {
  const p = main.pts[hi - 1];
  if (!p) return null;
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-neutral-800 pb-1">
      <span className="truncate text-neutral-300">
        {p.sym} {p.dir}
        <span className="text-neutral-500"> · {srcLabel(p.src)}</span>
      </span>
      <span className="font-semibold text-neutral-100">{fmtUsd(p.pnl, { sign: true })}</span>
    </div>
  );
}

// ------------------------------------------------------------------ P&L by day
function DailyPanel({ days, showSrc }: { days: Day[]; showSrc: boolean }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const W = Math.max(320, width);
  const M = { l: 52, r: 10, t: 16, b: 22 };
  const PH = 150;
  const H = M.t + PH + M.b;
  const PW = W - M.l - M.r;
  const nets = days.map((d) => d.net);
  const lo0 = Math.min(0, ...nets);
  const hi0 = Math.max(0, ...nets);
  const pad = (hi0 - lo0) * 0.12;
  const yt = niceTicks(lo0 - (lo0 < 0 ? pad : 0), hi0 + (hi0 > 0 ? pad : 0), 4);
  const [ylo, yhi] = [yt[0], yt[yt.length - 1]];
  const y = (v: number) => M.t + (1 - (v - ylo) / Math.max(1e-9, yhi - ylo)) * PH;
  const slot = PW / Math.max(1, days.length);
  const bw = Math.min(24, Math.max(1, slot - 2, slot * 0.68));
  const labelEvery = Math.max(1, Math.ceil(44 / slot));
  const traded = days.map((d, i) => ({ d, i })).filter(({ d }) => d.n > 0);
  const bestI = traded.reduce((b, c) => (b < 0 || c.d.net > days[b].net ? c.i : b), -1);
  const worstI = traded.reduce((b, c) => (b < 0 || c.d.net < days[b].net ? c.i : b), -1);
  const lastI = days.length - 1;
  const labelled = new Set([bestI, worstI, days[lastI]?.n ? lastI : -1].filter((i) => i >= 0));
  const key = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? lastI + 1) - 1));
    else if (e.key === "ArrowRight") setHover((h) => Math.min(lastI, (h ?? -1) + 1));
    else return;
    e.preventDefault();
  };
  const hd = hover != null ? days[hover] : null;
  return (
    <div ref={ref} className="relative outline-none focus-visible:ring-1 focus-visible:ring-neutral-500" tabIndex={0}
         onKeyDown={key} onBlur={() => setHover(null)}
         aria-label={`P&L by day, ${days.length} days. Arrow keys step through the days.`}>
      {width > 0 && (
        <svg width={W} height={H} className="block select-none" role="img" aria-label="P&L by day"
             onPointerLeave={() => setHover(null)}>
          {yt.map((v) => (
            <g key={`y${v}`}>
              <line x1={M.l} x2={W - M.r} y1={y(v)} y2={y(v)} stroke={v === 0 ? COL.zero : COL.grid} strokeWidth={1} />
              <text x={M.l - 6} y={y(v) + 3.5} textAnchor="end" className="fill-neutral-500 text-[10px] tabular-nums">{axUsd(v)}</text>
            </g>
          ))}
          {days.map((d, i) => {
            const cx = M.l + slot * i + slot / 2;
            const showDate = (lastI - i) % labelEvery === 0;
            const first = i === 0 || new Date(d.t).getUTCDate() === 1 || (lastI - i) === Math.floor(lastI / labelEvery) * labelEvery;
            return (
              <g key={d.t}>
                {d.n > 0 && (
                  <path d={barPath(cx - bw / 2, bw, y(0), y(d.net))} fill={d.net >= 0 ? COL.bull : COL.bear}
                        opacity={hover == null || hover === i ? 1 : 0.55} />
                )}
                {labelled.has(i) && d.n > 0 && (
                  <text x={cx} y={d.net >= 0 ? y(d.net) - 4 : y(d.net) + 11} textAnchor="middle"
                        className="fill-neutral-200 text-[10px] font-semibold">{usd0(d.net)}</text>
                )}
                {showDate && (
                  <text x={cx} y={H - 6} textAnchor="middle" className={`text-[10px] ${i === lastI ? "fill-neutral-300" : "fill-neutral-500"}`}>
                    {i === lastI ? "today" : first ? `${new Date(d.t).getUTCDate()} ${MON[new Date(d.t).getUTCMonth()]}` : new Date(d.t).getUTCDate()}
                  </text>
                )}
                {/* the whole column is the hover target, not just the bar */}
                <rect x={M.l + slot * i} y={M.t} width={slot} height={PH} fill="transparent"
                      onPointerEnter={() => setHover(i)} />
              </g>
            );
          })}
        </svg>
      )}
      {width === 0 && <div style={{ height: H }} />}
      {hd && (
        <Tip x={M.l + slot * (hover as number) + slot / 2} w={W}>
          <div className="mb-1 text-[11px] text-neutral-500">{utcDayText(hd.t)}{hover === lastI ? " - today" : ""}</div>
          {hd.n === 0 ? (
            <div className="text-neutral-400">no trade closed</div>
          ) : (<>
            <div className="text-sm font-semibold text-neutral-100">{fmtUsd(hd.net, { sign: true })}</div>
            <div className="text-neutral-400">{hd.n} trade{hd.n === 1 ? "" : "s"} · {hd.wins} won · {hd.n - hd.wins} not</div>
            {showSrc && hd.bySrc.size > 1 && (
              <div className="mt-1 space-y-0.5 border-t border-neutral-800 pt-1">
                {[...hd.bySrc.entries()].sort((a, b) => b[1].n - a[1].n).map(([src, s]) => (
                  <div key={src} className="flex items-center gap-1.5">
                    <Key color={colorFor(src)} />
                    <span className="font-semibold text-neutral-100">{usd0(s.net)}</span>
                    <span className="truncate text-neutral-500">{srcLabel(src)} · {s.n}</span>
                  </div>
                ))}
              </div>
            )}
          </>)}
        </Tip>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ each trade's result
function HistPanel({ pts, avgWin, avgLoss }: { pts: Pt[]; avgWin: number | null; avgLoss: number | null }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const W = Math.max(260, width);
  const M = { l: 34, r: 10, t: 26, b: 22 };
  const PH = 252;
  const H = M.t + PH + M.b;
  const PW = W - M.l - M.r;
  const { bins, under, over } = useMemo(() => {
    const xs = pts.map((p) => p.pnl).sort((a, b) => a - b);
    if (!xs.length) return { bins: [], under: [] as number[], over: [] as number[] };
    // The middle 96% sets the scale: two -$900 trades made every bar $500 wide and put nearly all trades in two bars
    // (2026-11-11). The few beyond it are counted in the end bars, and said so under the chart.
    const q = (f: number) => xs[Math.round(f * (xs.length - 1))];
    const lo = Math.min(0, xs.length >= 25 ? q(0.02) : xs[0]);
    const hi = Math.max(0, xs.length >= 25 ? q(0.98) : xs[xs.length - 1]);
    const w = niceStep(hi - lo || 1, 16);
    const k0 = Math.floor(lo / w);
    const k1 = Math.floor(hi / w);
    const out: { lo: number; hi: number; n: number; sum: number }[] = [];
    for (let k = k0; k <= k1; k++) out.push({ lo: k * w, hi: (k + 1) * w, n: 0, sum: 0 });
    for (const x of xs) {
      const b = out[Math.max(k0, Math.min(k1, Math.floor(x / w))) - k0];
      b.n += 1;
      b.sum += x;
    }
    return { bins: out, under: xs.filter((x) => x < out[0].lo), over: xs.filter((x) => x >= out[out.length - 1].hi) };
  }, [pts]);
  if (!bins.length) return null;
  const top = Math.max(...bins.map((b) => b.n));
  const yt = niceTicks(0, top, 4, 1);
  const yhi = yt[yt.length - 1];
  const y = (n: number) => M.t + (1 - n / Math.max(1, yhi)) * PH;
  const vlo = bins[0].lo;
  const vhi = bins[bins.length - 1].hi;
  const xv = (v: number) => M.l + ((v - vlo) / Math.max(1e-9, vhi - vlo)) * PW;
  const slot = PW / bins.length;
  const bw = Math.min(24, Math.max(1, slot - 2));
  const xt = niceTicks(vlo, vhi, 4).filter((v) => v >= vlo && v <= vhi);
  const hb = hover != null ? bins[hover] : null;
  return (
    <div ref={ref} className="relative" aria-label="Each trade's result, binned by its dollars">
      {width > 0 && (
        <svg width={W} height={H} className="block select-none" role="img"
             aria-label={`Each trade's result: ${pts.length} trades; average win ${avgWin == null ? "none" : usd0(avgWin)}, average loss ${avgLoss == null ? "none" : usd0(avgLoss)}`}
             onPointerLeave={() => setHover(null)}>
          {yt.map((v) => (
            <g key={`y${v}`}>
              <line x1={M.l} x2={W - M.r} y1={y(v)} y2={y(v)} stroke={v === 0 ? COL.zero : COL.grid} strokeWidth={1} />
              <text x={M.l - 5} y={y(v) + 3.5} textAnchor="end" className="fill-neutral-500 text-[10px] tabular-nums">{v}</text>
            </g>
          ))}
          {xt.map((v) => (
            <text key={`x${v}`} x={xv(v)} y={H - 6} textAnchor="middle" className="fill-neutral-500 text-[10px] tabular-nums">{axUsd(v)}</text>
          ))}
          {bins.map((b, i) => (
            <g key={b.lo}>
              {b.n > 0 && (
                <path d={barPath(M.l + slot * i + (slot - bw) / 2, bw, y(0), y(b.n))} fill={b.lo >= 0 ? COL.bull : COL.bear}
                      opacity={hover == null || hover === i ? 1 : 0.55} />
              )}
              <rect x={M.l + slot * i} y={M.t} width={slot} height={PH} fill="transparent" onPointerEnter={() => setHover(i)} />
            </g>
          ))}
          <line x1={xv(0)} x2={xv(0)} y1={M.t - 4} y2={M.t + PH} stroke={COL.zero} strokeWidth={1} />
          {avgLoss != null && (
            <g pointerEvents="none">
              <line x1={xv(avgLoss)} x2={xv(avgLoss)} y1={M.t - 6} y2={M.t + PH} stroke="#a3a3a3" strokeWidth={1} />
              <text x={xv(avgLoss) - 4} y={M.t - 10} textAnchor="end" className="fill-neutral-300 text-[10px]">avg loss {usd0(avgLoss)}</text>
            </g>
          )}
          {avgWin != null && (
            <g pointerEvents="none">
              <line x1={xv(avgWin)} x2={xv(avgWin)} y1={M.t - 6} y2={M.t + PH} stroke="#a3a3a3" strokeWidth={1} />
              <text x={xv(avgWin) + 4} y={M.t - 10} className="fill-neutral-300 text-[10px]">avg win {usd0(avgWin)}</text>
            </g>
          )}
        </svg>
      )}
      {width === 0 && <div style={{ height: H }} />}
      {(under.length > 0 || over.length > 0) && (
        <div className="text-[10px] text-neutral-500">
          The end bars also hold the {under.length + over.length} biggest result{under.length + over.length === 1 ? "" : "s"}
          {under.length > 0 && `, down to ${usd0(under[0])}`}{over.length > 0 && `${under.length ? " and" : ","} up to ${usd0(over[over.length - 1])}`}.
        </div>
      )}
      {hb && (
        <Tip x={M.l + slot * (hover as number) + slot / 2} w={W}>
          <div className="mb-1 text-[11px] text-neutral-500">
            {hover === 0 && under.length ? `a result under ${axUsd(hb.hi)} - ${under.length} of them below ${axUsd(hb.lo)}`
              : hover === bins.length - 1 && over.length ? `a result over ${axUsd(hb.lo)} - ${over.length} of them above ${axUsd(hb.hi)}`
              : `a result from ${axUsd(hb.lo)} to ${axUsd(hb.hi)}`}
          </div>
          <div className="text-sm font-semibold text-neutral-100">{hb.n} trade{hb.n === 1 ? "" : "s"}</div>
          {hb.n > 0 && <div className="text-neutral-400">{fmtUsd(hb.sum, { sign: true })} together</div>}
        </Tip>
      )}
    </div>
  );
}
