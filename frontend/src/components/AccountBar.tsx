import type { ReactNode } from "react";
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
export function AccountKpis({ account, risk, settings, positions }: Props) {
  const equity = account?.equity ?? null;
  const realized = account?.daily_realized_pnl ?? risk?.realized_pnl ?? 0;
  const floating = positions && positions.length
    ? positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0)
    : (risk?.unrealized_pnl ?? 0);
  const dayPnl = realized + floating;
  const dayPct = equity && equity > 0 ? (dayPnl / equity) * 100 : null;

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
    <div className="flex min-w-0 items-center gap-x-4 gap-y-1 2xl:gap-x-5">
      <Stat label="Equity" value={equity != null ? fmtUsd(equity) : "—"} />
      <Stat
        label="Day P&L"
        title="Realized today + open floating P&L"
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
                <span className="ml-1.5 text-[11px] font-normal text-neutral-500" title="Trades opened today">
                  · {risk.trades_count} today
                </span>
              )}
            </>
          ) : (
            "—"
          )
        }
      />
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
      {!ks && paused && (
        <div className="border-b border-warn/40 bg-warn/15 px-4 py-1.5 text-center text-sm font-medium text-warn">
          ⏸ Trading paused — {risk?.pause_reason || "daily-loss limit reached"}.
        </div>
      )}
    </>
  );
}
