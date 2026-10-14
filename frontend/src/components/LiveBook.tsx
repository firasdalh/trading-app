import { useEffect, useRef, useState, type ReactNode } from "react";
import { displaySymbol, fmtPrice, fmtUsd } from "../format";
import type { PositionAdvice, PositionView } from "../types";

/**
 * The desk's right-hand sidebar building blocks: the live book you must always be able to see.
 *
 * Before this, "what is open?" and "what is waiting on me?" lived in a Book tab under the chart, so
 * answering either meant leaving the analysis you were reading. They now sit beside the chart,
 * permanently, and the full-detail panels (exit advice, the SL/TP table, the advisor log) open in a
 * drawer from here.
 */

// ---------------------------------------------------------------------------------------------
// Section shell — one header style for every sidebar block
// ---------------------------------------------------------------------------------------------

export interface SectionBadge {
  n: number;
  label?: string;
  /** warn = waiting on you; neutral = informational. */
  tone?: "warn" | "neutral" | "bull" | "bear";
  /** Show even at 0 (e.g. "0/5" slots); otherwise a zero badge is hidden as noise. */
  always?: boolean;
}

const BADGE_TONE: Record<NonNullable<SectionBadge["tone"]>, string> = {
  warn: "bg-warn text-black font-bold",
  neutral: "bg-neutral-800 text-neutral-300",
  bull: "bg-bull/15 text-bull",
  bear: "bg-bear/15 text-bear",
};

/**
 * Collapsible sidebar card with a small uppercase title and count badges.
 *
 * `autoOpen` expands it whenever it goes from empty to non-empty (something new arrived) and
 * collapses it when it empties, unless you have toggled it yourself — then your choice sticks until
 * the next empty -> non-empty change. Collapsing HIDES the body rather than unmounting it, because
 * the pending-approval panel inside plays the approval chime and must keep polling while folded.
 */
