import type { ReactNode } from "react";
import { fmtUsd } from "../format";
import type { AccountState, PositionView, RiskState, SettingsResponse } from "../types";

interface Props {
  account: AccountState | null;
  risk: RiskState | null;
  settings: SettingsResponse | null;
  positions: PositionView[] | null;
}

function Stat({ label, value, valueClass = "", title }: {
  label: string; value: ReactNode; valueClass?: string; title?: string;
}) {
  return (
    <div className="flex flex-col" title={title}>
      <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
      <span className={`text-sm font-semibold tabular-nums ${valueClass}`}>{value}</span>
    </div>
  );
}

// Always-visible account header: equity, the day's P&L (realized + open floating), how many of the
// position slots are used, how much of the exposure budget is consumed, and the execution mode —
// plus a prominent banner when trading is paused or the kill-switch is engaged. Pulls the numbers a
// trader watches up to the top instead of leaving them only in the Risk panel at the bottom.
export function AccountBar({ account, risk, settings, positions }: Props) {
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
  const riskAmt = account?.total_risk_amount ?? risk?.total_risk_amount ?? 0;
  const budget = equity && maxExp ? equity * maxExp : null;
  const expPct = budget && budget > 0 ? (riskAmt / budget) * 100 : null;

  const paused = account?.trading_paused || risk?.trading_paused;
  const ks = settings?.app.kill_switch_engaged;
  const mode = settings?.app.execution_mode;
  const modeLabel =
    mode === "C_AUTO_LIVE" ? "Auto · LIVE" : mode === "B_AUTO_PAPER" ? "Auto · paper" : "Approve";

  const pnlClass = dayPnl > 0 ? "text-bull" : dayPnl < 0 ? "text-bear" : "text-neutral-300";
  const expColor =
    expPct == null ? "text-neutral-200" : expPct >= 90 ? "text-bear" : expPct >= 70 ? "text-warn" : "text-neutral-200";
  const barColor = expPct == null ? "bg-neutral-700" : expPct >= 90 ? "bg-bear" : expPct >= 70 ? "bg-warn" : "bg-bull";

  return (
    <div className="space-y-2">
      {(ks || paused) && (
        <div
          className={`rounded-md border px-3 py-2 text-sm font-medium ${
            ks ? "border-bear/50 bg-bear/10 text-bear" : "border-warn/50 bg-warn/10 text-warn"
          }`}
        >
          {ks
            ? "⛔ Kill-switch engaged — no new orders will be submitted (open positions are still managed)."
            : `⏸ Trading paused — ${risk?.pause_reason || "daily-loss limit reached"}.`}
        </div>
      )}
      <div className="card flex flex-wrap items-center gap-x-6 gap-y-3">
        <Stat label="Equity" value={equity != null ? fmtUsd(equity) : "—"} />
        <Stat
          label="Day P&L (incl. open)"
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
              </>
            ) : (
              "—"
            )
          }
        />
        <Stat
          label="Open positions"
          value={maxPos != null ? `${openN} / ${maxPos}` : String(openN)}
          title="Open positions vs the max-open-positions limit"
        />
        <div
          className="flex flex-col"
          title={
            budget != null
              ? `${fmtUsd(riskAmt)} risk-at-stop of a ${fmtUsd(budget)} exposure budget`
              : "Risk-at-stop across open positions vs the exposure budget"
          }
        >
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">Exposure used</span>
          <span className={`text-sm font-semibold tabular-nums ${expColor}`}>
            {expPct != null ? `${Math.round(expPct)}%` : "—"}
          </span>
          {expPct != null && (
            <div className="mt-1 h-1 w-24 overflow-hidden rounded-full bg-neutral-800">
              <div className={`h-full ${barColor}`} style={{ width: `${Math.min(100, expPct)}%` }} />
            </div>
          )}
        </div>
        <div className="ml-auto">
          <Stat label="Mode" value={modeLabel} valueClass={mode === "C_AUTO_LIVE" ? "text-bear" : ""} />
        </div>
      </div>
    </div>
  );
}
