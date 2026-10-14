import { useEffect, useState } from "react";
import { api } from "../api/client";
import { AccountAlerts, AccountKpis } from "./AccountBar";
import { NavTabs, type NavItem } from "./Tabs";
import type { AccountState, PositionView, RiskState, SettingsResponse } from "../types";

interface Props {
  settings: SettingsResponse | null;
  account: AccountState | null;
  risk: RiskState | null;
  positions: PositionView[] | null;
  nav: NavItem[];
  onKillSwitchChange: () => void;
  onOpenSettings: () => void;
}

/**
 * The ONE header bar, in the order a trader's eye scans it:
 *   identity (PAPER/LIVE, mode)  ·  the account (equity, day P&L, slots, exposure, breaker)
 *   ·  where to go (Desk, Scanner, Risk, Journal, Backtest)  ·  the controls (settings, kill, power).
 *
 * It replaces three stacked bands (title bar, tab bar, account card) that together cost ~170px of
 * height before the chart even started. Below 1280px the account strip drops to a second line of
 * the same bar instead of being squeezed.
 *
 * Alert strips (live auto-execution, MT5 refusing orders, kill-switch, paused) hang under it and
 * stay sticky with it.
 */
export function Header({ settings, account, risk, positions, nav, onKillSwitchChange, onOpenSettings }: Props) {
  const [busy, setBusy] = useState(false);
  // Two-step power off: the first click arms it, the second does it. A single click on a control
  // that stops trade management is too easy to hit by accident.
  const [armed, setArmed] = useState(false);
  const [stopping, setStopping] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);   // don't leave it armed to be hit later
    return () => clearTimeout(t);
  }, [armed]);

  const powerOff = async () => {
    if (!armed) { setArmed(true); return; }
    setStopping(true);
    try {
      const r = await api.appShutdown();
      // The server exits right after replying, so this is the last thing the UI can say.
      document.body.innerHTML =
        `<div style="font:15px system-ui;padding:3rem;text-align:center;color:#a3a3a3;background:#0a0a0a;height:100vh">` +
        `<div style="margin-bottom:1rem">${powerSvgMarkup(32)}</div>` +
        `<div style="color:#e5e5e5;font-weight:600;margin-bottom:.5rem">AI Trading Desk is stopped</div>` +
        `<div>${r.open_positions > 0 ? `${r.open_positions} open position(s) and ` : ""}${r.armed_setups} armed setup(s) are no longer being managed.<br>` +
        `Broker-side stop-losses still stand.</div>` +
        `<div style="margin-top:1.5rem;font-size:13px">To start it again, run <b>Start AI Trading Desk.bat</b>.</div></div>`;
    } catch {
      setStopping(false);
      setArmed(false);
    }
  };


  const env = settings?.app.broker_env ?? "paper";
  const isLive = env === "live";
  const mode = settings?.app.execution_mode ?? "A_PROPOSE_APPROVE";
  const killEngaged = settings?.app.kill_switch_engaged ?? false;
  const envKill = settings?.env_kill_switch ?? false;
  const effectiveKill = killEngaged || envKill;
  const liveAutoExec = mode === "C_AUTO_LIVE" && isLive;

  const toggleKill = async () => {
    setBusy(true);
    try {
      await api.setKillSwitch(!killEngaged);
      onKillSwitchChange();
    } finally {
      setBusy(false);
    }
  };

  return (
    // NOT sticky itself: App wraps this in the sticky stack and measures it (--hdr-h), so the alert
    // strips below change the stack's height instead of overlapping whatever sits under it.
    <div>
      <header className="border-b border-neutral-800 bg-neutral-900/90 backdrop-blur supports-[backdrop-filter]:bg-neutral-900/75">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-1.5 xl:flex-nowrap 2xl:gap-x-4">
          {/* Identity */}
          <div className="flex shrink-0 items-center gap-2">
            {/* 64px source rendered at 24px so it stays sharp on high-DPI screens. The artwork
                already has its own rounded card + transparent corners, so no background here. */}
            <img
              src="/logo-64.png"
              alt="AI Trading Desk"
              width={24}
              height={24}
              className="h-6 w-6 shrink-0 rounded-md shadow-card"
            />
            <span className="hidden text-sm font-semibold tracking-tight 2xl:inline">Trading Desk</span>
            <span
              className={`rounded-md px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ${
                isLive ? "bg-bear text-white" : "bg-bull/15 text-bull ring-1 ring-inset ring-bull/30"
              }`}
              title={isLive ? "Connected to a REAL-money account" : "Paper / demo account — no real money"}
            >
              {isLive ? "LIVE" : "PAPER"}
            </span>
            <span
              className={`whitespace-nowrap rounded-md px-2 py-0.5 text-[11px] ${
                mode === "C_AUTO_LIVE" ? "bg-bear/20 text-bear" : "bg-neutral-800 text-neutral-400"
              }`}
              title={modeLabel(mode)}
            >
              {shortModeLabel(mode)}
            </span>
          </div>

          <span className="hidden h-7 w-px shrink-0 bg-neutral-800 xl:block" aria-hidden="true" />

          {/* The account. Its own line below xl, so nothing in it is ever squeezed or hidden. */}
          <div className="order-last w-full min-w-0 overflow-x-auto border-t border-neutral-800/70 pt-1.5 xl:order-none xl:w-auto xl:flex-1 xl:border-0 xl:pt-0">
            <AccountKpis account={account} risk={risk} settings={settings} positions={positions} />
          </div>

          <div className="ml-auto min-w-0 overflow-x-auto xl:ml-0">
            <NavTabs items={nav} />
          </div>

          <span className="hidden h-7 w-px shrink-0 bg-neutral-800 sm:block" aria-hidden="true" />

          {/* Controls */}
          <div className="flex shrink-0 items-center gap-1">
            {/* The desktop window has no address bar and no Ctrl+Shift+R, so there is otherwise no
                way to reload after a UI rebuild. index.html is served `no-store`, so an ordinary
                reload already fetches the current bundle — the cache-buster below is belt-and-braces
                for any proxy or wrapper that ignores that header. */}
            <button
              onClick={() => {
                const url = new URL(window.location.href);
                url.searchParams.set("r", Date.now().toString(36));
                window.location.replace(url.toString());
              }}
              className="btn btn-ghost px-2"
              title="Reload the app (picks up a new build). Open trades and settings are unaffected — everything lives on the server."
              aria-label="Reload the app"
            >
              ↻
            </button>
            <button onClick={onOpenSettings} className="btn btn-ghost px-2" title="Settings" aria-label="Settings">
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.7}
                   strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3 1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
              </svg>
              <span className="hidden 2xl:inline">Settings</span>
            </button>
            <button
              onClick={toggleKill}
              disabled={busy || envKill}
              title={
                envKill
                  ? "Env KILL_SWITCH is on — cannot clear from UI"
                  : effectiveKill
                    ? "Kill-switch is ENGAGED: no new orders. Click to release it."
                    : "Halt ALL new orders at once (open positions keep being managed)"
              }
              className={`btn whitespace-nowrap ${
                effectiveKill
                  ? "border border-bear bg-bear/20 text-bear"
                  : "border border-neutral-700 text-neutral-200 hover:border-bear/60 hover:bg-bear/10 hover:text-bear"
              }`}
            >
              {effectiveKill ? "● Kill-switch on — release" : "Kill-switch"}
            </button>

            {/* Power off. Closing the app WINDOW deliberately leaves the backend running -- it is
                what monitors open positions, moves stops to breakeven, honours the daily-loss
                breaker and fires armed setups. So the only way to actually stop it was a .bat file
                outside the app. This is that switch, with the consequence stated: two clicks, and
                the second one names what stops being managed. */}
            <button
              onClick={() => void powerOff()}
              disabled={stopping}
              className={`btn whitespace-nowrap px-2 ${armed ? "border border-bear bg-bear/20 text-bear" : "btn-ghost"}`}
              title="Shut the app down completely (backend included). Your broker-side stop-losses stay in place, but nothing else is managed while it is off."
              aria-label={armed ? "Confirm shutdown" : "Shut the app down"}
            >
              {stopping ? (
                "Shutting down…"
              ) : (
                <>
                  <PowerIcon />
                  {armed && "Confirm shutdown"}
                </>
              )}
            </button>
          </div>
        </div>
      </header>

      {liveAutoExec && (
        <div className="bg-bear px-4 py-1.5 text-center text-sm font-semibold text-white">
          ⚠ LIVE AUTO-EXECUTION ACTIVE — orders place automatically with real money.
        </div>
      )}
      {/* MT5 refusing orders / kill-switch / paused — one strip per cause. */}
      <AccountAlerts account={account} risk={risk} settings={settings} />
    </div>
  );
}

