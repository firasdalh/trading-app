import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice, fmtUsd } from "../format";
import type {
  HybridVerdict, MonitorBias, MonitorHybrid, MonitorNote, MonitorResponse, MonitorRow, MonitorTf, PositionView,
} from "../types";
import { at, span } from "./MarketHoursChip";

interface Props {
  /** The pair on the chart right now - its row is highlighted. */
  currentSymbol: string;
  /** Chart a pair; `timeframe` is set when a specific timeframe cell was clicked. */
  onSelect: (p: { symbol: string; asset_class: string; timeframe?: string }) => void;
  /** The live book: a pair you hold is marked, with each timeframe judged against your side. */
  positions?: PositionView[] | null;
}

// Columns shown. The 15M is entry timing only: the bias, the filters and the sort are still read from
// 1H / 4H / 1D (BIAS_TFS), so a 15-minute wiggle never relabels a pair.
const TFS = ["15m", "1h", "4h", "1d"] as const;
const BIAS_TFS = ["1h", "4h", "1d"] as const;
// +DI and -DI must differ by at least this much before either side "owns" the ADX strength.
const DI_MARGIN = 5;
// The backend rebuilds the table at most every 20s and the slow timeframes far less often, so a
// minute between polls loses nothing while the drawer is open (it stops polling when closed).
const POLL_MS = 60_000;

type Filter = "all" | "mine" | "setups" | "aligned" | "pullback" | "zone" | "stretched";
type Sort = "clarity" | "strength" | "alpha" | "move";

// A closed market normally went quiet hours ago; a symbol with no bar for longer than this has stopped
// trading on the account (IN50m's newest bar is from June 2023). Longer than any weekend + holiday.
const STALE_MS = 4 * 24 * 3600_000;

/** Hybrid outcomes that are a trade idea (taken, under the bar, blocked, or waiting for a break). */
const isSetup = (v: HybridVerdict | null | undefined) =>
  !!v && (v.outcome === "candidate" || v.outcome === "below_bar" || v.outcome === "risk_veto" || v.outcome === "arm");

const FILTERS: { id: Filter; label: string; hint: string }[] = [
  { id: "all", label: "All", hint: "Every watched pair" },
  { id: "mine", label: "Mine", hint: "Pairs you have a position open on" },
  { id: "setups", label: "Hybrid setups", hint: "Pairs where the Hybrid's last scan found a trade, or a level to wait for" },
  { id: "aligned", label: "Aligned", hint: "1H, 4H and 1D all point the same way" },
  { id: "pullback", label: "Dips & bounces", hint: "4H and 1D agree, the 1H is moving against them" },
  { id: "zone", label: "At a zone", hint: "Price is inside, or within 0.5 ATR of, a demand/supply zone" },
  { id: "stretched", label: "Stretched RSI", hint: "RSI 70+ or 30- on the 1H or 4H" },
];

