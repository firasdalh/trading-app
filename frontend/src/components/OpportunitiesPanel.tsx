import { useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import { ArmSetupButton } from "./ArmSetupButton";
import { RegimeBadge } from "./RegimeBadge";
import { ReviewExplanation } from "./ReviewExplanation";
import type { OpportunityView } from "../types";

interface Props {
  onSelect?: (o: { symbol: string; asset_class: string }) => void; // open on the chart
  onOpened?: () => void; // refresh positions after opening
}

const DIR: Record<string, { label: string; cls: string }> = {
  long: { label: "LONG", cls: "bg-bull/20 text-bull" },
  short: { label: "SHORT", cls: "bg-bear/20 text-bear" },
  no_trade: { label: "NO TRADE", cls: "bg-neutral-800 text-neutral-400" },
};

// Scan every watchlist pair at once and rank the best setups — so you don't check pair by pair.
export function OpportunitiesPanel({ onSelect, onOpened }: Props) {
  const [items, setItems] = useState<OpportunityView[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [openingKey, setOpeningKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [listOpen, setListOpen] = useState(true); // collapse the (long) results list
  // Global scan timeframe — "" = each pair's own TF; else scan/run every pair on this TF. Persisted.
  const [scanTf, setScanTf] = useState<string>(() => {
    try {
      return localStorage.getItem("scan.timeframe") || "";
    } catch {
      return "";
    }
  });
  const setTf = (v: string) => {
    setScanTf(v);
    try {
      localStorage.setItem("scan.timeframe", v);
    } catch {
      /* ignore */
    }
  };

  const scan = async () => {
    setBusy(true);
    setError(null);
    try {
      setItems(await api.opportunities(scanTf || undefined));
      setListOpen(true); // show results after a scan
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const open = async (o: OpportunityView) => {
    const key = `${o.symbol}-${o.timeframe}`;
    setOpeningKey(key);
    setError(null);
    try {
      const res = await api.analyze(o.symbol, o.asset_class, o.timeframe);
      // Mode A leaves it pending approval; approve to execute. Mode B already auto-executed.
      if (res.status === "pending_approval") await api.approve(res.proposal_id);
      onOpened?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      // Free the button the instant the trade is opened — don't hold it through the (slow,
      // LLM-backed) list re-scan below.
      setOpeningKey(null);
    }
    void scan();  // refresh the list in the background (its own "Scanning…" state); the pair is now open
  };

  const actionableCount = items?.filter(
    (o) => (o.direction === "long" || o.direction === "short") && o.risk_approved && !o.already_open,
  ).length;

  return (
    <div className="card">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <button
          onClick={() => setListOpen((o) => !o)}
          className="flex items-center gap-1 text-sm font-semibold hover:text-neutral-300"
          title={listOpen ? "Collapse the results" : "Expand the results"}
          aria-expanded={listOpen}
        >
          <span className="text-xs text-neutral-500">{listOpen ? "▾" : "▸"}</span>
          Opportunities
        </button>
        <span className="text-xs text-neutral-500">
          ranks all pairs, then AI-reviews the actionable ones (matches Run analysis)
        </span>
        {items && (
          <span className="text-xs text-neutral-400">
            · {actionableCount} actionable / {items.length} scanned
          </span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <label className="text-xs text-neutral-500" title="Scan & Run-now every pair on this timeframe (instead of each pair's own)">
            TF
          </label>
          <select
            value={scanTf}
            onChange={(e) => setTf(e.target.value)}
            className="rounded bg-neutral-800 px-1.5 py-1 text-xs text-neutral-100"
            title="Timeframe the Scan watchlist + Hybrid Run-now use. 'Per-pair' = each pair's own."
          >
            <option value="">Per-pair</option>
            {["15m", "30m", "1h", "4h", "1d"].map((tf) => (
              <option key={tf} value={tf}>{tf}</option>
            ))}
          </select>
          <button
            onClick={scan}
            disabled={busy}
            className="btn bg-blue-600 text-white hover:bg-blue-500"
          >
            {busy ? "Scanning…" : "Scan watchlist"}
          </button>
        </div>
      </div>

      <HybridControl onOpened={onOpened} timeframe={scanTf} />

      {error && <div className="mb-2 rounded border border-bear/40 bg-bear/10 px-2 py-1 text-xs text-bear">{error}</div>}

      {!listOpen ? (
        items && (
          <div className="text-xs text-neutral-500">
            {items.length} setups hidden — click “Opportunities” to expand.
          </div>
        )
      ) : !items ? (
        <div className="text-sm text-neutral-500">
          Press <span className="font-semibold text-neutral-300">Scan watchlist</span> to analyze every
          pair and see the best setups ranked, with one-click open.
        </div>
      ) : items.length === 0 ? (
        <div className="text-sm text-neutral-500">No enabled watchlist pairs to scan.</div>
      ) : (
        <div className="max-h-[32rem] space-y-2 overflow-y-auto pr-1">
          {items.map((o, i) => {
            const dir = DIR[o.direction] ?? DIR.no_trade;
            const actionable = o.direction === "long" || o.direction === "short";
            const best = i === 0 && actionable && o.risk_approved && !o.already_open;
            const canOpen = actionable && o.risk_approved && !o.already_open;
            return (
              <div
                key={`${o.symbol}-${o.timeframe}`}
                className={`rounded-md border px-3 py-2 ${
                  best ? "border-blue-500/60 bg-blue-500/5" : "border-neutral-800 bg-neutral-900/40"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => onSelect?.({ symbol: o.symbol, asset_class: o.asset_class })}
                    className="font-semibold hover:text-blue-400 hover:underline"
                    title="Open on the chart"
                  >
                    {o.symbol}
                  </button>
                  <span className="text-xs text-neutral-500">{o.timeframe}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${dir.cls}`}>
                    {o.watch ? "WATCHING" : dir.label}
                  </span>
                  <RegimeBadge regime={o.regime} strategy={o.strategy} />
                  {actionable && (
                    <span className="text-xs tabular-nums text-neutral-400">
                      conf {(o.confidence * 100).toFixed(0)}%{o.rr ? ` · ${o.rr.toFixed(1)}R` : ""}
                    </span>
                  )}
                  {o.already_open && (
                    <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-400">
                      already open
                    </span>
                  )}
                  {best && (
                    <span className="rounded bg-blue-600 px-1.5 py-0.5 text-[10px] font-bold uppercase text-white">
                      best
                    </span>
                  )}
                  {canOpen && (
                    <button
                      onClick={() => open(o)}
                      disabled={openingKey !== null}
                      className="btn ml-auto bg-bull/20 text-bull hover:bg-bull/30"
                    >
                      {openingKey === `${o.symbol}-${o.timeframe}` ? "Opening…" : "Open"}
                    </button>
                  )}
                </div>
                {o.events_soon && (
                  <div
                    className="mt-1 text-xs text-warn/90"
                    title="Upcoming medium/high-impact events — a heads-up only. It does NOT block the trade; the hard stand-aside is high-impact events only."
                  >
                    📅 {o.events_soon}
                  </div>
                )}
                {actionable && o.entry != null && (
                  <div className="mt-1 text-xs tabular-nums text-neutral-400">
                    entry {fmtPrice(o.entry)} · SL <span className="text-bear">{fmtPrice(o.stop_loss)}</span> · TP{" "}
                    <span className="text-bull">{fmtPrice(o.take_profit)}</span>
                  </div>
                )}
                {actionable && o.lots != null && o.lots > 0 && (
                  <div className="mt-0.5 text-xs tabular-nums">
                    <span className="text-neutral-500">would open</span>{" "}
                    <span className="text-neutral-200">{o.lots} lots</span>
                    {o.risk_usd != null && <> · risk <span className="text-bear">{fmtUsd(o.risk_usd)}</span></>}
                    {o.reward_usd != null && <> · reward <span className="text-bull">{fmtUsd(o.reward_usd)}</span></>}
                  </div>
                )}
                <p className="mt-1 text-xs leading-relaxed text-neutral-500">
                  {!o.risk_approved && o.risk_reason ? `Risk: ${o.risk_reason}. ` : ""}
                  {o.rationale}
                </p>
                <div className="mt-2">
                  <ReviewExplanation rationale={o.rationale} />
                </div>
                {o.conditional && !o.already_open && (
                  <div className="mt-2">
                    <ArmSetupButton
                      symbol={o.symbol}
                      assetClass={o.asset_class}
                      timeframe={o.timeframe}
                      conditional={o.conditional}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Documented Hybrid ranges (RISK.md): check interval 30–90 min, confidence threshold 50–95%.
// We clamp the editor to these so the controls can't be pushed somewhere reckless. The money
// limits (per-trade, daily-loss, exposure, position count, no-stacking) apply regardless.
const HYB_MIN_MINUTES = 30;
const HYB_MAX_MINUTES = 90;
const HYB_MIN_CONF = 50;
const HYB_MAX_CONF = 95;
const clampInt = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, Math.round(v)));

// Hybrid auto-pilot: one toggle, plus adjustable check interval and confidence threshold. When
// on, every N min it opens the single best watchlist setup above the threshold if there's room —
// all risk gates still apply.
function HybridControl({ onOpened, timeframe }: { onOpened?: () => void; timeframe?: string }) {
  const [bump, setBump] = useState(0);
  const { data: state } = usePolling(() => api.hybridState(), 15000, [bump]);
  const { data: stats } = usePolling(() => api.hybridStats(), 20000, [bump]);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ interval: "", conf: "", cond: true, armed: "3" });
  const refresh = () => setBump((b) => b + 1);

  const on = state?.enabled ?? false;
  const intervalMin = state ? Math.round(state.interval_seconds / 60) : 35;
  const confPct = state ? Math.round(state.min_confidence * 100) : 70;
  const condOn = state?.conditional_enabled ?? true;

  const toggle = async () => {
    setBusy(true);
    try {
      await api.setHybridConfig({ enabled: !on });
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    try {
      await api.hybridRun(timeframe || undefined);
      refresh();
      onOpened?.();
    } finally {
      setBusy(false);
    }
  };

  // Open the editor seeded with the live values (a snapshot — polling can't overwrite it).
  const openEditor = () => {
    setForm({
      interval: String(intervalMin), conf: String(confPct),
      cond: state?.conditional_enabled ?? true, armed: String(state?.max_armed ?? 3),
    });
    setEditing(true);
  };

  const save = async () => {
    const mins = clampInt(Number(form.interval) || intervalMin, HYB_MIN_MINUTES, HYB_MAX_MINUTES);
    const conf = clampInt(Number(form.conf) || confPct, HYB_MIN_CONF, HYB_MAX_CONF);
    setBusy(true);
    try {
      await api.setHybridConfig({
        interval_seconds: mins * 60, min_confidence: conf / 100,
        conditional_enabled: form.cond, max_armed: clampInt(Number(form.armed) || 3, 0, 10),
      });
      refresh();
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  // What the (clamped) confidence in the editor will actually be saved as — drives the warning.
  const draftConf = clampInt(Number(form.conf) || confPct, HYB_MIN_CONF, HYB_MAX_CONF);

  return (
    <div
      className={`mb-2 rounded-md border px-3 py-2 ${
        on ? "border-bull/50 bg-bull/5" : "border-neutral-800 bg-neutral-900/40"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">🤖 Hybrid auto-pilot</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
            on ? "bg-bull/20 text-bull" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          {on ? "ON" : "OFF"}
        </span>
        <button
          onClick={openEditor}
          disabled={busy}
          className="btn ml-auto bg-neutral-700 text-neutral-100 hover:bg-neutral-600"
          title="Adjust the check interval and confidence threshold"
        >
          ⚙ Settings
        </button>
        <button
          onClick={toggle}
          disabled={busy}
          className={`btn text-white ${on ? "bg-bear/80 hover:bg-bear" : "bg-bull/80 hover:bg-bull"}`}
        >
          {on ? "Turn off" : "Activate"}
        </button>
        {on && (
          <button onClick={runNow} disabled={busy} className="btn bg-neutral-700 text-white hover:bg-neutral-600">
            {busy ? "…" : "Run now"}
          </button>
        )}
      </div>

      <p className="mt-1 text-xs text-neutral-500">
        Every ~{intervalMin} min, if fewer than 3 trades are open, it scans the watchlist and
        auto-opens the single best setup above <span className="text-neutral-300">{confPct}%</span>{" "}
        confidence. Kill-switch, daily-loss, exposure & no-stacking limits still apply.
        {condOn && (
          <> It also <span className="text-amber-300">arms “wait for the break”</span> setups that
          are blocked by structure, re-checking + opening them when the level gives way.</>
        )}
      </p>

      {editing && (
        <div className="mt-2 rounded-md border border-neutral-700 bg-neutral-900/60 p-3">
          <div className="flex flex-wrap items-end gap-4">
            <label className="text-xs text-neutral-400">
              <div className="mb-1">Check every (min)</div>
              <input
                name="hybrid-interval"
                autoComplete="off"
                inputMode="numeric"
                value={form.interval}
                onChange={(e) => setForm((f) => ({ ...f, interval: e.target.value }))}
                onKeyDown={(e) => e.key === "Enter" && save()}
                className="w-24 rounded bg-neutral-800 px-2 py-1.5 text-sm tabular-nums text-neutral-100"
              />
              <div className="mt-1 text-[10px] text-neutral-600">{HYB_MIN_MINUTES}–{HYB_MAX_MINUTES} min</div>
            </label>
            <label className="text-xs text-neutral-400">
              <div className="mb-1">Min confidence (%)</div>
              <input
                name="hybrid-conf"
                autoComplete="off"
                inputMode="numeric"
                value={form.conf}
                onChange={(e) => setForm((f) => ({ ...f, conf: e.target.value }))}
                onKeyDown={(e) => e.key === "Enter" && save()}
                className="w-24 rounded bg-neutral-800 px-2 py-1.5 text-sm tabular-nums text-neutral-100"
              />
              <div className="mt-1 text-[10px] text-neutral-600">{HYB_MIN_CONF}–{HYB_MAX_CONF}%</div>
            </label>
            <label className="text-xs text-neutral-400">
              <div className="mb-1">Max armed</div>
              <input
                name="hybrid-armed"
                autoComplete="off"
                inputMode="numeric"
                value={form.armed}
                onChange={(e) => setForm((f) => ({ ...f, armed: e.target.value }))}
                onKeyDown={(e) => e.key === "Enter" && save()}
                disabled={!form.cond}
                className="w-20 rounded bg-neutral-800 px-2 py-1.5 text-sm tabular-nums text-neutral-100 disabled:opacity-40"
              />
              <div className="mt-1 text-[10px] text-neutral-600">0–10 pending</div>
            </label>
            <label className="flex items-center gap-2 text-xs text-neutral-300">
              <input
                type="checkbox"
                checked={form.cond}
                onChange={(e) => setForm((f) => ({ ...f, cond: e.target.checked }))}
                className="h-4 w-4 accent-amber-500"
              />
              Arm conditional break-entries
            </label>
            <div className="flex gap-2">
              <button onClick={save} disabled={busy} className="btn bg-bull/80 text-white hover:bg-bull">
                {busy ? "…" : "Save"}
              </button>
              <button
                onClick={() => setEditing(false)}
                disabled={busy}
                className="btn bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
              >
                Cancel
              </button>
            </div>
          </div>
          {draftConf < 70 && (
            <p className="mt-2 text-[11px] leading-relaxed text-warn">
              Below the 70% default — Hybrid will auto-open lower-conviction setups. Every money
              limit (≤3% per trade, daily-loss, exposure, position count) still applies.
            </p>
          )}
        </div>
      )}

      {on && (state?.last_result ? (
        <div className="mt-1 text-xs text-neutral-400">
          Last check{state.last_run_at ? ` (${new Date(state.last_run_at).toLocaleTimeString()})` : ""}:{" "}
          {state.last_result}
        </div>
      ) : (
        <div className="mt-1 text-xs text-neutral-500">
          No check yet with these settings — press <span className="text-neutral-300">Run now</span> to scan immediately.
        </div>
      ))}

      {stats && <HybridActivity s={stats} />}
    </div>
  );
}

// A compact "today's activity" funnel for the Hybrid auto-pilot: scan → candidates → AI review →
// open / arm → trigger. All counters reset at UTC midnight (server side).
function HybridActivity({ s }: { s: import("../types").HybridStats }) {
  // Colour "Skipped" amber/red when the auto-pilot is passing on a large share of the real setups it
  // saw today — a hint the confidence threshold may be too high for current conditions. Needs a small
  // sample (≥3 real setups) before colouring, so a lone 1/1 doesn't flash red.
  const totalSetups = s.candidates + s.skipped_low_conf;
  const skipRatio = totalSetups >= 3 ? s.skipped_low_conf / totalSetups : 0;
  const skipTone =
    skipRatio >= 0.85 ? "text-bear" : skipRatio >= 0.6 ? "text-amber-300" : "text-neutral-400";
  const cells: { label: string; value: number; tone: string; title: string }[] = [
    { label: "Scans", value: s.scans, tone: "text-neutral-200",
      title: "Watchlist scans the auto-pilot ran today" },
    { label: "Candidates", value: s.candidates, tone: "text-sky-300",
      title: "Risk-approved setups that cleared the confidence threshold (the ranking pool)" },
    { label: "AI confirmed", value: s.ai_confirmed, tone: "text-bull",
      title: "The best candidate's LLM review said CONFIRM" },
    { label: "AI rejected", value: s.ai_rejected, tone: "text-bear",
      title: "The best candidate's LLM review said VETO" },
    { label: "Direct trades", value: s.direct_trades, tone: "text-bull",
      title: "Market orders the Hybrid auto-opened" },
    { label: "Armed", value: s.armed_setups, tone: "text-amber-300",
      title: "'Wait for the break' conditionals the Hybrid armed" },
    { label: "Triggered", value: s.triggered_armed, tone: "text-amber-200",
      title: "Armed setups whose level broke and fired" },
    { label: "Skipped <thr", value: s.skipped_low_conf, tone: skipTone,
      title: "Real setups skipped for being below the confidence threshold. Amber ≥60% / red ≥85% of today's real setups skipped — the threshold may be too high for current conditions" },
  ];
  // Colour the acceptance rate: green = AI broadly agrees with the engine (coherent pipeline),
  // amber = it's filtering meaningfully, red = it's vetoing most setups (engine/AI friction — look).
  const rate = s.accept_rate;
  const acceptTone =
    rate == null ? "text-neutral-300"
    : rate >= 0.66 ? "text-bull"
    : rate >= 0.4 ? "text-amber-300"
    : "text-bear";
  return (
    <div className="mt-2 rounded-md border border-neutral-800 bg-neutral-900/40 p-2">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-neutral-500">
        Auto-pilot activity · today
      </div>
      <div className="grid grid-cols-4 gap-1.5">
        {cells.map((c) => (
          <div key={c.label} title={c.title}
               className="rounded bg-neutral-800/60 px-2 py-1.5 text-center">
            <div className={`text-lg font-bold tabular-nums leading-none ${c.tone}`}>{c.value}</div>
            <div className="mt-1 text-[10px] leading-tight text-neutral-500">{c.label}</div>
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 text-[11px] text-neutral-500">
        <span title="Of the setups the AI graded today, the share it CONFIRMED (confirmed ÷ reviewed). Green ≥66% · amber 40–66% · red <40%">
          AI accept:{" "}
          <span className={`font-semibold ${acceptTone}`}>
            {s.accept_rate == null ? "—" : `${Math.round(s.accept_rate * 100)}%`}
          </span>
          {s.accept_rate != null && (
            <span className="text-neutral-600"> ({s.ai_confirmed}/{s.ai_confirmed + s.ai_rejected})</span>
          )}
        </span>
        <span title="The most recent trade the auto-pilot opened">
          Last opened:{" "}
          <span className="font-semibold text-neutral-300">{s.last_opened ?? "—"}</span>
          {s.last_opened_at && (
            <span className="text-neutral-600"> · {new Date(s.last_opened_at).toLocaleString()}</span>
          )}
        </span>
      </div>
    </div>
  );
}
