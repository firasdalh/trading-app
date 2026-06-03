import type { AccountState, RiskState, SettingsResponse } from "../types";

interface Props {
  risk: RiskState | null;
  account: AccountState | null;
  settings: SettingsResponse | null;
}

// Daily P&L vs the daily-loss limit, current exposure, and open-position count, with
// visual warnings as limits approach.
export function RiskDashboard({ risk, account, settings }: Props) {
  const equity = account?.equity ?? risk?.starting_equity ?? null;
  const limits = settings?.risk;

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
          <span className="rounded bg-bear px-2 py-0.5 text-xs font-bold text-white">
            TRADING PAUSED
          </span>
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

      <Meter
        label={`Daily loss vs limit${dailyLimit ? ` ($${dailyLimit.toFixed(0)})` : ""}`}
        pct={dailyPct}
      />
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

function Meter({ label, pct }: { label: string; pct: number }) {
  const color = pct >= 0.9 ? "bg-bear" : pct >= 0.6 ? "bg-warn" : "bg-bull";
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-neutral-400">
        <span>{label}</span>
        <span>{Math.round(pct * 100)}%</span>
      </div>
      <div className="h-2 rounded bg-neutral-800">
        <div className={`h-2 rounded ${color}`} style={{ width: `${Math.round(pct * 100)}%` }} />
      </div>
    </div>
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
