import { useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import type { AssetClass, PositionView, StrategyCheck, StrategyCheckCondition, StrategyCheckSet, StrategyView } from "../types";
import { draftOf } from "./StrategyBoard";

/**
 * STRATEGIES AT A GLANCE: the Strategies board's live read, where the chart is - scalp mode's sidebar
 * and the full-screen chart's pop-up - so watching a strategy work doesn't mean leaving the chart.
 *
 * Per strategy: running or not, and today's result from the journal (so the trades its stop or target
 * closed count too). Per pair of a running one: the trade it holds, with its live P&L and how much of
 * its close rule holds - or, for BUY and SELL, how many of the entry conditions hold now, one chip per
 * condition. The reads are the board's own read-only check; nothing here edits a strategy, and
 * Start / Stop is the board's switch.
 */

const CHECK_MS = 5000;
const MAX_CHECKS = 8;    // pairs read per round, across the running strategies

// Chip names (the board's NAMES are menu entries, too long for a chip).
const SHORT: Record<string, string> = {
  ema_cross: "EMA×", ema_above: "EMA>", price_ema: "vs EMA", trend: "Trend", supertrend: "ST", adx: "ADX",
  rsi_rising: "RSI↗", rsi_level: "RSI", macd: "MACD", stoch_cross: "Stoch", vwap: "VWAP", volume: "Vol",
  bb_break: "BB", candles: "Candles", sr_room: "S/R", zone_room: "Zone", spread: "Spread", session: "Session",
  rsi_extreme: "RSI TP", profit_r: "+R", time_stop: "Time",
};

const sameSym = (a: string, b: string) => a.toUpperCase() === b.toUpperCase();

// The group closest to firing: the one that fires, else the one with the most conditions holding.
function closest(set: StrategyCheckSet | null | undefined) {
  if (!set || !set.groups.length) return null;
  const held = (i: number) => set.groups[i].conditions.filter((c) => c.ok === true).length;
  let gi = set.ok && set.group != null ? set.group : 0;
  if (!set.ok) set.groups.forEach((_, i) => { if (held(i) > held(gi)) gi = i; });
  const g = set.groups[gi];
  return { g, n: held(gi), of: g.conditions.length, fires: set.ok, alt: set.groups.length - 1 };
}

function Chip({ c }: { c: StrategyCheckCondition }) {
  const tone = c.ok === true ? "bg-bull/20 text-bull" : c.ok === false ? "bg-bear/15 text-bear" : "bg-neutral-800 text-neutral-500";
  return (
    <span className={`whitespace-nowrap rounded px-1 text-[10px] leading-4 ${tone}`} title={`${c.label}\nnow: ${c.detail}`}>
      {c.ok === true ? "✓" : c.ok === false ? "✗" : "…"} {SHORT[c.type] ?? c.type} <span className="opacity-60">{c.tf}</span>
    </span>
  );
}

function SideRow({ label, tone, set }: { label: string; tone: string; set: StrategyCheckSet | null | undefined }) {
  const b = closest(set);
  if (!b) return null;
  const pct = b.of ? b.n / b.of : 0;
  return (
    <div className="mt-1 flex items-start gap-1.5">
      <span className={`mt-px w-11 shrink-0 text-[10px] font-bold ${tone}`}>{label}</span>
      <span className="mt-1 flex shrink-0 items-center gap-1" title={b.alt ? `The closest of its ${b.alt + 1} groups` : undefined}>
        <span className="relative h-1.5 w-12 overflow-hidden rounded-full bg-neutral-800">
          <span
            className={`absolute inset-y-0 left-0 ${b.fires ? "bg-bull" : pct >= 0.75 ? "bg-amber-400" : "bg-neutral-500"}`}
            style={{ width: `${pct * 100}%` }}
          />
        </span>
        <span className={`w-8 text-[10px] tabular-nums ${b.fires ? "font-bold text-bull" : "text-neutral-400"}`}>
          {b.fires ? "GO" : `${b.n}/${b.of}`}
        </span>
      </span>
      <span className="flex min-w-0 flex-wrap gap-0.5">{b.g.conditions.map((c, i) => <Chip key={i} c={c} />)}</span>
    </div>
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
  const reads = running.flatMap((s) => s.pairs.map((p) => ({ s, p }))).slice(0, MAX_CHECKS);
  // Re-armed only when a running strategy's RULES change, not on every refresh of its notes.
  const readsKey = JSON.stringify(reads.map(({ s, p }) => [s.id, p.symbol, draftOf(s)]));
  const { data: checks } = usePolling(async () => {
    const out: Record<string, StrategyCheck | null> = {};
    await Promise.all(reads.map(async ({ s, p }) => {
      try {
        out[`${s.id}|${p.symbol}`] = await api.checkStrategy(draftOf(s), p.symbol, p.asset_class);
      } catch {
        out[`${s.id}|${p.symbol}`] = null;
      }
    }));
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
        return (
          <div
            key={s.id}
            className={`rounded-lg border p-2 ${s.enabled ? "border-bull/40 bg-bull/[0.04]" : "border-neutral-800 bg-neutral-900/40 opacity-80"}`}
          >
            <div className="flex items-center gap-1.5">
              <span className={`h-2 w-2 shrink-0 rounded-full ${s.enabled ? "animate-pulse bg-bull" : "bg-neutral-600"}`} />
              <span className="truncate font-semibold text-neutral-100" title={s.name}>{s.name}</span>
              <span className="shrink-0 rounded bg-neutral-800 px-1 text-[10px] text-sky-300" title="Base timeframe">{s.timeframe}</span>
              {s.sides !== "both" && (
                <span className="shrink-0 text-[10px] text-neutral-500">{s.sides === "long" ? "buy only" : "sell only"}</span>
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
                    <>
                      {s.sides !== "short" && <SideRow label="▲ BUY" tone="text-bull" set={check.long} />}
                      {s.sides !== "long" && <SideRow label="▼ SELL" tone="text-bear" set={check.short} />}
                      {check.market_open === false && <div className="mt-0.5 text-[10px] text-neutral-500">market closed</div>}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
      {msg && <div className="rounded border border-amber-500/40 px-2 py-1 text-amber-300">{msg}</div>}
      <div className="flex items-center justify-between px-0.5 text-[10px] text-neutral-600">
        <span title="Each chip is one condition of the group closest to firing: ✓ holds now, ✗ doesn't, … can't be read yet. Hover a chip for what it read.">
          rules read every 5 s · not backtested
        </span>
        {onOpenBoard && (
          <button onClick={onOpenBoard} className="text-neutral-400 hover:text-amber-200">Open the board ›</button>
        )}
      </div>
    </div>
  );
}
