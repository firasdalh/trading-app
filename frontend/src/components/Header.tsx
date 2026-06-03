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
      <header className="flex items-center justify-between border-b border-neutral-800 bg-neutral-900 px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="text-lg font-semibold">AI Trading Desk</span>
          <span
            className={`rounded px-2 py-0.5 text-xs font-bold uppercase ${
              isLive ? "bg-bear text-white" : "bg-bull text-white"
            }`}
          >
            {isLive ? "LIVE" : "PAPER"}
          </span>
          <span className="rounded bg-neutral-800 px-2 py-0.5 text-xs text-neutral-300">
            {modeLabel(mode)}
          </span>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={onOpenSettings}
            className="btn border border-neutral-700 bg-neutral-800 text-neutral-100 hover:bg-neutral-700"
          >
            Settings
          </button>
          <button
            onClick={toggleKill}
            disabled={busy || envKill}
            title={envKill ? "Env KILL_SWITCH is on — cannot clear from UI" : ""}
            className={`btn border ${
              effectiveKill
                ? "border-bear bg-bear/20 text-bear"
                : "border-neutral-700 bg-neutral-800 text-neutral-100 hover:bg-neutral-700"
            }`}
          >
            {effectiveKill ? "● KILL-SWITCH ENGAGED — click to release" : "Kill-switch"}
          </button>
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
