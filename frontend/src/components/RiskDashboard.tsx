import { useState, createContext, useContext } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
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
//
// No title of its own: it lives in the Risk drawer, whose header already says "Risk" — a second
// "Risk Dashboard" heading right under it was the same word twice. The row only appears to carry
// the "Trading paused" badge and its Resume button.
export function RiskDashboard({ risk, account, settings, onChanged }: Props) {
  const equity = account?.equity ?? risk?.starting_equity ?? null;
  const limits = settings?.risk;
  const isLive = settings?.app.broker_env === "live";

  // Breaker state: prefer the settings value, fall back to risk-state echo.
  const breakerOn = limits?.daily_loss_breaker_enabled ?? risk?.daily_loss_breaker_enabled ?? true;
  const [busy, setBusy] = useState(false);
  // A refused or failed save must SAY so: these are safety controls, and a silent failure left the
  // switch looking on while the server still had it off (full review 2026-10-20, FE-09).
  const [err, setErr] = useState<string | null>(null);
  const [rev, setRev] = useState(0);      // bumped on a failure: number fields drop the refused value
  const fail = (what: string, e: unknown) => {
    setErr(`${what} NOT saved: ${e instanceof Error ? e.message : String(e)}`);
    setRev((r) => r + 1);
  };

  // How many of the extra entry breakers are currently armed (for the collapsed-row status).
  const activeBreakers =
    ((limits?.max_trades_per_day ?? 0) > 0 ? 1 : 0) +
    ((limits?.max_consecutive_losses ?? 0) > 0 ? 1 : 0) +
    (limits?.perf_breaker_enabled ? 1 : 0);

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
    setErr(null);
    try {
      await api.updateRisk({ daily_loss_breaker_enabled: next });
      onChanged?.();
    } catch (e) {
      fail("Daily-loss breaker change", e);
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
    setErr(null);
    try {
      await api.resumeTrading();
      onChanged?.();
    } catch (e) {
      fail("Resume", e);
    } finally {
      setBusy(false);
    }
  }

  async function saveRisk(patch: Record<string, number | boolean>) {
    setBusy(true);
    setErr(null);
    try {
      await api.updateRisk(patch);
      onChanged?.();
    } catch (e) {
      fail("Risk setting", e);
    } finally {
      setBusy(false);
    }
  }

  // Real fractions, NOT clamped to 1: only the meter's bar is capped at full width. Clamping the
  // number made this read "100%" while the header said 120% for the same book — and the part over
  // the limit is exactly what this panel exists to show.
  const dailyLoss = risk ? -Math.min(0, risk.realized_pnl) : 0;
  const dailyLimit = risk?.daily_loss_limit_amount ?? null;
  const dailyPct = dailyLimit ? dailyLoss / dailyLimit : 0;

  const exposure = risk?.total_risk_amount ?? account?.total_risk_amount ?? 0;
  const exposureLimit = equity && limits ? equity * limits.max_total_exposure : null;
  const exposurePct = exposureLimit ? exposure / exposureLimit : 0;

  const posCount = account?.open_positions ?? 0;
  const posLimit = limits?.max_open_positions ?? 0;
  const posPct = posLimit ? posCount / posLimit : 0;

  return (
    <RevCtx.Provider value={rev}>
    <div className="card space-y-3">
      {err && (
        <div className="flex items-start justify-between gap-2 rounded border border-bear/50 bg-bear/15 px-2 py-1.5 text-xs font-semibold text-bear">
          <span>{err}</span>
          <button onClick={() => setErr(null)} className="shrink-0 underline">dismiss</button>
        </div>
      )}
      {risk?.trading_paused && (
        <div className="flex items-center justify-end">
          <div className="flex items-center gap-2">
            <span className="rounded-md bg-bear px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-white">
              Trading paused
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={resume}
              title="Clear today's daily-loss pause and allow new trades again."
              className="btn btn-subtle px-2 py-0.5 text-xs"
            >
              Resume
            </button>
          </div>
        </div>
      )}

      {risk?.entry_breaker && (
        <div
          className="rounded-md border border-bear/40 bg-bear/15 px-2.5 py-1.5 text-xs font-semibold text-bear"
          title="A circuit breaker is pausing new entries. Open positions are unaffected."
        >
          ⛔ New entries paused — {risk.entry_breaker}
        </div>
      )}

      <div className="grid grid-cols-3 gap-3 text-sm">
        <KV
          label="Equity"
          value={equity != null ? fmtUsd(equity) : "—"}
          title="Live account equity (balance + floating P&L). The daily-loss limit is measured against the day's starting equity."
        />
        <KV
          label="Realized (today)"
          value={risk ? fmtUsd(risk.realized_pnl, { sign: true }) : "—"}
          valueClass={risk && risk.realized_pnl < 0 ? "text-bear" : "text-bull"}
        />
        <KV
          label="Open P&L"
          value={risk ? fmtUsd(risk.unrealized_pnl, { sign: true }) : "—"}
          valueClass={risk && risk.unrealized_pnl < 0 ? "text-bear" : "text-bull"}
        />
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs text-neutral-400">
            Daily loss vs limit{dailyLimit ? ` (${fmtUsd(dailyLimit)})` : ""}
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
        label={`Exposure vs limit${exposureLimit ? ` · ${fmtUsd(exposure)} of ${fmtUsd(exposureLimit)}` : ""}`}
        pct={exposurePct}
        title="Risk-at-stop across open positions vs the exposure budget (equity × max exposure). Over 100%, no new trade can open until risk comes off."
      />
      <Meter label={`Open positions ${posCount}/${posLimit}`} pct={posPct} />

      {limits && (
        <div className="text-xs text-neutral-500">
          Per-trade risk {(limits.risk_per_trade * 100).toFixed(1)}% · cooldown{" "}
          {limits.per_pair_cooldown_minutes}m
        </div>
      )}

      {limits && (
        <details className="rounded-lg border border-neutral-800 bg-neutral-800/30">
          <summary className="flex cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-xs font-semibold text-neutral-300">
            <span>Circuit breakers</span>
            {activeBreakers > 0 ? (
              <span className="rounded bg-bull/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-bull">
                ● {activeBreakers} on
              </span>
            ) : (
              <span className="rounded bg-neutral-700/60 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-neutral-500">
                all off
              </span>
            )}
            <span className="font-normal text-neutral-500">— pause new entries</span>
          </summary>
          <div className="space-y-2 px-2.5 pb-2.5 pt-1">
            {/* One click to the anti-tilt limits - YOUR click, with the values spelled out first.
                These are process limits against revenge trading, not a strategy filter (the old
                breaker backtest predates the look-ahead fix, so they are justified as tilt control,
                not as edge). Full review 2026-10-20. */}
            <button
              onClick={() => {
                if (!window.confirm(
                  "Apply the discipline preset?\n\n" +
                  "- Pause new entries after 3 losses in a row, for 120 minutes\n" +
                  "- At most 6 new trades a day\n\n" +
                  "Applies to manual trades too. You can change or turn off each one below.")) return;
                void saveRisk({ max_consecutive_losses: 3, breaker_cooldown_minutes: 120, max_trades_per_day: 6 });
              }}
              className="w-full rounded border border-neutral-700 py-1 text-xs text-neutral-200 hover:bg-neutral-800"
              title="Fills in: 3 losses in a row -> 120 min pause, max 6 trades/day"
            >
              Apply discipline preset (3 losses → pause · max 6 trades/day)
            </button>
            <NumField
              label="Max trades / day"
              hint="Backstop against a runaway loop. 0 = off. Counts trades opened per UTC day."
              value={limits.max_trades_per_day}
              step={1}
              min={0}
              busy={busy}
              active={limits.max_trades_per_day > 0}
              onCommit={(v) => saveRisk({ max_trades_per_day: v })}
            />
            <NumField
              label="Max losses in a row"
              hint="After N losing trades in a row, pause new entries for the cooldown, then probe. A win resets the streak. 0 = off."
              value={limits.max_consecutive_losses}
              step={1}
              min={0}
              busy={busy}
              active={limits.max_consecutive_losses > 0}
              onCommit={(v) => saveRisk({ max_consecutive_losses: v })}
            />
            <NumField
              label="Breaker cooldown (min)"
              hint="How long the loss-streak / performance breakers pause before letting a probe trade through."
              value={limits.breaker_cooldown_minutes}
              step={15}
              min={0}
              busy={busy}
              onCommit={(v) => saveRisk({ breaker_cooldown_minutes: v })}
            />
            <label className="flex items-center justify-between gap-2 text-xs" title="Pause new entries when the last N trades average below the R floor (live results diverging from the backtest edge).">
              <span className="flex items-center gap-1.5">
                <span className="text-neutral-300">Performance breaker</span>
                <StatePill on={limits.perf_breaker_enabled} />
              </span>
              <input
                type="checkbox"
                disabled={busy}
                checked={limits.perf_breaker_enabled}
                onChange={(e) => saveRisk({ perf_breaker_enabled: e.target.checked })}
              />
            </label>
            {limits.perf_breaker_enabled && (
              <>
                <NumField
                  label="Expectancy floor (R)"
                  hint="Set this to your backtest expectancy minus a tolerance. If the live average drops below it, entries pause. e.g. -0.2"
                  value={limits.min_expectancy_r}
                  step={0.05}
                  busy={busy}
                  onCommit={(v) => saveRisk({ min_expectancy_r: v })}
                />
                <NumField
                  label="Window (trades)"
                  hint="How many recent closed trades the expectancy is measured over (needs a full window before it can trip)."
                  value={limits.expectancy_window}
                  step={1}
                  min={1}
                  busy={busy}
                  onCommit={(v) => saveRisk({ expectancy_window: v })}
                />
              </>
            )}
          </div>
        </details>
      )}

      {limits && (
        <details className="rounded-lg border border-neutral-800 bg-neutral-800/30">
          <summary className="flex cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-xs font-semibold text-neutral-300">
            <span>Spread gate</span>
            <StatePill on={limits.spread_gate_enabled} />
            <span className="font-normal text-neutral-500">— execution cost</span>
          </summary>
          <div className="space-y-2 px-2.5 pb-2.5 pt-1">
            <p className="text-[11px] leading-snug text-neutral-500">
              Blocks an entry when the live bid/ask spread is too big a share of its own stop
              distance. A spread worth 40% of R means the trade must be right by 1.4R just to make
              1R. Checked fresh at entry, so a symbol is only blocked while its book is actually
              wide (thin hours / rollover).
            </p>
            <label
              className="flex items-center justify-between gap-2 text-xs"
              title="Veto entries whose spread eats too much of the risk budget. Can only block trades, never loosen a limit."
            >
              <span className="flex items-center gap-1.5">
                <span className="text-neutral-300">Spread gate</span>
                <StatePill on={limits.spread_gate_enabled} />
              </span>
              <input
                type="checkbox"
                disabled={busy}
                checked={limits.spread_gate_enabled}
                onChange={(e) => saveRisk({ spread_gate_enabled: e.target.checked })}
              />
            </label>
            {limits.spread_gate_enabled && (
              <NumField
                label="Max spread (% of R)"
                hint="Reject entries whose spread exceeds this share of the stop distance. 25 is lenient (passes every liquid-session index entry); 10 is strict. 0 = off."
                value={Math.round(limits.max_spread_r_fraction * 1000) / 10}
                step={5}
                min={0}
                busy={busy}
                active={limits.max_spread_r_fraction > 0}
                onCommit={(v) => saveRisk({ max_spread_r_fraction: v / 100 })}
              />
            )}
            {limits.spread_gate_enabled && limits.max_spread_r_fraction === 0 && (
              <div className="rounded bg-bear/10 px-2 py-1 text-[11px] text-bear">
                ⚠ Max spread is 0% — the gate is on but does nothing.
              </div>
            )}
          </div>
        </details>
      )}

      {limits && (
        <details className="rounded-lg border border-neutral-800 bg-neutral-800/30">
          <summary className="flex cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-xs font-semibold text-neutral-300">
            <span>Weekend gap</span>
            <StatePill on={limits.weekend_block_enabled || limits.weekend_flatten_enabled} />
            <span className="font-normal text-neutral-500">— the risk a stop can't cover</span>
          </summary>
          <div className="space-y-2 px-2.5 pb-2.5 pt-1">
            <p className="text-[11px] leading-snug text-neutral-500">
              A stop only works while the market trades. Over a weekend price doesn't pass through
              it — it jumps over it. UKOILm was sized to lose 1R and lost 8.9R (−$434) on a Monday
              reopen. Crypto is exempt (trades 24/7).
            </p>
            <label
              className="flex items-center justify-between gap-2 text-xs"
              title="Refuse to OPEN new non-crypto trades in the hours before the Friday close."
            >
              <span className="flex items-center gap-1.5">
                <span className="text-neutral-300">No new trades before the close</span>
                <StatePill on={limits.weekend_block_enabled} />
              </span>
              <input
                type="checkbox"
                disabled={busy}
                checked={limits.weekend_block_enabled}
                onChange={(e) => saveRisk({ weekend_block_enabled: e.target.checked })}
              />
            </label>
            {limits.weekend_block_enabled && (
              <NumField
                label="Stop opening (hours before)"
                hint="How many hours before the Friday close to stop opening new non-crypto positions. 0 = off."
                value={limits.weekend_block_hours}
                step={1}
                min={0}
                busy={busy}
                active={limits.weekend_block_hours > 0}
                onCommit={(v) => saveRisk({ weekend_block_hours: v })}
              />
            )}
            <label
              className="flex items-center justify-between gap-2 text-xs"
              title="CLOSES your open non-crypto positions before the weekend. This acts on live trades."
            >
              <span className="flex items-center gap-1.5">
                <span className="text-neutral-300">Close positions before the weekend</span>
                <StatePill on={limits.weekend_flatten_enabled} />
              </span>
              <input
                type="checkbox"
                disabled={busy}
                checked={limits.weekend_flatten_enabled}
                onChange={(e) => saveRisk({ weekend_flatten_enabled: e.target.checked })}
              />
            </label>
            {limits.weekend_flatten_enabled ? (
              <NumField
                label="Close (hours before)"
                hint="How many hours before the Friday close to flatten open non-crypto positions."
                value={limits.weekend_flatten_hours}
                step={0.5}
                min={0}
                busy={busy}
                active={limits.weekend_flatten_hours > 0}
                onCommit={(v) => saveRisk({ weekend_flatten_hours: v })}
              />
            ) : (
              <div className="rounded bg-neutral-700/30 px-2 py-1 text-[11px] text-neutral-500">
                Off — positions open on Friday are carried through the weekend.
              </div>
            )}
          </div>
        </details>
      )}
    </div>
    </RevCtx.Provider>
  );
}

