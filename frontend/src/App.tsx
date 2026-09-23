import { useCallback, useState } from "react";
import { api } from "./api/client";
import { Header } from "./components/Header";
import { Dashboard, type DeskSection } from "./components/Dashboard";
import { BacktestView } from "./components/BacktestView";
import { JournalView } from "./components/JournalView";
import { SettingsPanel } from "./components/SettingsPanel";
import { Tabs, type TabDef } from "./components/Tabs";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { usePolling } from "./hooks/usePolling";

type View = DeskSection | "journal" | "backtest";

const DESK_SECTIONS: DeskSection[] = ["trade", "scan", "book", "risk"];

export default function App() {
  // A bump counter lets child actions (kill-switch toggle) force an immediate settings refetch.
  const [bump, setBump] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Persisted: reopening the app on the tab you were last working in is what every desk tool does.
  const [view, setView] = useLocalStorage<View>("ta.view", "trade");
  const { data: settings, error } = usePolling(() => api.settings(), 5000, [bump]);
  const refresh = useCallback(() => setBump((b) => b + 1), []);

  // Badge counts. These are the whole point of the tab bar: a decision waiting on you used to be
  // invisible until you scrolled to it. Polled here (not in Dashboard) so the badge is still right
  // while you are reading the Journal. Cheap endpoints, deliberately slower than the desk's own.
  const { data: openPositions } = usePolling(() => api.livePositions(), 8000, []);
  const { data: pending } = usePolling(
    () => api.proposals({ status: "pending_approval", limit: 50 }),
    8000,
    [],
  );

  const pendingN = pending?.length ?? 0;
  const openN = openPositions?.length ?? 0;

  const tabs: TabDef<View>[] = [
    { id: "trade", label: "Trade", hint: "Chart, analysis and manual entry for the selected pair" },
    { id: "scan", label: "Scan", hint: "Watchlist, opportunities, RSI extremes and the auto-trader" },
    {
      id: "book",
      label: "Book",
      count: openN,
      alert: pendingN,
      hint: "Open positions, exit advice, armed setups and anything awaiting your approval",
    },
    { id: "risk", label: "Risk", hint: "Risk limits, the AI-vs-deterministic scorecard and entry filters" },
    { id: "journal", label: "Journal", hint: "Closed trades and performance by source" },
    { id: "backtest", label: "Backtest", hint: "Replay the engine over historical candles" },
  ];

  const isDesk = (v: View): v is DeskSection => (DESK_SECTIONS as string[]).includes(v);

  return (
    <div className="min-h-screen">
      {/* ONE sticky stack: title bar, any live/kill banners, and the tab bar travel together.
          Sticking them separately needs hardcoded pixel offsets, which go wrong the moment a
          banner appears and changes the header's height. */}
      <div className="sticky top-0 z-20">
        <Header
          settings={settings}
          onKillSwitchChange={refresh}
          onOpenSettings={() => setSettingsOpen(true)}
        />
        <Tabs tabs={tabs} active={view} onChange={setView} />
      </div>
      {settingsOpen && (
        <SettingsPanel
          settings={settings}
          onClose={() => setSettingsOpen(false)}
          onChanged={refresh}
        />
      )}

      {error && (
        <div className="mx-auto max-w-7xl px-4 pt-4">
          <div className="rounded-lg border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
            Cannot reach backend: {error}. Is the API running on :8000?
          </div>
        </div>
      )}

      {/* The desk stays MOUNTED across its four sections: the chart, the pair you are on and the
          last analysis survive a tab switch, and its polling is not torn down and restarted each
          time. Journal and Backtest are separate views and may unmount freely. */}
      {isDesk(view) && (
        <Dashboard
          section={view}
          settings={settings}
          onSettingsChanged={refresh}
          onNavigate={setView}
        />
      )}
      {view === "journal" && <JournalView />}
      {view === "backtest" && <BacktestView />}
    </div>
  );
}
