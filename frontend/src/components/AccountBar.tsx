import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import { fmtUsd } from "../format";
import type { AccountState, PositionView, RiskState, SettingsResponse } from "../types";

interface Props {
  account: AccountState | null;
  risk: RiskState | null;
  settings: SettingsResponse | null;
  positions: PositionView[] | null;
}

function Stat({ label, value, valueClass = "", title, extra }: {
  label: string; value: ReactNode; valueClass?: string; title?: string; extra?: ReactNode;
}) {
  return (
    <div className="flex shrink-0 flex-col leading-tight" title={title}>
      <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
      <span className={`whitespace-nowrap text-sm font-semibold tabular-nums ${valueClass}`}>{value}</span>
      {extra}
    </div>
  );
}

/**
 * The account numbers a trader watches, as ONE compact strip that lives inside the sticky header:
 * equity, the day's P&L (realized + open floating), position slots used, exposure budget used, and
 * whether the daily-loss breaker has paused trading.
 *
 * It used to be a card under the header, which made it a third horizontal band before the chart and
 * scrolled away with the page. In the header it is visible on every view, including Journal.
 */
// 00:00 UTC - when the broker's day, and this cell's, starts - on this clock ("04:00" in Dubai).
const utcMidnightHere = () => {
  const d = new Date();
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()))
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
};