export function SideSection({
  title,
  badges = [],
  hint,
  actions,
  autoOpen,
  defaultOpen = true,
  body = "padded",
  empty,
  bodyClassName = "",
  children,
}: {
  title: string;
  badges?: SectionBadge[];
  hint?: string;
  actions?: ReactNode;
  /** When set, the section opens when this is true and folds when it is false (see above). */
  autoOpen?: boolean;
  defaultOpen?: boolean;
  /**
   * padded = plain content; bare = the content brings its own padding; flush = the children are
   * re-housed panels (`.card`), whose card chrome is stripped so they read as parts of this section.
   */
  body?: "padded" | "bare" | "flush";
  /** Muted word shown in the header when every badge is zero (e.g. "nothing"). */
  empty?: string;
  /** Extra classes for the body (e.g. to quiet a re-housed panel's own duplicate title). */
  bodyClassName?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(autoOpen ?? defaultOpen);
  const prevAuto = useRef(autoOpen);
  useEffect(() => {
    if (autoOpen === undefined || autoOpen === prevAuto.current) return;
    prevAuto.current = autoOpen;
    setOpen(autoOpen);
  }, [autoOpen]);

  return (
    <section className="rounded-xl border border-neutral-800/80 bg-neutral-900/50 shadow-card">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          title={hint}
          className="flex min-w-0 flex-1 items-center gap-2 rounded text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50"
        >
          <span className={`text-[10px] text-neutral-500 transition-transform ${open ? "rotate-90" : ""}`}>▶</span>
          <span className="truncate text-[11px] font-semibold uppercase tracking-wider text-neutral-400">{title}</span>
          {badges
            .filter((b) => b.n > 0 || b.always)
            .map((b, i) => (
              <span
                key={i}
                className={`shrink-0 rounded px-1.5 py-px text-[11px] tabular-nums ${BADGE_TONE[b.tone ?? "neutral"]}`}
              >
                {b.n}
                {b.label ? ` ${b.label}` : ""}
              </span>
            ))}
          {empty && badges.every((b) => b.n <= 0) && (
            <span className="text-[11px] text-neutral-600">{empty}</span>
          )}
        </button>
        {actions}
      </div>
      <div
        className={`${open ? "" : "hidden"} border-t border-neutral-800/80 ${
          body === "flush"
            ? "[&>.card]:rounded-none [&>.card]:border-0 [&>.card]:bg-transparent [&>.card]:px-3 [&>.card]:py-3 [&>.card]:shadow-none [&>.card+.card]:border-t [&>.card+.card]:border-neutral-800/80"
            : body === "padded"
              ? "p-3"
              : ""
        } ${bodyClassName}`}
      >
        {children}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------------------------
// Open positions
// ---------------------------------------------------------------------------------------------

// Dollar value of the position AT a price level, from the live P&L per price unit (which already
// folds in lot size, contract size and FX conversion). Same derivation as ChartPositionBar's.
export function usdAtLevel(p: PositionView, level: number | null | undefined): number | null {
  if (level == null || p.last_price == null || p.last_price === p.entry_price) return null;
  const perPrice = p.unrealized_pnl / (p.last_price - p.entry_price);
  return Number.isFinite(perPrice) ? (level - p.entry_price) * perPrice : null;
}

// ---------------------------------------------------------------------------------------------
// R — one basis everywhere: the risk taken at ENTRY
// ---------------------------------------------------------------------------------------------

/** A position as GET /positions/live sends it: plus the stop it was opened with (newer backends). */
type LivePosition = PositionView & { initial_stop_loss?: number | null };

/** How far a stop sits on the LOSING side of entry, in price; null at/through breakeven or absent. */
function lossSideDist(p: PositionView, stop: number | null | undefined): number | null {
  if (stop == null) return null;
  const d = p.direction === "long" ? p.entry_price - stop : stop - p.entry_price;
  return d > Math.abs(p.entry_price) * 1e-6 ? d : null;
}

/**
 * The stop the trade was OPENED with. `undefined` = the backend did not say (an older build);
 * `null` = the app has no record of the open (a trade placed directly in the terminal).
 */
export function entryStopOf(p: PositionView): number | null | undefined {
  return "initial_stop_loss" in p ? ((p as LivePosition).initial_stop_loss ?? null) : undefined;
}

/**
 * Profit in R, measured against the risk taken at ENTRY — never against the current stop, which
 * made a widened stop shrink the loss shown in R (EUR read −0.58R here while the advisor said
 * −1.21R). The advisor's own R comes first when it has one: it is the R its scale-out rules act on
 * ("bank a third at 1.5R"), and it is what the advisor drawer and the analysis panel print, so the
 * desk shows ONE number. (It measures with the plan's entry→stop distance; the entry stop below
 * uses the fill, which a pip of slippage moves by a few percent.) Without it: the entry stop from
 * the app's order record. For a trade the app has no record of (opened in the terminal), the
 * current stop is the only basis there is.
 */
export function positionR(p: PositionView, adviceR?: number | null): number | null {
  if (p.last_price == null) return null;
  const init = entryStopOf(p);
  if (init !== null && adviceR != null) return adviceR;
  const risk = lossSideDist(p, init ?? p.stop_loss);
  if (risk == null) return null;
  return (p.direction === "long" ? p.last_price - p.entry_price : p.entry_price - p.last_price) / risk;
}

/**
 * How many times further from entry the stop sits now than when the trade opened: above 1 the stop
 * was widened, i.e. the trade now risks more than it was sized for. null when the entry stop is
 * unknown or the stop is no longer on the losing side (trailed to breakeven or past it).
 */
export function stopWidening(p: PositionView): number | null {
  const init = entryStopOf(p);
  if (init == null) return null;
  const was = lossSideDist(p, init);
  const now = lossSideDist(p, p.stop_loss);
  return was != null && now != null ? now / was : null;
}

/**
 * The loss at the CURRENT stop against the per-trade risk cap (RISK.md). Returns the numbers only
 * when it is over the cap — a stop widened by hand can take a trade far past what it was sized
 * for, and nothing else on the card would say so. 5% slack so a trade sized AT the cap is not
 * flagged for a fill a tick worse or a slightly smaller equity since.
 */
export function overCapRisk(
  p: PositionView,
  equity: number | null | undefined,
  cap: number | null | undefined,
): { usd: number; pct: number } | null {
  if (!equity || equity <= 0 || !cap || cap <= 0) return null;
  const at = usdAtLevel(p, p.stop_loss);
  if (at == null || at >= 0) return null;
  const pct = -at / equity;
  return pct > cap * 1.05 ? { usd: -at, pct } : null;
}

/** "×2.2" — how much wider the stop is than at entry. */
export const widenText = (k: number) => `×${k.toFixed(k >= 10 ? 0 : 1)}`;
// "3%", "2.5%" — the cap as the user set it, without a trailing ".0".
const pctText = (v: number) => `${Number((v * 100).toFixed(1))}%`;

/** Small red chip: "risk $78 · 6.7% > 3% cap". */
export function OverCapChip({ over, cap, compact = false }: { over: { usd: number; pct: number }; cap: number; compact?: boolean }) {
  return (
    <span
      className="shrink-0 rounded bg-bear/20 px-1.5 py-px text-[10px] font-semibold text-bear ring-1 ring-inset ring-bear/40"
      title={`If the stop is hit this trade loses ${fmtUsd(over.usd)} — ${(over.pct * 100).toFixed(1)}% of equity, above the ${(cap * 100).toFixed(1)}% per-trade cap. Usually a stop moved further away after entry.`}
    >
      {compact ? "" : `risk ${fmtUsd(over.usd)} · `}
      {(over.pct * 100).toFixed(1)}% &gt; {pctText(cap)} cap
    </span>
  );
}

/** Small amber chip: "stop widened ×2.2". */
export function WidenedChip({ k }: { k: number }) {
  return (
    <span
      className="shrink-0 rounded bg-warn/15 px-1.5 py-px text-[10px] font-semibold text-warn ring-1 ring-inset ring-warn/30"
      title={`The stop is ${widenText(k)} as far from entry as when the trade opened — it now risks ${widenText(k)} what it was sized for. R is still measured against the risk at entry.`}
    >
      stop widened {widenText(k)}
    </span>
  );
}

const ADVICE_TONE: Record<PositionAdvice["severity"], { label: string; cls: string }> = {
  danger: { label: "Act now", cls: "bg-bear/20 text-bear ring-1 ring-inset ring-bear/40" },
  warn: { label: "Decide", cls: "bg-warn/15 text-warn ring-1 ring-inset ring-warn/30" },
  info: { label: "On plan", cls: "bg-neutral-800 text-neutral-300" },
};

const THESIS_TEXT: Record<PositionAdvice["thesis"], string> = {
  intact: "on track",
  weakening: "losing steam",
  invalidated: "trend flipped",
  unknown: "",
};

export function OpenPositionsList({
  positions,
  advice,
  activeSymbol,
  equity,
  riskCap,
  onChart,
  onClose,
  onAdvice,
}: {
  positions: PositionView[] | null;
  /** The advisor's latest read per position (may be stale by one poll; null = not loaded). */
  advice: PositionAdvice[] | null;
  activeSymbol: string;
  /** Account equity and the per-trade risk cap (fraction), to flag a trade risking more than the cap. */
  equity?: number | null;
  riskCap?: number | null;
  onChart: (p: PositionView) => void;
  onClose: (p: PositionView) => Promise<void> | void;
  onAdvice: (p: PositionView) => void;
}) {
  if (positions == null) {
    return <div className="px-3 py-3 text-xs text-neutral-500">Loading positions…</div>;
  }
  if (positions.length === 0) {
    return (
      <div className="px-3 py-4 text-center text-xs leading-relaxed text-neutral-500">
        No open positions.
        <br />
        Run an analysis or approve a setup to open one.
      </div>
    );
  }
  return (
    <ul className="divide-y divide-neutral-800/80">
      {positions.map((p) => (
        <PositionCard
          key={p.id}
          p={p}
          advice={
            advice?.find(
              (a) => a.symbol.toUpperCase() === p.symbol.toUpperCase() && a.direction === p.direction,
            ) ?? null
          }
          active={p.symbol.toUpperCase() === activeSymbol.toUpperCase()}
          equity={equity}
          riskCap={riskCap}
          onChart={onChart}
          onClose={onClose}
          onAdvice={onAdvice}
        />
      ))}
    </ul>
  );
}

function PositionCard({
  p,
  advice,
  active,
  equity,
  riskCap,
  onChart,
  onClose,
  onAdvice,
}: {
  p: PositionView;
  advice: PositionAdvice | null;
  active: boolean;
  equity?: number | null;
  riskCap?: number | null;
  onChart: (p: PositionView) => void;
  onClose: (p: PositionView) => Promise<void> | void;
  onAdvice: (p: PositionView) => void;
}) {
  const isLong = p.direction === "long";
  const last = p.last_price;
  const sl = p.stop_loss;
  const tp = p.take_profit;

  // R so far, against the risk taken at ENTRY (see positionR). Without a record of the entry stop it
  // only exists while the current stop is on the losing side; once that has been trailed to
  // breakeven or past it, the honest number is what the stop now locks in, in dollars.
  const riskDist = sl != null ? (isLong ? p.entry_price - sl : sl - p.entry_price) : null;
  const r = positionR(p, advice?.r_multiple);
  const locked = riskDist != null && riskDist <= Math.abs(p.entry_price) * 1e-6 ? usdAtLevel(p, sl) : null;
  const widened = stopWidening(p);
  const over = overCapRisk(p, equity, riskCap);
  const tone = advice ? ADVICE_TONE[advice.severity] : null;

  return (
    <li className={`relative px-3 py-2.5 transition ${active ? "bg-brand-500/[0.07]" : ""}`}>
      {/* The charted position gets an accent rail, so the sidebar and the chart visibly agree. */}
      {active && <span className="absolute inset-y-0 left-0 w-0.5 bg-brand-500" aria-hidden="true" />}
      <div className="flex items-baseline gap-2">
        <span
          className={`rounded px-1.5 py-px text-[10px] font-bold ${isLong ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear"}`}
        >
          {isLong ? "▲ LONG" : "▼ SHORT"}
        </span>
        <button
          onClick={() => onChart(p)}
          className="truncate font-semibold text-neutral-100 hover:text-brand-400"
          title={`Show ${displaySymbol(p.symbol)} on the chart`}
        >
          {displaySymbol(p.symbol)}
        </button>
        <span className="text-xs tabular-nums text-neutral-500" title="Size in lots">{p.qty}</span>
        <span
          className={`ml-auto text-sm font-semibold tabular-nums ${
            p.unrealized_pnl > 0 ? "text-bull" : p.unrealized_pnl < 0 ? "text-bear" : "text-neutral-300"
          }`}
          title="Floating P&L"
        >
          {fmtUsd(p.unrealized_pnl, { sign: true })}
        </span>
      </div>

      <div className="mt-1 flex items-center gap-2 text-[11px] tabular-nums text-neutral-500">
        <span title="Entry → last price">
          {fmtPrice(p.entry_price)} <span className="text-neutral-600">→</span>{" "}
          <span className="text-neutral-300">{fmtPrice(last)}</span>
        </span>
        {r != null && (
          <span
            className={r >= 0 ? "text-bull" : "text-bear"}
            title="Profit in R: multiples of the risk taken at entry (entry to the stop the trade opened with) — the same R the advisor quotes"
          >
            {r >= 0 ? "+" : ""}
            {r.toFixed(2)}R
          </span>
        )}
        {locked != null && (
          <span className={locked >= 0 ? "text-bull" : "text-neutral-400"} title="What the stop locks in if it is hit">
            stop locks {fmtUsd(locked, { sign: true })}
          </span>
        )}
        {tone && advice && (
          <button
            onClick={() => onAdvice(p)}
            className={`ml-auto shrink-0 rounded px-1.5 py-px text-[10px] font-semibold ${tone.cls}`}
            title={`${advice.headline}${advice.detail ? ` — ${advice.detail}` : ""}`}
          >
            {tone.label}
            {THESIS_TEXT[advice.thesis] ? ` · ${THESIS_TEXT[advice.thesis]}` : ""}
          </button>
        )}
      </div>

      {/* Risk the trade carries NOW vs what it was sized for. Both only appear when something is
          off, so a normal card stays two lines. */}
      {((widened != null && widened > 1.05) || over) && (
        <div className="mt-1 flex flex-wrap items-center gap-1">
          {over && riskCap != null && <OverCapChip over={over} cap={riskCap} />}
          {widened != null && widened > 1.05 && <WidenedChip k={widened} />}
        </div>
      )}

      <TradeRail entry={p.entry_price} sl={sl} tp={tp} last={last} />

      <div className="mt-2 flex items-center gap-1.5">
        <button
          onClick={() => onChart(p)}
          className={`btn px-2 py-1 text-xs ${active ? "bg-brand-600/80 text-white hover:bg-brand-500" : "btn-subtle"}`}
          title={`Show ${displaySymbol(p.symbol)} on the chart`}
        >
          {active ? "On chart" : "Chart"}
        </button>
        <button
          onClick={() => onAdvice(p)}
          className="btn btn-subtle px-2 py-1 text-xs"
          title="Exit advice, edit SL/TP, and the advisor's recent actions"
        >
          Advice
        </button>
        <CloseButton p={p} onClose={onClose} />
      </div>
    </li>
  );
}

/**
 * A slim stop -> target rail with the entry tick and a marker at the current price — the same
 * red/green reading the chart's zones give, small enough to scan three trades at a glance.
 * Left is always the stop and right always the target, for longs and shorts alike, so "marker moving
 * right" always means "going your way".
 */
function TradeRail({ entry, sl, tp, last }: { entry: number; sl: number | null; tp: number | null; last: number | null }) {
  if (sl == null || tp == null || sl === tp) {
    return (
      <div className="mt-1.5 rounded bg-warn/10 px-2 py-1 text-[11px] text-warn">
        {sl == null && tp == null
          ? "⚠ No stop and no target on this position"
          : sl == null
            ? `⚠ No stop · TP ${fmtPrice(tp)}`
            : `SL ${fmtPrice(sl)} · no target`}
      </div>
    );
  }
  const frac = (x: number) => Math.min(1, Math.max(0, (x - sl) / (tp - sl)));
  const e = frac(entry) * 100;
  const now = last != null ? frac(last) * 100 : null;
  return (
    <div className="mt-2">
      <div className="relative h-1.5 rounded-full">
        <div className="absolute inset-y-0 left-0 rounded-l-full bg-bear/40" style={{ width: `${e}%` }} />
        <div className="absolute inset-y-0 right-0 rounded-r-full bg-bull/40" style={{ left: `${e}%` }} />
        <div className="absolute -inset-y-0.5 w-px bg-neutral-300/70" style={{ left: `${e}%` }} title={`Entry ${fmtPrice(entry)}`} />
        {now != null && (
          <div
            className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-neutral-950 bg-white shadow"
            style={{ left: `${now}%` }}
            title={`Now ${fmtPrice(last)}`}
          />
        )}
      </div>
      <div className="mt-1 flex justify-between text-[10px] tabular-nums">
        <span className="text-bear">SL {fmtPrice(sl)}</span>
        <span className="text-bull">TP {fmtPrice(tp)}</span>
      </div>
    </div>
  );
}

// Same two-step close as the positions table and the chart strip: the first click arms, the second
// closes, and an armed button disarms itself after 5s so a stray click cannot be confirmed later.
function CloseButton({ p, onClose }: { p: PositionView; onClose: (p: PositionView) => Promise<void> | void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 5000);
    return () => clearTimeout(t);
  }, [confirming]);

  if (!confirming) {
    return (
      <button
        onClick={() => setConfirming(true)}
        className="btn ml-auto border border-bear/50 bg-bear/10 px-2 py-1 text-xs text-bear hover:bg-bear/20"
        title={`Close ${p.symbol} ${p.direction.toUpperCase()} — asks to confirm`}
      >
        Close
      </button>
    );
  }
  return (
    <span className="ml-auto flex items-center gap-1.5">
      <button
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await onClose(p);
            setConfirming(false);
          } finally {
            setBusy(false);
          }
        }}
        className="btn bg-bear px-2 py-1 text-xs text-white hover:bg-red-700"
        title={`Confirm closing ${p.symbol} ${p.direction.toUpperCase()}`}
      >
        {busy ? "Closing…" : "Confirm"}
      </button>
      <button
        disabled={busy}
        onClick={() => setConfirming(false)}
        className="btn btn-subtle px-2 py-1 text-xs"
      >
        Cancel
      </button>
    </span>
  );
}
