import { useCallback, useState } from "react";
import { api } from "./api/client";
import { Header } from "./components/Header";
import { Dashboard } from "./components/Dashboard";
import { BacktestView } from "./components/BacktestView";
import { JournalView } from "./components/JournalView";
import { SettingsPanel } from "./components/SettingsPanel";
import { usePolling } from "./hooks/usePolling";

type View = "dashboard" | "backtest" | "journal";

export default function App() {
  // A bump counter lets child actions (kill-switch toggle) force an immediate settings refetch.
  const [bump, setBump] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [view, setView] = useState<View>("dashboard");
  const { data: settings, error } = usePolling(() => api.settings(), 5000, [bump]);
  const refresh = useCallback(() => setBump((b) => b + 1), []);

  return (
    <div className="min-h-screen">
      <Header
        settings={settings}
        onKillSwitchChange={refresh}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {settingsOpen && (
        <SettingsPanel
          settings={settings}
          onClose={() => setSettingsOpen(false)}
          onChanged={refresh}
        />
      )}
      {error && (
        <div className="mx-auto max-w-7xl px-4 pt-4">
          <div className="rounded-md border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
            Cannot reach backend: {error}. Is the API running on :8000?
          </div>
        </div>
      )}
      <nav className="mx-auto flex max-w-7xl gap-1 px-4 pt-3">
        {(["dashboard", "backtest", "journal"] as View[]).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`btn capitalize ${
              view === v ? "bg-neutral-700 text-white" : "text-neutral-400 hover:text-white"
            }`}
          >
            {v}
          </button>
        ))}
      </nav>

      {view === "dashboard" && <Dashboard settings={settings} />}
      {view === "backtest" && <BacktestView />}
      {view === "journal" && <JournalView />}
    </div>
  );
}