export function AccountKpis({ account, risk, settings, positions }: Props) {
  const equity = account?.equity ?? null;
  // Closed today: the risk state's figure first - the broker's own deal history since 00:00 UTC, every trade on the
  // account (the phone's too), swap and commission in. The MT5 account snapshot's daily_realized_pnl is always 0.0
  // (the adapter never fills it), and taking it first made this cell show open P&L only: "+$0.00" after a +$54 day
  // (2026-11-09; the user, 2026-11-11: "why is it always zero? I don't know how much I'm making per day").
  const realized = risk?.realized_pnl ?? account?.daily_realized_pnl ?? 0;
  const floating = positions && positions.length
    ? positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0)
    : (risk?.unrealized_pnl ?? 0);
  const dayPnl = realized + floating;
  // A % of what the account held when the day began (the risk state's starting equity). Of the equity NOW, a losing
  // day read worse than it was: -$97.75 showed -8.76% (of $1,116) where it was -8.05% of the day's $1,214 (2026-11-11).
  const dayBase = risk?.starting_equity && risk.starting_equity > 0 ? risk.starting_equity
    : equity != null ? equity - dayPnl : null;
  const dayPct = dayBase && dayBase > 0 ? (dayPnl / dayBase) * 100 : null;

  const openN = account?.open_positions ?? positions?.length ?? 0;
  const maxPos = settings?.risk.max_open_positions ?? null;

  const maxExp = settings?.risk.max_total_exposure ?? null;
  // Risk state first, same as RiskDashboard: the MT5 account snapshot reports total_risk_amount as
  // 0, so preferring it made this read "0%" while the Risk panel showed the real ~80%.
  const riskAmt = risk?.total_risk_amount ?? account?.total_risk_amount ?? 0;
  const budget = equity && maxExp ? equity * maxExp : null;
  const expPct = budget && budget > 0 ? (riskAmt / budget) * 100 : null;
  const noRoom = expPct != null && Math.round(expPct) >= 100;   // judged on the number shown

  const paused = !!(account?.trading_paused || risk?.trading_paused);
  const breakerOn = settings?.risk.daily_loss_breaker_enabled ?? risk?.daily_loss_breaker_enabled ?? true;
  const entryBreaker = risk?.entry_breaker ?? null;

  const pnlClass = dayPnl > 0 ? "text-bull" : dayPnl < 0 ? "text-bear" : "text-neutral-300";
  const expColor =
    expPct == null ? "text-neutral-200" : expPct >= 90 ? "text-bear" : expPct >= 70 ? "text-warn" : "text-neutral-200";
  const barColor = expPct == null ? "bg-neutral-700" : expPct >= 90 ? "bg-bear" : expPct >= 70 ? "bg-warn" : "bg-bull";

  // One word for the breaker, loudest state first. PAUSED is the one that matters: nothing new can
  // open until tomorrow (or until you lift it in Risk), and it must read as an alarm, not a label.
  const breaker = paused
    ? { text: "PAUSED", cls: "rounded bg-bear px-1.5 text-white", tip: `Trading paused — ${risk?.pause_reason || "daily-loss limit reached"}` }
    : entryBreaker
      ? { text: "TRIPPED", cls: "text-warn", tip: `Entry breaker tripped: ${entryBreaker}` }
      : !breakerOn
        // Amber, not grey: grey read as "nothing to see" even on a −5% day, while the Risk drawer
        // flags the same state as a warning. Off is a choice you can make, not a resting state.
        ? { text: "OFF", cls: "text-warn", tip: "The daily-loss breaker is switched off — no auto-pause on a losing day (Risk)" }
        : {
            text: "OK",
            cls: "text-bull",
            tip: risk?.daily_loss_limit_amount != null
              ? `Daily-loss breaker armed: pauses new trades at a ${fmtUsd(risk.daily_loss_limit_amount)} loss for the day`
              : "Daily-loss breaker armed",
          };

  return (
    <div className="flex min-w-0 items-center gap-x-3 gap-y-1 min-[2000px]:gap-x-5">
      <Stat label="Equity" value={equity != null ? fmtUsd(equity) : "—"} />
      <Stat
        label="Day P&L"
        title={`Today, on the broker's day - from 00:00 UTC (${utcMidnightHere()} your time): closed ${fmtUsd(realized, { sign: true })} + open now ${
          fmtUsd(floating, { sign: true })}. Every trade on the account counts, your phone's too; swap and commission in.${
          dayBase ? ` The % is of ${fmtUsd(dayBase)}, what the account held when the day began.` : ""}${
          risk?.trades_count != null ? ` ${risk.trades_count} trade${risk.trades_count === 1 ? "" : "s"} opened today.` : ""}`}
        valueClass={pnlClass}
        value={
          equity != null ? (
            <>
              {fmtUsd(dayPnl, { sign: true })}
              {dayPct != null && (
                <span className="ml-1 text-xs font-normal">
                  ({dayPct >= 0 ? "+" : ""}
                  {dayPct.toFixed(2)}%)
                </span>
              )}
              {/* How busy the day has been, next to its result - the discipline state was invisible. */}
              {risk?.trades_count != null && (
                <span className="ml-1.5 hidden text-[11px] font-normal text-neutral-500 min-[1500px]:inline" title="Trades opened today">
                  · {risk.trades_count} today
                </span>
              )}
            </>
          ) : (
            "—"
          )
        }
      />
      <TargetStat risk={risk} settings={settings} realized={realized} />
      {/* Slots alone overstate the room: with the exposure budget used up, "2 / 5" still read as
          three free slots while nothing new could open. So a full budget marks this full too. */}
      <Stat
        label="Open"
        value={
          <>
            {maxPos != null ? `${openN} / ${maxPos}` : String(openN)}
            {noRoom && <span className="ml-1 text-xs font-normal">· full</span>}
          </>
        }
        valueClass={(maxPos != null && openN >= maxPos) || noRoom ? "text-warn" : ""}
        title={
          noRoom
            ? `No room: exposure budget used up (${Math.round(expPct ?? 0)}%) — no new trade can open until risk comes off, whatever the slot count says`
            : "Open positions vs the max-open-positions limit"
        }
      />
      <Stat
        label="Exposure"
        valueClass={expColor}
        value={expPct != null ? `${Math.round(expPct)}%` : "—"}
        title={
          budget != null
            ? `${fmtUsd(riskAmt)} risk-at-stop of a ${fmtUsd(budget)} exposure budget`
            : "Risk-at-stop across open positions vs the exposure budget"
        }
        extra={
          <div className="mt-0.5 h-0.5 w-14 overflow-hidden rounded-full bg-neutral-800">
            <div className={`h-full ${barColor}`} style={{ width: `${Math.min(100, expPct ?? 0)}%` }} />
          </div>
        }
      />
      <Stat label="Breaker" value={breaker.text} valueClass={breaker.cls} title={breaker.tip} />
    </div>
  );
}

// The amounts the user named (2026-11-16: "$500, $600, $700, $1,000 per day").
const TARGET_PRESETS = [500, 600, 700, 1000];
const fmtWhole = (v: number) => `${v < 0 ? "-" : ""}$${Math.abs(Math.round(v)).toLocaleString()}`;
const hhmm = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }) : "";

/**
 * The account's DAILY PROFIT TARGET (asked 2026-11-16: "a daily target to win - $500, $600, $700, $1,000 - when I
 * reach it, we stop trading for that day"). Its cell shows the closed profit today against the target; a click opens
 * the editor. Reached (the server latches it, risk.daily_target.hit), nothing new opens until 00:00 UTC - the
 * desk's tickets, armed setups, the advisor and the strategies alike - while open trades keep their stop and target.
 * Raising or clearing a target already reached asks once more: it is the one way to trade on that day.
 */