// A small ON/off state chip so each breaker's status is visible at a glance.
function StatePill({ on }: { on: boolean }) {
  return (
    <span
      className={`rounded px-1 py-0.5 text-[9px] font-bold uppercase tracking-wide ${
        on ? "bg-bull/15 text-bull" : "bg-neutral-700/60 text-neutral-500"
      }`}
    >
      {on ? "on" : "off"}
    </span>
  );
}

// Bumped by RiskDashboard when a save fails, so every NumField re-mounts with the server's value.
const RevCtx = createContext(0);

// A compact number field that commits on blur / Enter (so typing doesn't fire a request per keystroke).
function NumField({
  label,
  hint,
  value,
  step,
  min,
  busy,
  active,
  onCommit,
}: {
  label: string;
  hint?: string;
  value: number;
  step?: number;
  min?: number;
  busy: boolean;
  active?: boolean;
  onCommit: (value: number) => void;
}) {
  const rev = useContext(RevCtx);
  const commit = (raw: string) => {
    const v = Number(raw);
    if (!Number.isFinite(v) || v === value) return;
    if (min != null && v < min) return;
    onCommit(v);
  };
  return (
    <label className="flex items-center justify-between gap-2 text-xs" title={hint}>
      <span className="flex items-center gap-1.5">
        <span className="text-neutral-300">{label}</span>
        {active !== undefined && <StatePill on={active} />}
      </span>
      <input
        type="number"
        step={step ?? 1}
        min={min}
        disabled={busy}
        defaultValue={value}
        key={`${value}-${rev}`}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
        className="w-20 rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-right tabular-nums text-neutral-100"
      />
    </label>
  );
}