export const BIAS: Record<MonitorBias, { label: string; cls: string; hint: string; rank: number }> = {
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
  const v = BIAS_TFS.map((tf) => r.tfs[tf]?.adx).filter((x): x is number => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0;
}

/** Directional trend strength: ADX signed by each timeframe's arrow, summed and divided by THREE. ADX
 *  alone is direction-blind, so a pair whose timeframes trend hard against each other ranked top;
 *  dividing by the readable count let one readable timeframe outrank three. */
function trendStrength(r: MonitorRow): number {
  const s = BIAS_TFS.reduce((acc, tf) => {
    const t = r.tfs[tf];
    if (t?.adx == null) return acc;
    return acc + (t.trend === "up" ? t.adx : t.trend === "down" ? -t.adx : 0);
  }, 0);
  return Math.abs(s) / BIAS_TFS.length;
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
export function MarketMonitor({ currentSymbol, onSelect, positions }: Props) {
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

  // Your open positions by symbol (a scale-in can mean two on one pair).
  const posBySym = useMemo(() => {
    const m = new Map<string, PositionView[]>();
    for (const p of positions ?? []) {
      const k = p.symbol.toUpperCase();
      m.set(k, [...(m.get(k) ?? []), p]);
    }
    return m;
  }, [positions]);

  const rows = useMemo(() => {
    const all = data?.rows ?? [];
    const kept = all.filter((r) => {
      switch (filter) {
        case "mine":
          return posBySym.has(r.symbol.toUpperCase());
        case "setups":
          return isSetup(r.hybrid);
        case "aligned":
          return r.bias === "aligned_up" || r.bias === "aligned_down";
        case "pullback":
          return r.bias === "pullback_up" || r.bias === "pullback_down";
        case "zone":
          return BIAS_TFS.some((tf) => nearZone(r.tfs[tf]));
        case "stretched":
          return stretched(r.tfs["1h"]) || stretched(r.tfs["4h"]);
        default:
          return true;
      }
    });
    const by = [...kept];
    if (sort === "alpha") by.sort((a, b) => a.symbol.localeCompare(b.symbol));
    else if (sort === "move") by.sort((a, b) => Math.abs(b.change_pct ?? 0) - Math.abs(a.change_pct ?? 0));
    else if (sort === "strength")
      by.sort(
        (a, b) =>
          Number(a.market_open === false) - Number(b.market_open === false) || trendStrength(b) - trendStrength(a),
      );
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
  }, [data, filter, sort, posBySym]);

  const counts = useMemo(() => {
    const all = data?.rows ?? [];
    return {
      all: all.length,
      mine: all.filter((r) => posBySym.has(r.symbol.toUpperCase())).length,
      setups: all.filter((r) => isSetup(r.hybrid)).length,
      aligned: all.filter((r) => r.bias.startsWith("aligned")).length,
      pullback: all.filter((r) => r.bias.startsWith("pullback")).length,
      zone: all.filter((r) => BIAS_TFS.some((tf) => nearZone(r.tfs[tf]))).length,
      stretched: all.filter((r) => stretched(r.tfs["1h"]) || stretched(r.tfs["4h"])).length,
    } as Record<Filter, number>;
  }, [data, posBySym]);

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
              <option value="strength">Strongest trend (ADX)</option>
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

      {data?.hybrid && <HybridSummary h={data.hybrid} now={now} />}

      {legend && <Legend />}

      {error && (
        <div className="rounded-lg border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
          Monitor unavailable: {error}
          {error.startsWith("404") && " — the app is running older code. Restart it (Stop, then Start AI Trading Desk)."}
        </div>
      )}

      {!data && !error && (
        <div className="rounded-lg border border-neutral-800 px-3 py-6 text-center text-sm text-neutral-500">
          Reading 15M, 1H, 4H and 1D for every watched pair…
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
          <table className="w-full min-w-[56rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-neutral-800 bg-neutral-900/80 text-left text-[11px] uppercase tracking-wider text-neutral-500">
                <th className="sticky left-0 z-[1] bg-neutral-900 px-3 py-2 font-semibold">Pair</th>
                <th className="px-3 py-2 font-semibold">Today</th>
                {TFS.map((tf) => (
                  <th key={tf} className="px-2 py-2 font-semibold"
                      title={tf === "15m"
                        ? "Entry timing - shown, but not part of the bias. Click a 15M cell to open that chart"
                        : `Click a ${tf.toUpperCase()} cell to open that chart`}>
                    {tf.toUpperCase()}{tf === "15m" && <span className="ml-1 normal-case tracking-normal text-neutral-600">timing</span>}
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
                positions={posBySym.get(r.symbol.toUpperCase()) ?? []}
                hybrid={data?.hybrid ?? null}
                now={now}
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

function Row({
  r, active, onSelect, positions, hybrid, now,
}: {
  r: MonitorRow;
  active: boolean;
  onSelect: Props["onSelect"];
  /** Your open positions on this pair. */
  positions: PositionView[];
  hybrid: MonitorHybrid | null;
  now: number;
}) {
  const b = BIAS[r.bias];
  const chg = r.change_pct;
  // `false` only: null means "unknown" (e.g. no broker serves this pair) and must not read as closed.
  const closed = r.market_open === false;
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
            <HoursTag r={r} now={now} />
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
          {positions.map((p) => (
            <PositionTag key={p.id ?? `${p.symbol}-${p.direction}-${p.entry_price}`} p={p} r={r} />
          ))}
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

        {/* 15M / 1H / 4H / 1D */}
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
          <Watch r={r} hybrid={hybrid} now={now} />
        </td>
      </tr>
      {/* ...and its own line under the readings on narrower ones. Pinned left like the pair name, so
          scrolling the table sideways does not carry the notes out of view. */}
      {(r.notes.length > 0 || hasHybridLine(r, hybrid)) && (
        <tr className="2xl:hidden">
          <td colSpan={6} className="px-3 pb-2.5 pt-0">
            <div className="sticky left-3 max-w-[40rem] border-l-2 border-neutral-800 pl-2.5">
              <Watch r={r} hybrid={hybrid} now={now} />
            </div>
          </td>
        </tr>
      )}
    </tbody>
  );
}

const CLOSURE_WORD: Record<string, string> = {
  daily_break: "daily break", overnight: "night", weekend: "weekend", unscheduled: "break",
};

/**
 * Market hours for the row: closed pairs say when they reopen, open ones warn when a close is near
 * (2 hours ahead, 24 before the weekend - amber, red inside 15 minutes), and a symbol with no prices
 * for days says so outright instead of passing for "closed". Hours learned from the broker's own
 * prices (backend data/market_hours.py); nothing is shown while a close is far off.
 */
function HoursTag({ r, now }: { r: MonitorRow; now: number }) {
  const h = r.hours;
  const crypto = r.asset_class === "crypto";
  const lastAt = r.last_bar_at ? Date.parse(r.last_bar_at) : null;
  if (r.market_open === false) {
    if (lastAt != null && now - lastAt > STALE_MS) {
      const since = new Date(lastAt).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
      return (
        <span
          className="rounded bg-bear/15 px-1 font-medium text-bear"
          title={`No prices since ${since}: this symbol has stopped trading on your account (or was renamed), so every reading in this row is that old. Check it in MT5, or remove it from the watchlist.`}
        >
          ⚠ no prices since {since}
        </span>
      );
    }
    const opens = !crypto ? h?.opens_at : null;
    return (
      <span
        className="rounded bg-neutral-800 px-1 text-neutral-400"
        title={crypto
          ? "No new prices for 10+ minutes - crypto trades 24/7, so the feed is quiet rather than closed"
          : `No fresh prices - the session is closed.${opens ? ` Reopens ${at(opens, now)}${h?.closure && h.closure !== "unscheduled" ? ` after its ${CLOSURE_WORD[h.closure]}` : ""}.` : ""}${h?.off_schedule ? " It normally trades at this hour - a holiday or a broker pause." : ""}`}
      >
        {crypto ? "⏸ no ticks" : "⏸ closed"}
        {opens && ` · opens ${at(opens, now)} (${span(Date.parse(opens) - now)})`}
      </span>
    );
  }
  if (r.market_open && h?.closes_at) {
    const left = Date.parse(h.closes_at) - now;
    const weekend = h.closure === "weekend";
    if (left > 0 && left <= (weekend ? 24 : 2) * 3600_000) {
      return (
        <span
          className={`rounded px-1 font-medium ${left <= 15 * 60_000 ? "bg-bear/20 text-bear" : "bg-warn/15 text-warn"}`}
          title={`Closes ${at(h.closes_at, now)}${h.closure ? ` for the ${CLOSURE_WORD[h.closure]}` : ""}${h.opens_at ? `, reopens ${at(h.opens_at, now)}` : ""} - a trade opened now is carried through it.`}
        >
          {weekend ? "weekend close" : "closes"} {at(h.closes_at, now)} · {span(left)}
        </span>
      );
    }
  }
  return null;
}

/** Your position on this pair, and each timeframe's arrow judged against YOUR side: ✓ with you, ✗
 *  against. Red when most of 1H / 4H / 1D point the other way - the bigger picture has turned. */
function PositionTag({ p, r }: { p: PositionView; r: MonitorRow }) {
  const side = p.direction === "long" ? "up" : "down";
  const marks = TFS.map((tf) => {
    const t = r.tfs[tf]?.trend;
    return { tf, m: t === side ? "with" : t === "up" || t === "down" ? "against" : "flat" };
  });
  const big = marks.filter((x) => x.tf !== "15m");
  const against = big.filter((x) => x.m === "against").length;
  const withYou = big.filter((x) => x.m === "with").length;
  const turned = against > withYou;
  return (
    <div
      className={`mt-1.5 w-fit rounded px-1.5 py-1 text-[11px] ${
        turned ? "bg-bear/10 ring-1 ring-inset ring-bear/40" : "bg-neutral-800/80"
      }`}
      title={`Your ${p.direction.toUpperCase()} ${p.qty} lots: each timeframe's direction against it (✓ with you, ✗ against).${
        turned ? " Most of 1H / 4H / 1D now point the other way." : ""
      }`}
    >
      <div className="flex items-center gap-1.5 font-semibold tabular-nums">
        <span className={p.direction === "long" ? "text-bull" : "text-bear"}>
          {p.direction === "long" ? "▲ LONG" : "▼ SHORT"}
        </span>
        <span className="font-normal text-neutral-400">{p.qty}</span>
        <span className={p.unrealized_pnl >= 0 ? "text-bull" : "text-bear"}>{fmtUsd(p.unrealized_pnl, { sign: true })}</span>
      </div>
      <div className="mt-0.5 flex gap-1.5 whitespace-nowrap">
        {marks.map(({ tf, m }) => (
          <span key={tf} className={m === "with" ? "text-bull" : m === "against" ? "text-bear" : "text-neutral-500"}>
            {tf.toUpperCase()} {m === "with" ? "✓" : m === "against" ? "✗" : "–"}
          </span>
        ))}
      </div>
    </div>
  );
}

const hasHybridLine = (r: MonitorRow, h: MonitorHybrid | null) => !!h?.enabled && !!r.hybrid;

function Watch({ r, hybrid, now }: { r: MonitorRow; hybrid: MonitorHybrid | null; now: number }) {
  return (
    <>
      {hybrid && r.hybrid && hasHybridLine(r, hybrid) && <HybridLine v={r.hybrid} meta={hybrid} now={now} />}
      <Notes notes={r.notes} />
    </>
  );
}

/** The engine's rationale cut to its first sentence, without the mode prefix; the tooltip has it whole. */
function gist(reason: string | undefined, max = 110): string {
  let s = (reason ?? "").replace(/^Trend-only mode:\s*standing aside\s*[—-]\s*/i, "");
  const end = s.search(/\.\s/);
  if (end > 0) s = s.slice(0, end);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

const pct = (x: number | undefined) => (x == null ? "" : `${Math.round(x * 100)}%`);
const sideWord = (d: string | undefined) => (d === "long" ? "▲ long" : d === "short" ? "▼ short" : "");

/** What the Hybrid's last scan made of this pair - the answer to "why didn't it trade this?". */
function HybridLine({ v, meta, now }: { v: HybridVerdict; meta: MonitorHybrid; now: number }) {
  let text: string;
  let cls = "text-violet-300";
  switch (v.outcome) {
    case "candidate":
      text = `${sideWord(v.direction)} setup ${pct(v.confidence)} - clears the ${pct(v.bar)} bar`;
      cls = "text-bull";
      break;
    case "below_bar":
      text = `${sideWord(v.direction)} setup ${pct(v.confidence)} - under the ${pct(v.bar)} bar`;
      break;
    case "risk_veto":
      text = `${sideWord(v.direction)} setup ${pct(v.confidence)} - blocked by risk: ${v.risk ?? ""}`;
      cls = "text-warn";
      break;
    case "arm":
      text =
        `waits for a break - ${v.arm_side === "long" ? "buy" : "sell"} at ${fmtLevel(v.trigger ?? null)} ` +
        `(${pct(v.arm_confidence)})${meta.arming ? "" : " · arming is off, so it isn't placed"}`;
      break;
    case "aside":
      text = `no trade - ${gist(v.reason)}`;
      cls = "text-neutral-400";
      break;
    case "holding":
      text = "skipped - you have a position open";
      cls = "text-neutral-500";
      break;
    case "pending":
      text = "skipped - a proposal is waiting for your approval";
      cls = "text-neutral-500";
      break;
    case "armed":
      text = "skipped - a setup is already armed here";
      cls = "text-neutral-500";
      break;
    case "closed":
      text = "skipped - market closed";
      cls = "text-neutral-500";
      break;
    default:
      text = `couldn't analyse - ${gist(v.reason)}`;
      cls = "text-bear";
  }
  return (
    <div className={`mb-1 flex gap-1.5 text-xs leading-snug ${cls}`} title={v.reason || undefined}>
      <span className="shrink-0" aria-hidden="true">🤖</span>
      <span>
        <span className="font-semibold">Hybrid</span>
        {meta.scan_at && <span className="text-neutral-500"> ({ago(meta.scan_at, now)})</span>}: {text}
      </span>
    </div>
  );
}

/** One line over the table: is the Hybrid on, what did its last check conclude, is arming off. */
function HybridSummary({ h, now }: { h: MonitorHybrid; now: number }) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-violet-800/40 bg-violet-950/20 px-3 py-1.5 text-xs text-neutral-400">
      <span className="font-semibold text-violet-300">🤖 Hybrid {h.enabled ? "on" : "off"}</span>
      {h.enabled && h.last_at && (
        <span>
          · last check {ago(h.last_at, now)}
          {h.last_reason ? `: ${h.last_reason}` : ""}
        </span>
      )}
      {h.enabled && !h.arming && (
        <span
          className="text-warn"
          title="The engine finds wait-for-the-break setups but the Hybrid doesn't place them. Turn arming on in the Hybrid settings (Scanner) to let it."
        >
          · arming is off
        </span>
      )}
      {h.enabled && !h.scan_at && <span className="text-neutral-500">· its read of each pair shows after its next scan</span>}
    </div>
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

const STRUCT: Record<MonitorTf["structure"], { label: string; cls: string; words: string }> = {
  up: { label: "HH·HL", cls: "bg-bull/10 text-bull", words: "higher highs and higher lows" },
  down: { label: "LH·LL", cls: "bg-bear/10 text-bear", words: "lower highs and lower lows" },
  range: { label: "Range", cls: "bg-neutral-800 text-neutral-400", words: "no clean higher-high/low or lower-high/low sequence" },
};

function fmtLevel(v: number | null): string {
  return v == null ? "—" : fmtPrice(Number(v.toPrecision(6)));
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
  // Which side owns the ADX strength. Shown only when it DISAGREES with the arrow - agreeing is the
  // normal case and would just be one more symbol to read.
  // A side only counts with a clear gap between +DI and -DI, and only matters when there IS strength
  // (ADX 20+): a 0.9-point gap at ADX 12 painted a quarter of the table amber for nothing.
  const diSide =
    t.plus_di != null && t.minus_di != null && Math.abs(t.plus_di - t.minus_di) >= DI_MARGIN
      ? t.plus_di > t.minus_di ? "up" : "down"
      : null;
  const diConflict = diSide != null && (t.adx ?? 0) >= 20 && (up || down) && diSide !== t.trend;
  // Swing structure vs the arrow: ✓ when the swings confirm the EMA direction, ≠ when they contradict it.
  // Neither once the structure has just BROKEN (CHoCH): the label is then the old structure, and a ✓
  // would assert a confirmation price has already invalidated - the CHoCH tag carries the meaning.
  const st = STRUCT[t.structure] ?? STRUCT.range;
  const broken = t.choch_side != null;
  const structAgrees = !broken && t.structure !== "range" && t.structure === t.trend;
  const structFights = !broken && t.structure !== "range" && (up || down) && t.structure !== t.trend;
  const v = t.volume;
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
    t.adx != null
      ? `ADX ${t.adx.toFixed(1)}${t.adx_dir ? `, ${t.adx_dir}` : ""} - ${t.strength} trend` +
        (t.plus_di != null && t.minus_di != null
          ? ` | +DI ${t.plus_di.toFixed(1)} / -DI ${t.minus_di.toFixed(1)} (${diSide === "up" ? "buyers" : diSide === "down" ? "sellers" : "neither side"} own the strength)`
          : "")
      : null,
    `structure: ${st.words} | last swing high ${fmtLevel(t.swing_high)}, last swing low ${fmtLevel(t.swing_low)}` +
      (t.choch_side ? ` - ${t.choch_side === "up" ? "bullish" : "bearish"} change of character` : ""),
    t.rsi != null ? `RSI ${t.rsi.toFixed(1)}${t.rsi_prev != null ? ` (prev ${t.rsi_prev.toFixed(1)})` : ""}` : null,
    v
      ? `volume (last completed bar) ${v.ratio.toFixed(2)}x its 20-bar average${v.trend ? `, ${v.trend}` : ""}` +
        (v.up_share != null ? ` | ${Math.round(v.up_share * 100)}% of the last 20 bars' volume on up bars` : "")
      : "volume: not enough completed bars",
    t.price_vs_ema200 ? `price ${t.price_vs_ema200} the EMA 200` : null,
    t.state_headline ? `now: ${t.state_headline}` : null,
    insideKinds.length ? `inside a ${insideKinds.join(" and a ")} zone` : null,
    "Click to open this chart",
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <button
      onClick={onClick}
      title={title}
      className="w-full min-w-[9rem] rounded-md px-2 py-1 text-left transition hover:bg-neutral-800/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50"
    >
      {/* line 1: direction + trend strength (ADX) */}
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
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
        {t.adx != null && (
          <span
            className={`text-[11px] tabular-nums ${
              t.strength === "strong" ? "font-semibold text-neutral-200" : t.strength === "moderate" ? "text-neutral-300" : "text-neutral-500"
            }`}
          >
            {/* floored: the 20/25 thresholds are lower bounds, so 24.9 must not print as "25" */}
            ADX {Math.floor(t.adx)}
            {t.adx_dir === "rising" && <span className="ml-0.5 text-neutral-400" aria-label="strength rising">↗</span>}
            {t.adx_dir === "falling" && <span className="ml-0.5 text-neutral-600" aria-label="strength fading">↘</span>}
          </span>
        )}
        {diConflict && (
          <span
            className="rounded bg-warn/15 px-1 text-[10px] font-semibold text-warn"
            title={`+DI ${t.plus_di?.toFixed(1)} / -DI ${t.minus_di?.toFixed(1)}: the ADX strength belongs to the ${diSide === "up" ? "buyers" : "sellers"}, against the arrow`}
          >
            {diSide === "up" ? "+DI" : "−DI"} ≠
          </span>
        )}
      </div>
      {/* line 2: swing structure (+ does it agree with the arrow) and what the bar is doing */}
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span
          className={`rounded px-1.5 py-px text-[11px] font-semibold ${st.cls} ${broken ? "line-through opacity-50" : ""}`}
          title={broken ? "This structure has just been broken - see the CHoCH tag" : undefined}
        >
          {st.label}
          {structAgrees && <span className="ml-1 font-normal opacity-80" aria-label="agrees with the arrow">✓</span>}
          {structFights && <span className="ml-1 font-bold text-warn" aria-label="contradicts the arrow">≠</span>}
        </span>
        {t.choch_side && (
          <span
            className={`text-[11px] font-semibold ${t.choch_side === "up" ? "text-bull" : "text-bear"}`}
            title={`${t.choch_side === "up" ? "Bullish" : "Bearish"} change of character: closed ${t.choch_side === "up" ? "above the last swing high" : "below the last swing low"}`}
          >
            CHoCH {t.choch_side === "up" ? "▲" : "▼"}
          </span>
        )}
        {STATE_LABEL[t.state] && <span className="text-[11px] text-neutral-500">{STATE_LABEL[t.state]}</span>}
      </div>
      {/* line 3: momentum + volume */}
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <RsiPill t={t} />
        <VolumeTag v={v} />
      </div>
      {/* line 4: zone(s) */}
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

/** Volume status of the last COMPLETED bar: how big vs its average, building or fading, and which
 *  side the recent volume traded on. */
function VolumeTag({ v }: { v: MonitorTf["volume"] }) {
  if (!v) return <span className="text-[11px] text-neutral-600">Vol —</span>;
  const lvl =
    v.level === "high" ? "font-semibold text-neutral-100" : v.level === "low" ? "text-neutral-600" : "text-neutral-400";
  const share = v.up_share;
  const shareCls = share == null ? "" : share >= 0.6 ? "text-bull" : share <= 0.4 ? "text-bear" : "text-neutral-500";
  return (
    <span className="text-[11px] tabular-nums">
      <span className={lvl}>
        {/* two decimals, as the backend rounds: one decimal printed 0.69 and 0.72 both as "0.7x", one
            dimmed and one not */}
        Vol {v.ratio.toFixed(2)}×
        {v.trend === "expanding" && <span className="ml-0.5" aria-label="volume building">↑</span>}
        {v.trend === "fading" && <span className="ml-0.5 opacity-70" aria-label="volume fading">↓</span>}
      </span>
      {share != null && <span className={`ml-1 ${shareCls}`}>· {Math.round(share * 100)}% up</span>}
    </span>
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
      {item("ADX ↗ ↘", "Trend STRENGTH (not direction): 25+ strong (3 bars), 20-25 moderate (2), under 20 weak (1). ↗ strength building, ↘ fading. Amber +DI≠ / −DI≠ = with ADX 20+ and a clear gap (5+ points), the strength belongs to the other side than the arrow. Hover a cell for +DI / −DI.")}
      {item("HH·HL / LH·LL", "Swing structure: higher highs + higher lows (up), lower highs + lower lows (down), or Range. ✓ = it agrees with the arrow, amber ≠ = it contradicts it (the EMAs have turned before the swings, or the other way round). CHoCH ▲▼ = price just closed beyond the last opposite swing - the old structure is then struck through and gets no ✓/≠. Hover for the two swing prices.")}
      {item("RSI", "14-period. Green above 50, red below. Amber OB/OS = 70+ or 30- (stretched, either way). ↑↓ = turning since the last bar.")}
      {item("Vol", "The last COMPLETED bar's volume (a still-forming bar is ignored - its volume is partial) vs the SAME HOUR in about 20 earlier sessions on 1H/4H (the same 15-minute slot on 15M), so the US open is not a 'spike' every day; vs the last 20 sessions on 1D. Bold = 1.5x+, dim = 0.7x or less. ↑ building / ↓ fading. “62% up” = share of the last 20 bars' volume on up bars (green 60%+, red 40%-). MT5 tick volume - a proxy for real volume.")}
      {item("State", "What the latest bar is doing: pullback, breakout, rejection, range, impulse or reversal.")}
      {item("in / near zone", "Order blocks on that timeframe - the same zones as the chart's Order-blocks overlay. Cyan = demand, amber = supply. Near = within 0.5 ATR.")}
      {item("Bias", "How the three timeframes line up (1H, 4H, 1D - the 15M is not part of it). Dip in uptrend = 4H and 1D up, 1H down. Up vs the 1D = 1H and 4H up against the daily.")}
      {item("15M", "Entry timing: the same reading on 15-minute bars. It never changes the bias or the filters; it shows whether the short-term move is with the bigger picture right now.")}
      {item("% of day", "Today's range so far against a normal day's (daily ATR). Past 100% the day's move is already extended.")}
      {item("Your position", "A pair you hold shows your side, size and floating P&L, and each timeframe's arrow judged against you: ✓ with you, ✗ against. Red when most of 1H / 4H / 1D point the other way. “Mine” filters to them.")}
      {item("🤖 Hybrid", "What the Hybrid's last scan made of the pair, in the engine's words: a setup (and whether it clears your confidence bar), a level it waits for price to break, or why there is no trade. “Hybrid setups” filters to the pairs with an idea.")}
      {item("Hours", "Learned from the broker's own prices: amber when a pair closes within 2 hours (24 before the weekend), red inside 15 minutes; a closed pair says when it reopens. “No prices since …” = the symbol has stopped trading on your account.")}
      {item("Click", "A pair name opens it on the chart; a 15M / 1H / 4H / 1D cell opens that timeframe.")}
    </dl>
  );
}