// The power symbol, drawn rather than typed: "⏻" (U+23FB) is missing from the Windows 10 fonts and
// rendered as an empty box — on a destructive control, of all places. Same stroke style as the
// settings cog. The paths are shared with the shutdown page, which is plain innerHTML (no React).
const POWER_PATHS = ["M12 3v8", "M6.3 6.3a8 8 0 1 0 11.4 0"];

function PowerIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.8}
         strokeLinecap="round" aria-hidden="true">
      {POWER_PATHS.map((d) => <path key={d} d={d} />)}
    </svg>
  );
}

function powerSvgMarkup(px: number): string {
  return (
    `<svg viewBox="0 0 24 24" width="${px}" height="${px}" fill="none" stroke="currentColor" ` +
    `stroke-width="1.8" stroke-linecap="round" style="display:inline-block" aria-hidden="true">` +
    POWER_PATHS.map((d) => `<path d="${d}"/>`).join("") +
    `</svg>`
  );
}

function modeLabel(mode: string): string {
  switch (mode) {
    case "A_PROPOSE_APPROVE":
      return "Mode A · Propose & Approve";
    case "B_AUTO_PAPER":
      return "Mode B · Auto (Paper)";
    case "C_AUTO_LIVE":
      return "Mode C · Auto (Live)";
    default:
      return mode;
  }
}

// The header chip is short on purpose; the full name is its tooltip.
function shortModeLabel(mode: string): string {
  switch (mode) {
    case "A_PROPOSE_APPROVE":
      return "Mode A · Approve";
    case "B_AUTO_PAPER":
      return "Mode B · Auto paper";
    case "C_AUTO_LIVE":
      return "Mode C · Auto LIVE";
    default:
      return mode;
  }
}
