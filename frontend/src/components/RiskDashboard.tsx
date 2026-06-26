import { useState } from "react";
import { api } from "../api/client";
import type { AccountState, RiskState, SettingsResponse } from "../types";

interface Props {
  risk: RiskState | null;
  account: AccountState | null;
  settings: SettingsResponse | null;
  // Lets a toggle here force an immediate settings/risk refetch.
  onChanged?: () => void;
}

// Daily P&L vs the daily-loss limit, current exposure, and open-position count, with
// visual warnings as limits approach.
export function RiskDashboard({ risk, account, settings, onChanged }: Props) {
  const equity = account?.equity ?? risk?.starting_equity ?? null;
  const limits = settings?.risk;
  const isLive = settings?.app.broker_env === "live";

  // Breaker state: prefer the settings value, fall back to risk-state echo.
  const breakerOn = limits?.daily_loss_breaker_enabled ?? risk?.daily_loss_breaker_enabled ?? true;
  const [busy, setBusy] = useState(false);

  async function toggleBreaker(next: boolean) {
    if (!next && isLive) {
      const ok = window.confirm(
        "Disable the daily-loss circuit breaker on a LIVE account?\n\n" +
          "This removes a hard real-money protection: the app will no longer auto-pause " +
          "after a losing day. Only do this if you really mean to.",
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      await api.updateRisk({ daily_loss_breaker_enabled: next });
      onChanged?.();
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    const ok = window.confirm(
      "Resume trading?\n\nThis clears today's daily-loss pause. The breaker stays armed and will " +
        "pause again if the day's realized loss reaches the limit.",
    );
    if (!ok) return;
    setBusy(true);
    try {
      await api.resumeTrading();
      onChanged?.();
    } finally {
      setBusy(false);
    }
  }

  const dailyLoss = risk ? -Math.min(0, risk.realized_pnl) : 0;
  const dailyLimit = risk?.daily_loss_limit_amount ?? null;
  const dailyPct = dailyLimit ? Math.min(1, dailyLoss / dailyLimit) : 0;

  const exposure = risk?.total_risk_amount ?? account?.total_risk_amount ?? 0;
  const exposureLimit = equity && limits ? equity * limits.max_total_exposure : null;
  const exposurePct = exposureLimit ? Math.min(1, exposure / exposureLimit) : 0;

  const posCount = account?.open_positions ?? 0;
  const posLimit = limits?.max_open_positions ?? 0;
  const posPct = posLimit ? Math.min(1, posCount / posLimit) : 0;

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold">Risk Dashboard</div>
        {risk?.trading_paused && (
          <div className="flex items-center gap-2">
            <span className="rounded bg-bear px-2 py-0.5 text-xs font-bold text-white">
              TRADING PAUSED
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={resume}
              title="Clear today's daily-loss pause and allow new trades again."
              className="rounded bg-neutral-700 px-2 py-0.5 text-xs font-semibold text-neutral-100 hover:bg-neutral-600 disabled:opacity-50"
            >
              Resume trading
            </button>
          </div>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3 text-sm">
        <KV
          label="Equity"
          value={equity != null ? `$${equity.toLocaleString()}` : "—"}
          title="Live account equity (balance + floating P&L). The daily-loss limit is measured against the day's starting equity."
        />
        <KV
          label="Realized (today)"
          value={risk ? `${risk.realized_pnl >= 0 ? "+" : ""}$${risk.realized_pnl.toFixed(2)}` : "—"}
          valueClass={risk && risk.realized_pnl < 0 ? "text-bear" : "text-bull"}
        />
        <KV
          label="Open P&L"
          value={risk ? `${risk.unrealized_pnl >= 0 ? "+" : ""}$${risk.unrealized_pnl.toFixed(2)}` : "—"}
          valueClass={risk && risk.unrealized_pnl < 0 ? "text-bear" : "text-bull"}
        />
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs text-neutral-400">
            Daily loss vs limit{dailyLimit ? ` ($${dailyLimit.toFixed(0)})` : ""}
          </span>
          <BreakerToggle on={breakerOn} busy={busy} onChange={toggleBreaker} />
        </div>
        {breakerOn ? (
          <Meter pct={dailyPct} />
        ) : (
          <div
            className={`rounded px-2 py-1 text-xs font-semibold ${
              isLive ? "bg-bear/20 text-bear" : "bg-warn/20 text-warn"
            }`}
            title="The daily-loss auto-pause and veto are disabled. New trades are NOT blocked on daily-loss grounds."
          >
            ⚠ Breaker OFF{isLive ? " on LIVE" : " (testing)"} — daily-loss pause disabled
          </div>
        )}
      </div>
      <Meter
        label={`Exposure vs limit${exposureLimit ? ` ($${exposureLimit.toFixed(0)})` : ""}`}
        pct={exposurePct}
      />
      <Meter label={`Open positions ${posCount}/${posLimit}`} pct={posPct} />

      {limits && (
        <div className="text-xs text-neutral-500">
          Per-trade risk {(limits.risk_per_trade * 100).toFixed(1)}% · cooldown{" "}
          {limits.per_pair_cooldown_minutes}m
        </div>
      )}
    </div>
  );
}

function Meter({ label, pct }: { label?: string; pct: number }) {
  const color = pct >= 0.9 ? "bg-bear" : pct >= 0.6 ? "bg-warn" : "bg-bull";
  return (
    <div>
      {label && (
        <div className="mb-1 flex justify-between text-xs text-neutral-400">
          <span>{label}</span>
          <span>{Math.round(pct * 100)}%</span>
        </div>
      )}
      <div className="h-2 rounded bg-neutral-800">
        <div className={`h-2 rounded ${color}`} style={{ width: `${Math.round(pct * 100)}%` }} />
      </div>
    </div>
  );
}

// Small on/off switch for the daily-loss circuit breaker. Green = armed (safe default),
// amber = OFF. Disabling removes a hard protection, so the label says so plainly.
function BreakerToggle({
  on,
  busy,
  onChange,
}: {
  on: boolean;
  busy: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => onChange(!on)}
      title={
        on
          ? "Daily-loss circuit breaker is ARMED. Click to turn OFF (no daily-loss auto-pause)."
          : "Daily-loss circuit breaker is OFF. Click to re-arm the protection."
      }
      className={`flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[11px] font-semibold transition ${
        on ? "bg-bull/15 text-bull hover:bg-bull/25" : "bg-warn/20 text-warn hover:bg-warn/30"
      } ${busy ? "opacity-50" : ""}`}
    >
      <span
        className={`inline-block h-2 w-2 rounded-full ${on ? "bg-bull" : "bg-warn"}`}
      />
      Breaker {on ? "ON" : "OFF"}
    </button>
  );
}

function KV({
  label,
  value,
  valueClass,
  title,
}: {
  label: string;
  value: string;
  valueClass?: string;
  title?: string;
}) {
  return (
    <div className="rounded bg-neutral-800/60 p-2" title={title}>
      <div className="text-xs text-neutral-400">{label}</div>
      <div className={`tabular-nums ${valueClass ?? ""}`}>{value}</div>
    </div>
  );
}
