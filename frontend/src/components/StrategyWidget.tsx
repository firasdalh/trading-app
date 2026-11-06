import { useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { AssetClass, PositionView, StrategyCheck, StrategyCheckCondition, StrategyCheckSet, StrategyView } from "../types";
import { draftOf, LeanStrip, pairWait, targetDone, zoneLabel } from "./StrategyBoard";

/**
 * STRATEGIES AT A GLANCE: the Strategies board's live read, where the chart is - scalp mode's sidebar
 * and the full-screen chart's pop-up - so watching a strategy work doesn't mean leaving the chart.
 *
 * Per strategy: running or not, and today's result from the journal (so the trades its stop or target
 * closed count too). Per pair of a running one: the trade it holds, with its live P&L and how much of
 * its close rules hold - or, for BUY and SELL, how many of each group's entry conditions hold now, a line per
 * group and a chip per condition. The reads are the board's own read-only check; nothing here edits a strategy, and
 * Start / Stop is the board's switch.
 */

const CHECK_MS = 5000;
const MAX_CHECKS = 8;    // pairs read per round, across the running strategies; more take turns

// Chip names (the board's NAMES are menu entries, too long for a chip).
const SHORT: Record<string, string> = {
  ema_cross: "EMA×", ema_above: "EMA>", price_ema: "vs EMA", trend: "Trend", tf_align: "TFs", supertrend: "ST", adx: "ADX",
  rsi_rising: "RSI↗", rsi_level: "RSI", macd: "MACD", stoch_cross: "Stoch×", stoch_room: "Stoch", vwap: "VWAP", volume: "Vol",
  bb_break: "BB", candles: "Candles", clean_candle: "Bar", structure: "HH/HL", sr_room: "S/R", zone_room: "Zone", atr: "ATR", spread: "Spread", session: "Session",
  rsi_extreme: "RSI TP", profit_r: "+R", time_stop: "Time",
};

const sameSym = (a: string, b: string) => a.toUpperCase() === b.toUpperCase();

function Chip({ c }: { c: StrategyCheckCondition }) {
  // A Timeframes rule's chip holds the strip's colours, so its own ✓ / ✗ colour is a border, not a fill.
  const tone = c.leans
    ? (c.ok === true ? "border border-bull/60 text-bull" : c.ok === false ? "border border-bear/50 text-bear" : "border border-neutral-700 text-neutral-500")
    : (c.ok === true ? "bg-bull/20 text-bull" : c.ok === false ? "bg-bear/15 text-bear" : "bg-neutral-800 text-neutral-500");
  return (
    <span
      className={`rounded px-1 text-[10px] leading-4 ${c.leans ? "inline-flex flex-wrap items-center gap-0.5 py-px" : "whitespace-nowrap"} ${tone}`}
      title={`${c.label}\nnow: ${c.detail}`}
    >
      {c.ok === true ? "✓" : c.ok === false ? "✗" : "…"} {SHORT[c.type] ?? c.type}{" "}
      {c.leans ? <LeanStrip leans={c.leans} compact /> : <span className="opacity-60">{c.tf}</span>}
    </span>
  );
}

// Every group of a side, a line each: any one group is enough. A group shows "GO" when all its conditions
// hold - unless it is switched off on the board (dimmed: read, but it opens nothing), or `paused`: outside the
// strategy's trading hours, while the pair waits out its cooldown, or once the strategy reached its daily target.
function SideRow({ label, tone, set, paused }: { label: string; tone: string; set: StrategyCheckSet | null | undefined; paused?: boolean }) {
  if (!set?.groups.length) return null;
  return (
    <>
      {set.groups.map((g, gi) => {
        const n = g.conditions.filter((c) => c.ok === true).length;
        const of = g.conditions.length;
        const pct = of ? n / of : 0;
        const go = g.ok && !paused && !g.off;
        return (
          <div key={gi} className="mt-1 flex items-start gap-1.5">
            <span
              className={`mt-px w-11 shrink-0 text-[10px] font-bold ${gi ? "pl-2 text-amber-300/80" : tone}`}
              title={gi ? "Or this group - any one group is enough" : undefined}
            >
              {gi ? "or" : label}
            </span>
            {g.off ? (
              <span className="mt-px w-[5.25rem] shrink-0 text-[10px] text-neutral-500" title="Switched off on the board: read here, but it opens nothing">
                ○ off
              </span>
            ) : (
              <span className="mt-1 flex shrink-0 items-center gap-1">
                <span className="relative h-1.5 w-12 overflow-hidden rounded-full bg-neutral-800">
                  <span
                    className={`absolute inset-y-0 left-0 ${go ? "bg-bull" : pct >= 0.75 ? "bg-amber-400" : "bg-neutral-500"}`}
                    style={{ width: `${pct * 100}%` }}
                  />
                </span>
                <span className={`w-8 text-[10px] tabular-nums ${go ? "font-bold text-bull" : "text-neutral-400"}`}>
                  {go ? "GO" : `${n}/${of}`}
                </span>
              </span>
            )}
            <span className={`flex min-w-0 flex-wrap gap-0.5 ${g.off ? "opacity-40" : ""}`}>{g.conditions.map((c, i) => <Chip key={i} c={c} />)}</span>
          </div>
        );
      })}
    </>
  );
}

export function StrategyWidget({ positions, chartSymbol, onPick, onOpenBoard }: {
  positions: PositionView[] | null;
  chartSymbol?: string;
  onPick?: (symbol: string, assetClass: AssetClass) => void;
  onOpenBoard?: () => void;
}) {
  const [bump, setBump] = useState(0);
  const [busy, setBusy] = useState<number | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const { data: list, error } = usePolling(() => api.strategies(), CHECK_MS, [bump]);
  const { data: review } = usePolling(() => api.journalReview(), 20000);

  const running = (list ?? []).filter((s) => s.enabled);
  const reads = running.flatMap((s) => s.pairs.map((p) => ({ s, p })));
  // Re-armed only when a running strategy's RULES change, not on every refresh of its notes.
  const readsKey = JSON.stringify(reads.map(({ s, p }) => [s.id, p.symbol, draftOf(s)]));
  // More pairs than a round reads take turns, MAX_CHECKS a round, each keeping its last read in between: the
  // 9th pair on used to show "reading the rules…" forever (FR40m, 2026-11-04). New rules start the reads afresh.
  const turn = useRef(0);
  const kept = useRef<{ key: string; reads: Record<string, StrategyCheck | null> }>({ key: "", reads: {} });
  const { data: checks } = usePolling(async () => {
    if (kept.current.key !== readsKey) kept.current = { key: readsKey, reads: {} };
    const batch = reads.length <= MAX_CHECKS
      ? reads
      : Array.from({ length: MAX_CHECKS }, (_, i) => reads[(turn.current + i) % reads.length]);
    turn.current = reads.length ? (turn.current + batch.length) % reads.length : 0;
    const out: Record<string, StrategyCheck | null> = { ...kept.current.reads };
    await Promise.all(batch.map(async ({ s, p }) => {
      try {
        out[`${s.id}|${p.symbol}`] = await api.checkStrategy(draftOf(s), p.symbol, p.asset_class);
      } catch {
        out[`${s.id}|${p.symbol}`] = null;
      }
    }));
    if (kept.current.key === readsKey) kept.current.reads = out;
    return out;
  }, CHECK_MS, [readsKey]);

  const run = async (s: StrategyView, on: boolean) => {
    setBusy(s.id);
    setMsg(null);
    try {
      await api.runStrategy(s.id, on);
      setBump((b) => b + 1);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  if (error && !list) return <div className="px-3 py-2 text-xs text-amber-300">Can't load the strategies: {error}</div>;
  if (!list) return <div className="px-3 py-2 text-xs text-neutral-500">Loading the strategies…</div>;
  if (!list.length) {
    return (
      <div className="px-3 py-2.5 text-xs text-neutral-500">
        No strategy yet.{" "}
        {onOpenBoard && <button onClick={onOpenBoard} className="text-amber-300 underline">Build one on the board ›</button>}
      </div>
    );
  }

  return (
    <div className="space-y-2 p-2 text-xs">
      {[...list].sort((a, b) => Number(b.enabled) - Number(a.enabled)).map((s) => {
        const src = `strategy:${s.id}`;
        const closed = (review?.trades ?? []).filter((t) => t.source === src);
        const net = closed.reduce((a, t) => a + t.pnl, 0);
        const wins = closed.filter((t) => t.pnl > 0).length;
        const mine = (positions ?? []).filter((p) => p.source === src);
        const offHours = !!s.hours && s.hours_now?.open === false;
        const done = targetDone(s);
        return (
          <div
            key={s.id}
            className={`rounded-lg border p-2 ${s.enabled ? "border-bull/40 bg-bull/[0.04]" : "border-neutral-800 bg-neutral-900/40 opacity-80"}`}
          >
            <div className="flex items-center gap-1.5">
              <span className={`h-2 w-2 shrink-0 rounded-full ${s.enabled ? "animate-pulse bg-bull" : "bg-neutral-600"}`} />
              <span className="truncate font-semibold text-neutral-100" title={s.name}>{s.name}</span>
              <span className="shrink-0 rounded bg-neutral-800 px-1 text-[10px] text-sky-300" title="Base timeframe">{s.timeframe}</span>
              {s.live && (
                <span className="shrink-0 rounded bg-bear/25 px-1 text-[10px] font-bold text-bear" title="It trades a real account too: real money, no approval per order (while LIVE trading is confirmed)">
                  LIVE $
                </span>
              )}
              {s.sides !== "both" && (
                <span className="shrink-0 text-[10px] text-neutral-500">{s.sides === "long" ? "buy only" : "sell only"}</span>
              )}
              {s.enabled && s.hours && (
                <span
                  className={`shrink-0 rounded px-1 text-[10px] ${s.hours_now?.open ? "bg-bull/15 text-bull" : "bg-neutral-800 text-neutral-400"}`}
                  title={`Trading hours ${s.hours.start}–${s.hours.end} (${zoneLabel(s.hours.tz)}): it opens trades only then. A trade it holds keeps its close rules, stop and target at any hour.`}
                >
                  ⏰ {s.hours_now ? (s.hours_now.open ? `on until ${s.hours.end}` : `off until ${s.hours.start}`) : `${s.hours.start}–${s.hours.end}`}
                </span>
              )}
              {s.enabled && s.daily_target != null && (
                <span
                  className={`shrink-0 rounded px-1 text-[10px] tabular-nums ${done ? "bg-bull/20 font-semibold text-bull" : "bg-neutral-800 text-neutral-400"}`}
                  title={done
                    ? `Its trades today reached the daily target of +${fmtUsd(s.daily_target)} at ${done.at}: no new trades until ${done.resumes} (00:00 UTC). The trades it holds keep their close rules, stop and target.`
                    : `Daily target: once the trades it closed today make +${fmtUsd(s.daily_target)}, it opens nothing more until 00:00 UTC.`}
                >
                  🎯 {done ? `target hit · back ${done.resumes}` : `+${fmtUsd(s.daily_target)}`}
                </span>
              )}
              <button
                onClick={() => void run(s, !s.enabled)}
                disabled={busy === s.id}
                className={`ml-auto shrink-0 rounded border px-1.5 py-px text-[10px] font-semibold disabled:opacity-50 ${
                  s.enabled ? "border-bear/50 text-bear hover:bg-bear/15" : "border-bull/50 text-bull hover:bg-bull/15"
                }`}
                title={s.enabled
                  ? "Stop it. A trade it holds stays on its stop and target."
                  : "Start it - it trades a demo / paper account only, through the Risk Manager"}
              >
                {busy === s.id ? "…" : s.enabled ? "■ Stop" : "▶ Start"}
              </button>
            </div>
            <div
              className="mt-0.5 flex flex-wrap gap-x-2 text-[10px] text-neutral-500"
              title="Today (UTC): the trades it closed, from the journal - stops and targets included"
            >
              <span>today {closed.length} closed{mine.length ? ` · ${mine.length} open` : ""}</span>
              {closed.length > 0 && (
                <>
                  <span><span className="text-bull">{wins}W</span> <span className="text-bear">{closed.length - wins}L</span></span>
                  <span className={`font-semibold tabular-nums ${net >= 0 ? "text-bull" : "text-bear"}`}>{fmtUsd(net, { sign: true })}</span>
                </>
              )}
              {!s.enabled && <span className="text-neutral-600">stopped</span>}
            </div>

            {s.enabled && s.pairs.map((p) => {
              const check = checks?.[`${s.id}|${p.symbol}`];
              const trade = mine.find((x) => sameSym(x.symbol, p.symbol));
              const note = s.state?.[p.symbol]?.note;
              const wait = trade ? null : pairWait(s, p.symbol);
              const charted = !!chartSymbol && sameSym(chartSymbol, p.symbol);
              return (
                <div
                  key={p.symbol}
                  className={`mt-1.5 rounded border px-2 py-1 ${charted ? "border-amber-500/50 bg-amber-500/[0.04]" : "border-neutral-800 bg-neutral-950/40"}`}
                >
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => onPick?.(p.symbol, p.asset_class)}
                      className="shrink-0 font-semibold text-neutral-100 hover:text-amber-200"
                      title={charted ? "On the chart" : "Chart this pair"}
                    >
                      {displaySymbol(p.symbol)}
                    </button>
                    {trade ? (
                      <span className={`font-semibold tabular-nums ${trade.unrealized_pnl >= 0 ? "text-bull" : "text-bear"}`}>
                        {trade.direction === "long" ? "▲ LONG" : "▼ SHORT"} {trade.qty} · {fmtUsd(trade.unrealized_pnl, { sign: true })}
                      </span>
                    ) : wait ? (
                      <span
                        className="shrink-0 rounded bg-amber-500/10 px-1 text-[10px] tabular-nums text-amber-200"
                        title={`After a trade on this pair closes it waits ${s.cooldown_minutes} min before it opens another - free again at ${wait.until}.`}
                      >
                        ⏳ waits {wait.mins} min · until {wait.until}
                      </span>
                    ) : (
                      note && <span className="truncate text-[10px] text-neutral-500" title={note}>{note}</span>
                    )}
                  </div>
                  {check === undefined ? (
                    <div className="text-[10px] text-neutral-600">reading the rules…</div>
                  ) : check === null ? (
                    <div className="text-[10px] text-amber-300/80">can't read this pair right now</div>
                  ) : trade ? (
                    <SideRow label="CLOSE" tone="text-amber-300" set={trade.direction === "long" ? check.exit_long : check.exit_short} />
                  ) : (
                    // Outside its trading hours, while the pair waits out its cooldown, or once the daily target
                    // is reached, the rules still read, dimmed: they open nothing until then.
                    <div className={offHours || wait || done ? "opacity-50" : undefined}>
                      {s.sides !== "short" && <SideRow label="▲ BUY" tone="text-bull" set={check.long} paused={offHours || !!wait || !!done} />}
                      {/* A hairline between the two sides, green into red, so each side's chips read as one block. */}
                      {s.sides === "both" && (
                        <div className="mx-1 mb-0.5 mt-1.5 h-px bg-gradient-to-r from-bull/40 via-neutral-600/30 to-bear/40" aria-hidden="true" />
                      )}
                      {s.sides !== "long" && <SideRow label="▼ SELL" tone="text-bear" set={check.short} paused={offHours || !!wait || !!done} />}
                      {check.market_open === false && <div className="mt-0.5 text-[10px] text-neutral-500">market closed</div>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
      {msg && <div className="rounded border border-amber-500/40 px-2 py-1 text-amber-300">{msg}</div>}
      <div className="flex items-center justify-between px-0.5 text-[10px] text-neutral-600">
        <span title="Each line is one group of rules - any one group is enough. Each chip is one of its conditions: ✓ holds now, ✗ doesn't, … can't be read yet. Hover a chip for what it read.">
          rules read every 5 s · not backtested
        </span>
        {onOpenBoard && (
          <button onClick={onOpenBoard} className="text-neutral-400 hover:text-amber-200">Open the board ›</button>
        )}
      </div>
    </div>
  );
}
