import type { MacroContext, MacroEvent } from "../types";

/**
 * MACRO & NEWS - the Forex Factory context layer.
 *
 * This panel shows timing risk and crowd positioning, and it is built so that neither can be
 * misread as a trade signal. There is no green/red "bullish/bearish" anywhere in it: a scheduled
 * release is a volatility event whose direction nobody knows, and a crowded book is fragility, not
 * a fade. The strongest thing it will ever say is WAIT.
 */
export function MacroPanel({ macro }: { macro: MacroContext | null | undefined }) {
  if (!macro || !macro.enabled) return null;
  const n = macro.news;
  const p = macro.positioning;

  const tone =
    macro.action === "wait" ? "border-warn/50 bg-warn/10"
      : macro.action === "warn" ? "border-neutral-700 bg-neutral-800/40"
        : "border-neutral-800";

  return (
    <div className={`rounded-md border p-3 ${tone}`}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
          Macro &amp; news
        </span>
        <span className="flex items-center gap-2 text-[11px]">
          {macro.source_mode === "cached" && (
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-neutral-400"
                  title="The live feed was unreadable; this is the stored calendar.">
              cached
            </span>
          )}
          {!n.available && (
            <span className="rounded bg-bear/20 px-1.5 py-0.5 font-semibold text-bear">
              NEWS DATA UNAVAILABLE
            </span>
          )}
          {n.stale && n.available && (
            <span className="rounded bg-warn/20 px-1.5 py-0.5 text-warn">stale</span>
          )}
        </span>
      </div>

      {/* The verdict. Three words only, and none of them is a direction. */}
      <div className="flex flex-wrap items-center gap-2">
        <Badge label={macro.action.toUpperCase()} band={macro.action} />
        <span className="text-sm text-neutral-300">
          News risk <strong className="tabular-nums">{n.score}</strong>/100
          <span className="ml-1 text-neutral-500">({n.band})</span>
        </span>
        {n.both_sides && (
          <span className="rounded bg-warn/15 px-1.5 py-0.5 text-[11px] text-warn"
                title="Both legs of this pair have events inside the warning window.">
            both legs exposed
          </span>
        )}
      </div>
      {macro.gate_reason && (
        <p className="prose-measure mt-1.5 text-xs leading-relaxed text-neutral-400">
          {macro.gate_reason}
        </p>
      )}

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <EventCard title="Next high impact" ev={n.next_high} />
        <EventCard title="Next medium impact" ev={n.next_medium} />
      </div>

      {n.recent.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Just released
          </div>
          <ul className="space-y-1">
            {n.recent.slice(0, 3).map((e) => (
              <li key={`${e.currency}-${e.title}-${e.when}`}
                  className="flex items-baseline justify-between gap-2 text-xs">
                <span className="text-neutral-200">{e.currency} {e.title}</span>
                <span className="tabular-nums text-neutral-500">
                  {Math.abs(e.minutes_away).toFixed(0)}m ago
                  {e.actual != null && <> · actual {e.actual}</>}
                  {e.forecast != null && <> vs fc {e.forecast}</>}
                </span>
              </li>
            ))}
          </ul>
          <p className="prose-measure mt-1 text-[11px] text-neutral-500">
            The first move after a release is not reliable information about the fundamentals - it
            is the spike and the traders caught in it.
          </p>
        </div>
      )}

      <Positioning p={p} symbol={macro.symbol} />

      <p className="prose-measure mt-3 text-[11px] leading-relaxed text-neutral-500">
        {macro.news_articles_note}
      </p>
    </div>
  );
}


function Badge({ label, band }: { label: string; band: string }) {
  const cls =
    band === "wait" ? "bg-warn/20 text-warn"
      : band === "warn" ? "bg-neutral-700/60 text-neutral-200"
        : "bg-bull/15 text-bull";
  return (
    <span className={`rounded px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ${cls}`}>
      {label}
    </span>
  );
}


function EventCard({ title, ev }: { title: string; ev: MacroEvent | null }) {
  return (
    <div className="rounded bg-neutral-900/50 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">{title}</div>
      {ev == null ? (
        <div className="text-sm text-neutral-500">none in range</div>
      ) : (
        <>
          <div className="truncate text-sm font-semibold text-neutral-100" title={ev.title}>
            {ev.currency} {ev.title}
          </div>
          <div className="text-[11px] tabular-nums text-neutral-400">
            {fmtAway(ev.minutes_away)} · {ev.impact}
            {ev.forecast != null && <> · fc {ev.forecast}</>}
            {ev.previous != null && <> · prev {ev.previous}</>}
          </div>
        </>
      )}
    </div>
  );
}


function fmtAway(minutes: number): string {
  if (minutes < 0) return `${Math.abs(minutes).toFixed(0)}m ago`;
  if (minutes < 90) return `in ${minutes.toFixed(0)}m`;
  const h = minutes / 60;
  return h < 48 ? `in ${h.toFixed(1)}h` : `in ${(h / 24).toFixed(1)}d`;
}


function Positioning({ p, symbol }: { p: MacroContext["positioning"]; symbol: string }) {
  return (
    <div className="mt-3 border-t border-neutral-800 pt-3">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
          {symbol} positioning
        </span>
        {p.available ? (
          <span className="flex items-center gap-1.5 text-[11px]">
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-neutral-300">
              crowding {p.crowding_score}/100 · {p.crowding_band}
            </span>
            {p.stale && <span className="rounded bg-warn/20 px-1.5 py-0.5 text-warn">STALE</span>}
          </span>
        ) : (
          <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[11px] text-neutral-400">
            UNAVAILABLE
          </span>
        )}
      </div>

      {p.available ? (
        <>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <Row label="Long traders" v={p.long_traders_pct} />
            <Row label="Long lots" v={p.long_lots_pct} />
            <Row label="Short traders" v={p.short_traders_pct} />
            <Row label="Short lots" v={p.short_lots_pct} />
            <Row label="Trader imbalance" v={p.trader_imbalance} signed />
            <Row label="Lot imbalance" v={p.lot_imbalance} signed />
          </div>
          <div className="mt-1.5 text-[11px] text-neutral-400">
            {p.bias_label}
            {p.percentile != null && <> · {p.percentile}th percentile for this pair</>}
            {p.trend !== "unknown" && <> · {p.trend}</>}
            {p.samples > 0 && <> · {p.samples} snapshots</>}
          </div>
        </>
      ) : (
        <p className="prose-measure text-[11px] leading-relaxed text-neutral-500">{p.note}</p>
      )}

      {/* The label the spec asks for, and the reason the whole block is deliberately colourless. */}
      <p className="mt-2 rounded bg-neutral-900/60 px-2 py-1 text-[11px] text-neutral-400">
        {p.disclaimer} A crowded book can stay crowded for months; it is a measure of fragility, not
        a reason to take the other side.
      </p>
    </div>
  );
}


function Row({ label, v, signed }: { label: string; v: number | null; signed?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-neutral-500">{label}</span>
      <span className="tabular-nums text-neutral-200">
        {v == null ? "—" : `${signed && v > 0 ? "+" : ""}${v.toFixed(0)}%`}
      </span>
    </div>
  );
}
