import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import type { MonitorBias, MonitorNote, MonitorResponse, MonitorRow, MonitorTf } from "../types";

interface Props {
  /** The pair on the chart right now - its row is highlighted. */
  currentSymbol: string;
  /** Chart a pair; `timeframe` is set when a specific timeframe cell was clicked. */
  onSelect: (p: { symbol: string; asset_class: string; timeframe?: string }) => void;
}

const TFS = ["1h", "4h", "1d"] as const;
// The backend rebuilds the table at most every 20s and the slow timeframes far less often, so a
// minute between polls loses nothing while the drawer is open (it stops polling when closed).
const POLL_MS = 60_000;

type Filter = "all" | "aligned" | "pullback" | "zone" | "stretched";
type Sort = "clarity" | "alpha" | "move";

const FILTERS: { id: Filter; label: string; hint: string }[] = [
  { id: "all", label: "All", hint: "Every watched pair" },
  { id: "aligned", label: "Aligned", hint: "1H, 4H and 1D all point the same way" },
  { id: "pullback", label: "Dips & bounces", hint: "4H and 1D agree, the 1H is moving against them" },
  { id: "zone", label: "At a zone", hint: "Price is inside, or within 0.5 ATR of, a demand/supply zone" },
  { id: "stretched", label: "Stretched RSI", hint: "RSI 70+ or 30- on the 1H or 4H" },
];

const BIAS: Record<MonitorBias, { label: string; cls: string; hint: string; rank: number }> = {
  aligned_up: { label: "Aligned up", cls: "bg-bull/15 text-bull ring-1 ring-inset ring-bull/30", rank: 0,
    hint: "1H, 4H and 1D all point up" },
  aligned_down: { label: "Aligned down", cls: "bg-bear/15 text-bear ring-1 ring-inset ring-bear/30", rank: 0,
    hint: "1H, 4H and 1D all point down" },
  pullback_up: { label: "Dip in uptrend", cls: "text-bull ring-1 ring-inset ring-bull/30", rank: 1,
    hint: "4H and 1D point up; the 1H is moving down against them" },
  pullback_down: { label: "Bounce in downtrend", cls: "text-bear ring-1 ring-inset ring-bear/30", rank: 1,
    hint: "4H and 1D point down; the 1H is moving up against them" },
  counter_up: { label: "Up vs the 1D", cls: "text-neutral-300 ring-1 ring-inset ring-neutral-700", rank: 2,
    hint: "1H and 4H point up, but the daily points down" },
  counter_down: { label: "Down vs the 1D", cls: "text-neutral-300 ring-1 ring-inset ring-neutral-700", rank: 2,
    hint: "1H and 4H point down, but the daily points up" },
  mixed: { label: "Mixed", cls: "text-neutral-500 ring-1 ring-inset ring-neutral-800", rank: 3,
    hint: "No two higher timeframes agree" },
};

const STATE_LABEL: Record<string, string> = {
  impulse: "impulse", pullback: "pullback", rejection: "rejection", breakout: "breakout",
  breakdown: "breakdown", range: "range", reversal: "reversal", unclear: "",
};

function nearZone(t: MonitorTf | null | undefined): boolean {
  if (!t) return false;
  const z = t.zones;
  if (z.inside) return true;
  return [z.demand, z.supply].some((b) => b?.dist_atr != null && b.dist_atr <= 0.5);
}

function stretched(t: MonitorTf | null | undefined): boolean {
  return t?.rsi_zone === "overbought" || t?.rsi_zone === "oversold";
}

function avgAdx(r: MonitorRow): number {
  const v = TFS.map((tf) => r.tfs[tf]?.adx).filter((x): x is number => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
}

function ago(iso: string | undefined, now: number): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
}

/**
 * The watchlist on one screen: for every watched pair, which way the 1H, 4H and 1D point, how hard,
 * where RSI sits, and whether price is on a demand/supply zone - so a manual trade starts from the
 * whole picture instead of twenty chart switches.
 *
 * Every reading is the engine's own (the backend reuses its indicator code), so an arrow here never
 * disagrees with the analysis panel, and a zone here is a zone on the chart's order-block overlay.
 * It is a read-out: nothing on this screen opens, sizes or blocks a trade.
 */