// `pct` is the real fraction (1.2 = 120%). The bar caps at full width; the number does not, and
// past 100% it says so in red.
function Meter({ label, pct, title }: { label?: string; pct: number; title?: string }) {
  const color = pct >= 0.9 ? "bg-bear" : pct >= 0.6 ? "bg-warn" : "bg-bull";
  const shown = Math.round(pct * 100);
  const over = shown > 100;   // judged on the printed number, so "100%" is never labelled over
  return (
    <div title={title}>
      {label && (
        <div className="mb-1 flex justify-between gap-2 text-xs">
          <span className="text-neutral-400">{label}</span>
          <span className={`whitespace-nowrap tabular-nums ${over ? "font-semibold text-bear" : "text-neutral-300"}`}>
            {shown}%{over && " · over limit"}
          </span>
        </div>
      )}
      <div className="h-1.5 overflow-hidden rounded-full bg-neutral-800">
        <div
          className={`h-full rounded-full ${color} transition-all duration-500`}
          style={{ width: `${Math.round(Math.min(1, Math.max(0, pct)) * 100)}%` }}
        />
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
    <div className="rounded-lg border border-neutral-800 bg-neutral-800/40 px-2.5 py-1.5" title={title}>
      <div className="text-[10px] font-medium uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`text-sm font-semibold tabular-nums ${valueClass ?? "text-neutral-100"}`}>{value}</div>
    </div>
  );
}