function TargetStat({ risk, settings, realized }: { risk: RiskState | null; settings: SettingsResponse | null; realized: number }) {
  const server = risk?.daily_target ?? null;
  const savedTarget = server?.target ?? settings?.risk.daily_profit_target ?? null;
  // Just saved: shown at once, until the 5-s polls bring the server's own reading.
  const [local, setLocal] = useState<{ v: number | null } | null>(null);
  useEffect(() => {
    if (local && (savedTarget ?? null) === local.v) setLocal(null);
  }, [savedTarget, local]);
  const target = local ? local.v : savedTarget;
  const hit = !local && !!server?.hit;
  const closed = server?.realized ?? realized;

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [confirm, setConfirm] = useState<number | null | undefined>(undefined);   // a loosening waiting for "yes"
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const r = btnRef.current.getBoundingClientRect();
    setAt({ left: Math.max(8, Math.min(r.left, window.innerWidth - 312)), top: r.bottom + 8 });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!popRef.current?.contains(t) && !btnRef.current?.contains(t)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("mousedown", down);
    window.addEventListener("keydown", key);
    return () => { window.removeEventListener("mousedown", down); window.removeEventListener("keydown", key); };
  }, [open]);

  const start = () => {
    setDraft(target != null ? String(target) : "");
    setConfirm(undefined);
    setErr(null);
    setOpen((o) => !o);
  };
  const save = async (v: number | null, sure = false) => {
    // Reached today: a higher target or none at all lets trading start again - asked once more.
    if (hit && !sure && (v == null || (target != null && v > target))) {
      setConfirm(v);
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.updateRisk({ daily_profit_target: v ?? 0 });
      setLocal({ v });
      setOpen(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const typed = Number(draft);
  const typedOk = draft.trim() !== "" && Number.isFinite(typed) && typed > 0 && typed <= 1_000_000;

  const pct = target && target > 0 ? Math.max(0, Math.min(100, (closed / target) * 100)) : 0;
  const tip = target == null
    ? "No daily profit target. Click to set one: once the account's closed profit today reaches it, nothing new opens until the next day."
    : hit
      ? `Daily target ${fmtUsd(target)} reached at ${hhmm(server?.hit_at)} with ${fmtUsd(server?.hit_pnl ?? 0, { sign: true })} closed - no new trades until ${utcMidnightHere()} (00:00 UTC). Open trades keep their stop and target. Click to change it.`
      : `Closed today ${fmtUsd(closed, { sign: true })} of your ${fmtUsd(target)} daily target (${Math.round(pct)}%). Reached, nothing new opens until ${utcMidnightHere()}. Click to change it.`;

  return (
    <>
      <Stat
        label="Target"
        title={tip}
        valueClass={hit ? "text-bull" : target == null ? "text-neutral-500" : "text-neutral-200"}
        value={
          <button ref={btnRef} type="button" onClick={start} aria-expanded={open} className="rounded hover:text-white">
            {target == null ? "＋ set" : hit ? `🎯 HIT ${fmtWhole(server?.hit_pnl ?? closed)}` : `${fmtWhole(closed)} / ${fmtWhole(target)}`}
            {local && <span className="ml-1 text-[11px] font-normal text-neutral-500">…</span>}
          </button>
        }
        extra={target != null && !hit ? (
          <div className="mt-0.5 h-0.5 w-14 overflow-hidden rounded-full bg-neutral-800">
            <div className="h-full bg-bull" style={{ width: `${pct}%` }} />
          </div>
        ) : undefined}
      />
      {open && at && createPortal(
        <div
          ref={popRef}
          role="dialog"
          aria-label="Daily profit target"
          className="fixed z-[90] w-[300px] rounded-lg border border-neutral-700 bg-neutral-900 p-3 text-xs text-neutral-300 shadow-2xl"
          style={{ left: at.left, top: at.top }}
        >
          <div className="text-sm font-semibold text-neutral-100">🎯 Daily profit target</div>
          <p className="mt-1 leading-snug text-neutral-400">
            Once the account&apos;s <b className="text-neutral-200">closed</b> profit today reaches it, nothing new opens until{" "}
            {utcMidnightHere()} (00:00 UTC): your tickets, armed setups, the advisor and the strategies. Open trades keep their
            stop and target. Trades from your phone count, but the app can&apos;t stop those.
          </p>
          {hit && (
            <p className="mt-2 rounded bg-bull/15 px-2 py-1 text-bull">
              Reached at {hhmm(server?.hit_at)} with {fmtUsd(server?.hit_pnl ?? 0, { sign: true })} closed.
            </p>
          )}
          <div className="mt-2 flex flex-wrap gap-1">
            {TARGET_PRESETS.map((p) => (
              <button
                key={p}
                type="button"
                disabled={busy}
                onClick={() => save(p)}
                className={`rounded border px-2 py-0.5 font-semibold tabular-nums ${
                  target === p ? "border-bull/60 bg-bull/20 text-bull" : "border-neutral-700 text-neutral-200 hover:border-bull/50 hover:text-bull"
                }`}
              >
                {fmtWhole(p)}
              </button>
            ))}
          </div>
          <form
            className="mt-2 flex items-center gap-1"
            onSubmit={(e) => { e.preventDefault(); if (typedOk) save(Math.round(typed * 100) / 100); }}
          >
            <span className="text-neutral-500">$</span>
            <input
              type="number"
              min={1}
              step={50}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="any amount"
              aria-label="Daily profit target in dollars"
              className="w-28 rounded border border-neutral-700 bg-neutral-950 px-1.5 py-0.5 tabular-nums text-neutral-100"
            />
            <button type="submit" disabled={!typedOk || busy} className="rounded bg-bull/80 px-2 py-0.5 font-semibold text-black disabled:opacity-40">
              Set
            </button>
            {target != null && (
              <button type="button" disabled={busy} onClick={() => save(null)} className="ml-auto rounded px-1.5 py-0.5 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100">
                No target
              </button>
            )}
          </form>
          {confirm !== undefined && (
            <div className="mt-2 rounded border border-warn/50 bg-warn/10 p-2 text-warn">
              {confirm == null ? "Clear the target" : `Raise it to ${fmtWhole(confirm)}`}? You reached your target today - this lets
              trading start again right now.
              <div className="mt-1.5 flex gap-1">
                <button type="button" disabled={busy} onClick={() => save(confirm, true)} className="rounded bg-warn px-2 py-0.5 font-semibold text-black">
                  Yes, trade on
                </button>
                <button type="button" onClick={() => setConfirm(undefined)} className="rounded px-2 py-0.5 text-neutral-300 hover:bg-neutral-800">
                  Keep it
                </button>
              </div>
            </div>
          )}
          {err && <p className="mt-2 text-bear">{err}</p>}
        </div>,
        document.body,
      )}
    </>
  );
}

/**
 * Full-width alert strips under the header. Only states that stop orders from going through get a
 * strip; everything else is a number in the KPI strip. One strip per cause, never two for the same
 * cause (the kill-switch used to be announced twice, by the header and by the account bar).
 */
export function AccountAlerts({ account, risk, settings }: Omit<Props, "positions">) {
  // Poll whether MT5 will actually accept automated orders. Every 30s is plenty: the switch is
  // flipped by hand in the terminal, and the cost of noticing 30s late is one refused order.
  const { data: mt5 } = usePolling(() => api.mt5Status(), 30000, []);
  const algoOff = mt5?.connected && mt5.algo_trading === false;
  const paused = account?.trading_paused || risk?.trading_paused;
  const ks = settings?.app.kill_switch_engaged || settings?.env_kill_switch;

  return (
    <>
      {/* The loudest strip, on purpose: with Algo Trading off in MT5 the app still looks connected
          and healthy, yet the hybrid, armed setups, auto-trader and quick trades all fail on submit.
          On 2026-09-09 seven orders were refused in two minutes that way. */}
      {algoOff && (
        <div className="bg-bear/90 px-4 py-1.5 text-center text-sm font-semibold text-white">
          ⛔ MT5 is refusing automated orders — {mt5?.algo_trading_reason ?? "Algo Trading is disabled"}.
          <span className="ml-1 font-normal">No new trade can open until it is switched back on.</span>
        </div>
      )}
      {ks && (
        <div className="bg-warn px-4 py-1.5 text-center text-sm font-medium text-black">
          ⛔ Kill-switch engaged — no new orders will be submitted (open positions are still managed).
        </div>
      )}
      {/* The day's target reached (2026-11-16): orders are refused for the rest of the day - say why, in green. */}
      {!ks && risk?.daily_target?.hit && (
        <div className="border-b border-bull/40 bg-bull/15 px-4 py-1.5 text-center text-sm font-medium text-bull">
          🎯 Daily target reached - {fmtUsd(risk.daily_target.hit_pnl ?? 0, { sign: true })} closed today (target{" "}
          {fmtUsd(risk.daily_target.target)}). No new trades until {utcMidnightHere()}; open trades keep their stop and target.
        </div>
      )}
      {!ks && paused && (
        <div className="border-b border-warn/40 bg-warn/15 px-4 py-1.5 text-center text-sm font-medium text-warn">
          ⏸ Trading paused — {risk?.pause_reason || "daily-loss limit reached"}.
        </div>
      )}
    </>
  );
}
