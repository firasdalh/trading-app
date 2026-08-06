import { useState } from "react";
import { api } from "../api/client";
import type { SettingsResponse } from "../types";

interface Props {
  settings: SettingsResponse | null;
  onKillSwitchChange: () => void;
  onOpenSettings: () => void;
}

// Always-visible global header: paper/live indicator + the kill-switch.
// Surfaces a persistent red banner whenever live auto-execution (Mode C) is active.
export function Header({ settings, onKillSwitchChange, onOpenSettings }: Props) {
  const [busy, setBusy] = useState(false);

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
    <div className="sticky top-0 z-20">
      <header className="border-b border-neutral-800 bg-neutral-900/80 backdrop-blur supports-[backdrop-filter]:bg-neutral-900/60">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            {/* 64px source rendered at 24px so it stays sharp on high-DPI screens. The artwork
                already has its own rounded card + transparent corners, so no background here. */}
            <img
              src="/logo-64.png"
              alt="AI Trading Desk"
              width={24}
              height={24}
              className="h-6 w-6 shrink-0 rounded-md shadow-card"
            />

            <span className="text-base font-semibold tracking-tight">Trading Desk</span>
            <span
              className={`rounded-md px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ${
                isLive ? "bg-bear text-white" : "bg-bull/15 text-bull ring-1 ring-inset ring-bull/30"
              }`}
            >
              {isLive ? "LIVE" : "PAPER"}
            </span>
            <span className="hidden rounded-md bg-neutral-800 px-2 py-0.5 text-[11px] text-neutral-400 sm:inline">
              {modeLabel(mode)}
            </span>
          </div>

          <div className="flex items-center gap-2">
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
              className="btn btn-subtle"
              title="Reload the app (picks up a new build). Open trades and settings are unaffected — everything lives on the server."
              aria-label="Reload the app"
            >
              ↻
            </button>
            <button onClick={onOpenSettings} className="btn btn-subtle">
              Settings
            </button>
            <button
              onClick={toggleKill}
              disabled={busy || envKill}
              title={envKill ? "Env KILL_SWITCH is on — cannot clear from UI" : ""}
              className={`btn ${
                effectiveKill ? "border border-bear bg-bear/20 text-bear" : "btn-subtle"
              }`}
            >
              {effectiveKill ? "● Kill-switch engaged — release" : "Kill-switch"}
            </button>
          </div>
        </div>
      </header>

      {liveAutoExec && (
        <div className="bg-bear px-4 py-2 text-center text-sm font-semibold text-white">
          ⚠ LIVE AUTO-EXECUTION ACTIVE — orders place automatically with real money.
        </div>
      )}
      {effectiveKill && (
        <div className="bg-warn px-4 py-1.5 text-center text-sm font-medium text-black">
          Kill-switch is engaged: all new orders are halted.
        </div>
      )}
    </div>
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