export function MarketMonitor({ currentSymbol, onSelect }: Props) {
  const [data, setData] = useState<MonitorResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>("clarity");
  const [legend, setLegend] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const inFlight = useRef(false);

  const load = useCallback(async (refresh = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    try {
      setData(await api.monitor(refresh));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(() => load(), POLL_MS);
    // A clock for "updated 12s ago" - cheap, and it makes a stale table visibly stale.
    const c = setInterval(() => setNow(Date.now()), 5_000);
    return () => {
      clearInterval(t);
      clearInterval(c);
    };
  }, [load]);

  const rows = useMemo(() => {
    const all = data?.rows ?? [];
    const kept = all.filter((r) => {
      switch (filter) {
        case "aligned":
          return r.bias === "aligned_up" || r.bias === "aligned_down";
        case "pullback":
          return r.bias === "pullback_up" || r.bias === "pullback_down";
        case "zone":
          return TFS.some((tf) => nearZone(r.tfs[tf]));
        case "stretched":
          return stretched(r.tfs["1h"]) || stretched(r.tfs["4h"]);
        default:
          return true;
      }
    });
    const by = [...kept];
    if (sort === "alpha") by.sort((a, b) => a.symbol.localeCompare(b.symbol));
    else if (sort === "move") by.sort((a, b) => Math.abs(b.change_pct ?? 0) - Math.abs(a.change_pct ?? 0));
    // Clearest first: aligned, then dips/bounces, then counter, then mixed; within a tier the
    // stronger average ADX first. Closed markets sink - their readings are a session old.
    else
      by.sort(
        (a, b) =>
          Number(a.market_open === false) - Number(b.market_open === false) ||
          BIAS[a.bias].rank - BIAS[b.bias].rank ||
          avgAdx(b) - avgAdx(a),
      );
    return by;
  }, [data, filter, sort]);

  const counts = useMemo(() => {
    const all = data?.rows ?? [];
    return {
      all: all.length,
      aligned: all.filter((r) => r.bias.startsWith("aligned")).length,
      pullback: all.filter((r) => r.bias.startsWith("pullback")).length,
      zone: all.filter((r) => TFS.some((tf) => nearZone(r.tfs[tf]))).length,
      stretched: all.filter((r) => stretched(r.tfs["1h"]) || stretched(r.tfs["4h"])).length,
    } as Record<Filter, number>;
  }, [data]);

  return (
    <div className="space-y-3">
      {/* ── toolbar ── */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter pairs">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              aria-pressed={filter === f.id}
              title={f.hint}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${
                filter === f.id
                  ? "bg-brand-600 text-white"
                  : "bg-neutral-800/70 text-neutral-300 hover:bg-neutral-700"
              }`}
            >
              {f.label}
              <span className={`ml-1.5 tabular-nums ${filter === f.id ? "text-blue-100" : "text-neutral-500"}`}>
                {data ? counts[f.id] : ""}
              </span>
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2 text-xs text-neutral-500">
          <label className="flex items-center gap-1.5">
            Sort
            <select value={sort} onChange={(e) => setSort(e.target.value as Sort)} className="field py-0.5 text-xs">
              <option value="clarity">Clearest trend first</option>
              <option value="move">Biggest move today</option>
              <option value="alpha">A–Z</option>
            </select>
          </label>
          <span className="tabular-nums" title={data ? new Date(data.generated_at).toLocaleString() : undefined}>
            {loading ? "updating…" : data ? `updated ${ago(data.generated_at, now)}` : ""}
          </span>
          <button onClick={() => load(true)} disabled={loading} className="btn btn-subtle px-2 py-1 text-xs">
            Refresh
          </button>
          <button
            onClick={() => setLegend((v) => !v)}
            aria-expanded={legend}
            className="btn btn-ghost px-2 py-1 text-xs"
          >
            {legend ? "Hide guide" : "How to read"}
          </button>
        </div>
      </div>

      {legend && <Legend />}

      {error && (
        <div className="rounded-lg border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
          Monitor unavailable: {error}
          {error.startsWith("404") && " — the app is running older code. Restart it (Stop, then Start AI Trading Desk)."}
        </div>
      )}

      {!data && !error && (
        <div className="rounded-lg border border-neutral-800 px-3 py-6 text-center text-sm text-neutral-500">
          Reading 1H, 4H and 1D for every watched pair…
        </div>
      )}

      {data && data.rows.length === 0 && (
        <div className="rounded-lg border border-neutral-800 px-3 py-6 text-center text-sm text-neutral-500">
          Your watchlist is empty. Add pairs from the Watchlist in the sidebar.
        </div>
      )}

      {data && data.rows.length > 0 && rows.length === 0 && (
        <div className="rounded-lg border border-neutral-800 px-3 py-6 text-center text-sm text-neutral-500">
          No pair matches “{FILTERS.find((f) => f.id === filter)?.label}” right now.
        </div>
      )}

      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-neutral-800">
          <table className="w-full min-w-[46rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-neutral-800 bg-neutral-900/80 text-left text-[11px] uppercase tracking-wider text-neutral-500">
                <th className="sticky left-0 z-[1] bg-neutral-900 px-3 py-2 font-semibold">Pair</th>
                <th className="px-3 py-2 font-semibold">Today</th>
                {TFS.map((tf) => (
                  <th key={tf} className="px-2 py-2 font-semibold" title={`Click a ${tf.toUpperCase()} cell to open that chart`}>
                    {tf.toUpperCase()}
                  </th>
                ))}
                <th className="hidden px-3 py-2 font-semibold xl:table-cell">Bias</th>
                <th className="hidden px-3 py-2 font-semibold 2xl:table-cell">What to watch</th>
              </tr>
            </thead>
            {/* One <tbody> per pair: below 2xl the notes drop to their own full-width line under the
                readings (a squeezed side column wrapped them to three words a line and the scroll box
                cut them off), and the pair's two lines must hover and border as one row. */}
            {rows.map((r) => (
              <Row
                key={`${r.symbol}:${r.asset_class}`}
                r={r}
                active={r.symbol.toUpperCase() === currentSymbol.toUpperCase()}
                onSelect={onSelect}
              />
            ))}
          </table>
        </div>
      )}

      <p className="text-[11px] text-neutral-600">
        Read-outs to frame your own decision, not trade signals. The direction, RSI and zones are the
        engine&apos;s own readings; the zones are the chart&apos;s order blocks on that timeframe.
      </p>
    </div>
  );
}

function Row({ r, active, onSelect }: { r: MonitorRow; active: boolean; onSelect: Props["onSelect"] }) {
  const b = BIAS[r.bias];
  const chg = r.change_pct;
  // `false` only: null means "unknown" (e.g. no broker serves this pair) and must not read as closed.
  const closed = r.market_open === false;
  const crypto = r.asset_class === "crypto";
  const noData = TFS.every((tf) => !r.tfs[tf]);
  return (
    <tbody
      className={`group border-b border-neutral-800/70 transition last:border-b-0 hover:bg-neutral-800/30 ${
        active ? "bg-brand-500/[0.07]" : ""
      } ${closed ? "opacity-60" : ""}`}
    >
      <tr className="align-top">
        {/* Pair - pinned, so a row scrolled sideways on a narrow window still says which pair it is.
            Sticky cells need an opaque background; the active pair gets an accent bar instead of the
            row tint that background would hide. */}
        <td
          className={`sticky left-0 z-[1] bg-neutral-950 px-3 py-2.5 group-hover:bg-neutral-900 ${
            active ? "before:absolute before:inset-y-0 before:left-0 before:w-[3px] before:bg-brand-500" : ""
          }`}
        >
          <button
            onClick={() => onSelect({ symbol: r.symbol, asset_class: r.asset_class })}
            className="text-left font-semibold text-neutral-100 hover:text-white hover:underline"
            title={`Open ${displaySymbol(r.symbol)} on the chart`}
          >
            {displaySymbol(r.symbol)}
          </button>
          <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[11px] text-neutral-500">
            <span>{r.asset_class}</span>
            {closed && (
              <span
                className="rounded bg-neutral-800 px-1 text-neutral-400"
                title={crypto
                  ? "No new prices for 10+ minutes - crypto trades 24/7, so the feed is quiet rather than closed"
                  : "No fresh prices - the session is closed"}
              >
                {crypto ? "⏸ no ticks" : "⏸ closed"}
              </span>
            )}
            {r.events_soon && (
              <span className="rounded bg-warn/15 px-1 font-medium text-warn" title={r.events_soon}>
                ⚑ news
              </span>
            )}
            {r.news_pending && !r.events_soon && (
              <span className="text-neutral-600" title="The economic calendar is still loading - it fills on the next refresh">
                news…
              </span>
            )}
          </div>
          {/* Narrow windows: the bias rides under the pair name - as the last column it was the first
              thing cut off, and it is the one-glance summary of the row. */}
          <span
            className={`mt-1.5 inline-block whitespace-nowrap rounded-md px-1.5 py-px text-[11px] font-semibold xl:hidden ${b.cls}`}
            title={b.hint}
          >
            {b.label}
          </span>
          {r.errors.length > 0 && (
            <div className="mt-1 max-w-[9rem] text-[11px] text-bear" title={r.errors.join("\n")}>
              {noData
                ? "no data for this pair"
                : `${r.errors.length} timeframe${r.errors.length > 1 ? "s" : ""} unavailable`}
            </div>
          )}
        </td>

        {/* Today */}
        <td className="px-3 py-2.5 tabular-nums">
          <div className="text-neutral-200">{fmtPrice(r.price)}</div>
          <div
            className={`text-xs font-medium ${
              chg == null ? "text-neutral-600" : chg > 0 ? "text-bull" : chg < 0 ? "text-bear" : "text-neutral-400"
            }`}
          >
            {chg == null ? "—" : `${chg > 0 ? "+" : ""}${chg.toFixed(2)}%`}
          </div>
          <RangeUsed v={r.range_used} />
        </td>

        {/* 1H / 4H / 1D */}
        {TFS.map((tf) => (
          <td key={tf} className="px-1 py-1.5">
            <TfCell
              tf={tf}
              t={r.tfs[tf]}
              onClick={() => onSelect({ symbol: r.symbol, asset_class: r.asset_class, timeframe: tf })}
            />
          </td>
        ))}

        {/* Bias */}
        <td className="hidden px-3 py-2.5 xl:table-cell">
          <span
            className={`inline-block whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-semibold ${b.cls}`}
            title={b.hint}
          >
            {b.label}
          </span>
        </td>

        {/* What to watch - its own column on wide screens */}
        <td className="hidden px-3 py-2.5 2xl:table-cell">
          <Notes notes={r.notes} />
        </td>
      </tr>
      {/* ...and its own line under the readings on narrower ones. Pinned left like the pair name, so
          scrolling the table sideways does not carry the notes out of view. */}
      {r.notes.length > 0 && (
        <tr className="2xl:hidden">
          <td colSpan={6} className="px-3 pb-2.5 pt-0">
            <div className="sticky left-3 max-w-[40rem] border-l-2 border-neutral-800 pl-2.5">
              <Notes notes={r.notes} />
            </div>
          </td>
        </tr>
      )}
    </tbody>
  );
}

/** How much of a normal day's range (daily ATR) today has already covered. Past 100% the day's move
 *  is extended, which is worth knowing before chasing it. */
function RangeUsed({ v }: { v: number | null }) {
  if (v == null) return null;
  const pct = Math.round(v * 100);
  const w = Math.min(v, 1.5) / 1.5;
  return (
    <div className="mt-1 w-16" title={`Today has covered ${pct}% of a normal day's range (daily ATR)`}>
      <div className="relative h-1 rounded-full bg-neutral-800">
        <div
          className={`absolute inset-y-0 left-0 rounded-full ${v >= 1 ? "bg-warn" : "bg-neutral-500"}`}
          style={{ width: `${w * 100}%` }}
        />
        {/* the 100% mark */}
        <div className="absolute inset-y-[-2px] w-px bg-neutral-400/60" style={{ left: `${(1 / 1.5) * 100}%` }} />
      </div>
      <div className="mt-0.5 text-[10px] text-neutral-500">{pct}% of day</div>
    </div>
  );
}

function TfCell({ tf, t, onClick }: { tf: string; t: MonitorTf | null | undefined; onClick: () => void }) {
  if (!t) {
    return <div className="px-2 py-1 text-xs text-neutral-600">no data</div>;
  }
  const up = t.trend === "up";
  const down = t.trend === "down";
  const weak = t.strength === "weak";
  const dirCls = up ? "text-bull" : down ? "text-bear" : "text-neutral-400";
  const bars = t.strength === "strong" ? 3 : t.strength === "moderate" ? 2 : t.strength === "weak" ? 1 : 0;
  const z = t.zones;
  const insideKinds = z.inside_kinds?.length ? z.inside_kinds : z.inside ? [z.inside.kind] : [];
  // Every zone within 0.5 ATR, nearest first - when price is squeezed between a demand and a supply
  // zone, showing only one of them (whichever came first) hid the one price was actually sitting on.
  const near: { kind: "demand" | "supply"; dist: number }[] = insideKinds.length
    ? []
    : (
        [
          z.demand?.dist_atr != null && z.demand.dist_atr <= 0.5 ? { kind: "demand" as const, dist: z.demand.dist_atr } : null,
          z.supply?.dist_atr != null && z.supply.dist_atr <= 0.5 ? { kind: "supply" as const, dist: z.supply.dist_atr } : null,
        ].filter(Boolean) as { kind: "demand" | "supply"; dist: number }[]
      ).sort((a, b) => a.dist - b.dist);
  const title = [
    `${tf.toUpperCase()}: ${t.trend ?? "?"}${t.ema_stacked ? " (EMA 20/50/200 fully stacked)" : ""}`,
    t.adx != null ? `ADX ${t.adx.toFixed(0)}${t.adx_dir ? `, ${t.adx_dir}` : ""} - ${t.strength} trend` : null,
    t.rsi != null ? `RSI ${t.rsi.toFixed(1)}${t.rsi_prev != null ? ` (prev ${t.rsi_prev.toFixed(1)})` : ""}` : null,
    t.price_vs_ema200 ? `price ${t.price_vs_ema200} the EMA 200` : null,
    t.state_headline ? `now: ${t.state_headline}` : null,
    insideKinds.length ? `inside a ${insideKinds.join(" and a ")} zone` : null,
    `structure ${t.structure}${t.choch_side ? ` - ${t.choch_side === "up" ? "bullish" : "bearish"} change of character` : ""}`,
    "Click to open this chart",
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <button
      onClick={onClick}
      title={title}
      className="w-full min-w-[8.5rem] rounded-md px-2 py-1 text-left transition hover:bg-neutral-800/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50"
    >
      {/* line 1: direction + strength */}
      <div className="flex items-center gap-1.5">
        <span className={`text-sm font-semibold ${dirCls} ${weak ? "opacity-60" : ""}`}>
          {up ? "▲ Up" : down ? "▼ Down" : "■ Flat"}
        </span>
        <span className="flex items-end gap-px" aria-label={`${t.strength ?? "unknown"} trend strength`}>
          {[1, 2, 3].map((i) => (
            <span
              key={i}
              className={`w-[3px] rounded-sm ${i <= bars ? (up ? "bg-bull" : down ? "bg-bear" : "bg-neutral-400") : "bg-neutral-700"}`}
              style={{ height: `${4 + i * 3}px` }}
            />
          ))}
        </span>
        {t.adx_dir === "rising" && <span className="text-[10px] text-neutral-400" aria-label="strength rising">↗</span>}
        {t.adx_dir === "falling" && <span className="text-[10px] text-neutral-600" aria-label="strength fading">↘</span>}
      </div>
      {/* line 2: RSI */}
      <div className="mt-1 flex items-center gap-1.5">
        <RsiPill t={t} />
        {STATE_LABEL[t.state] && <span className="text-[11px] text-neutral-500">{STATE_LABEL[t.state]}</span>}
      </div>
      {/* line 3: zone(s) */}
      {insideKinds.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {insideKinds.map((k) => (
            <span
              key={k}
              className={`rounded px-1.5 py-px text-[11px] font-medium ${
                k === "demand" ? "bg-cyan-500/15 text-cyan-300" : "bg-amber-500/15 text-amber-300"
              }`}
            >
              in {k}
            </span>
          ))}
        </div>
      )}
      {near.map((n) => (
        <div key={n.kind} className={`mt-0.5 text-[11px] ${n.kind === "demand" ? "text-cyan-400/80" : "text-amber-400/80"}`}>
          near {n.kind} · {n.dist.toFixed(1)} ATR
        </div>
      ))}
    </button>
  );
}

function RsiPill({ t }: { t: MonitorTf }) {
  if (t.rsi == null) return <span className="text-[11px] text-neutral-600">RSI —</span>;
  const v = t.rsi;
  // Extremes are amber ("stretched"), not red/green: an overbought reading is not a bearish signal,
  // it says the move is extended - which matters in either direction.
  const cls =
    t.rsi_zone === "overbought" || t.rsi_zone === "oversold"
      ? "bg-warn/15 text-warn ring-1 ring-inset ring-warn/30"
      : v >= 50
        ? "bg-bull/10 text-bull/90"
        : "bg-bear/10 text-bear/90";
  const turning = t.rsi_prev != null ? (v > t.rsi_prev + 0.5 ? "↑" : v < t.rsi_prev - 0.5 ? "↓" : "") : "";
  // Rounded TOWARD 50, so the label never crosses a threshold the reading has not: 69.6 shows "69",
  // not an unmarked "70" next to a legend that says 70+ is overbought.
  const shown = v >= 50 ? Math.floor(v) : Math.ceil(v);
  return (
    <span className={`rounded px-1.5 py-px text-[11px] font-semibold tabular-nums ${cls}`} title={`RSI ${v.toFixed(1)}`}>
      RSI {shown}
      {turning && <span className="ml-0.5 font-normal opacity-70">{turning}</span>}
      {t.rsi_zone === "overbought" && <span className="ml-1 font-normal">OB</span>}
      {t.rsi_zone === "oversold" && <span className="ml-1 font-normal">OS</span>}
    </span>
  );
}

const TONE: Record<MonitorNote["tone"], { icon: string; cls: string }> = {
  warn: { icon: "⚑", cls: "text-warn" },
  caution: { icon: "!", cls: "text-orange-300" },
  info: { icon: "•", cls: "text-neutral-400" },
};

function Notes({ notes }: { notes: MonitorNote[] }) {
  const [all, setAll] = useState(false);
  if (!notes.length) return <span className="text-xs text-neutral-600">nothing notable</span>;
  const shown = all ? notes : notes.slice(0, 3);
  return (
    <ul className="space-y-0.5">
      {shown.map((n, i) => (
        <li key={i} className="flex gap-1.5 text-xs leading-snug">
          <span className={`w-2.5 shrink-0 text-center font-bold ${TONE[n.tone].cls}`} aria-hidden="true">
            {TONE[n.tone].icon}
          </span>
          <span className={n.tone === "info" ? "text-neutral-400" : "text-neutral-200"}>{n.text}</span>
        </li>
      ))}
      {notes.length > 3 && (
        <li>
          <button onClick={() => setAll((v) => !v)} className="pl-4 text-[11px] text-neutral-500 hover:text-neutral-300">
            {all ? "show less" : `+${notes.length - 3} more`}
          </button>
        </li>
      )}
    </ul>
  );
}

function Legend() {
  const item = (k: string, v: string) => (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 font-semibold text-neutral-300">{k}</dt>
      <dd className="text-neutral-400">{v}</dd>
    </div>
  );
  return (
    <dl className="grid gap-1.5 rounded-lg border border-neutral-800 bg-neutral-900/60 p-3 text-xs md:grid-cols-2">
      {item("▲ / ▼", "Direction from the EMA 20 vs EMA 50 (and the 200 when all three stack) - the same read the engine uses for its higher-timeframe filter. Faded = weak trend.")}
      {item("Bars ↗ ↘", "Trend strength from ADX: 3 bars strong (25+), 2 moderate (20-25), 1 weak (under 20). ↗ strength building, ↘ fading.")}
      {item("RSI", "14-period. Green above 50, red below. Amber OB/OS = 70+ or 30- (stretched, either way). ↑↓ = turning since the last bar.")}
      {item("State", "What the latest bar is doing: pullback, breakout, rejection, range, impulse or reversal.")}
      {item("in / near zone", "Order blocks on that timeframe - the same zones as the chart's Order-blocks overlay. Cyan = demand, amber = supply. Near = within 0.5 ATR.")}
      {item("Bias", "How the three timeframes line up. Dip in uptrend = 4H and 1D up, 1H down. Up vs the 1D = 1H and 4H up against the daily.")}
      {item("% of day", "Today's range so far against a normal day's (daily ATR). Past 100% the day's move is already extended.")}
      {item("Click", "A pair name opens it on the chart; a 1H / 4H / 1D cell opens that timeframe.")}
    </dl>
  );
}
