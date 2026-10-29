import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useAlertWatcher } from "./hooks/priceAlerts";
import { api } from "./api/client";
import { Header } from "./components/Header";
import { Dashboard } from "./components/Dashboard";
import { BacktestView } from "./components/BacktestView";
import { DRAWER_IDS, Drawer, type DrawerId } from "./components/Drawer";
import { EntryFiltersPanel } from "./components/EntryFiltersPanel";
import { JournalView } from "./components/JournalView";
import { RiskDashboard } from "./components/RiskDashboard";
import { ScorecardPanel } from "./components/ScorecardPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { TargetCelebration, useTargetHits } from "./components/TargetCelebration";
import type { NavItem } from "./components/Tabs";
import { useLocalStorage } from "./hooks/useLocalStorage";
import { usePolling } from "./hooks/usePolling";
import type { AssetClass } from "./types";

/** Full-page views. Everything else is a drawer over the desk. */
type View = "desk" | "journal" | "backtest";
const VIEWS: View[] = ["desk", "journal", "backtest"];

// Values older layouts saved in `ta.view`. Trade/Scan/Book all live on the desk now, and Risk is a
// drawer — someone who last left the app on Risk gets the desk with the Risk drawer open.
const LEGACY_VIEWS: Record<string, View> = { trade: "desk", scan: "desk", book: "desk", risk: "desk" };

function initialDrawer(storedView: string): DrawerId | null {
  // ?drawer=monitor|scanner|risk|advisor opens a drawer straight from a link (and lets a headless browser
  // screenshot one, which cannot click).
  try {
    const q = new URLSearchParams(window.location.search).get("drawer");
    if (q && (DRAWER_IDS as string[]).includes(q)) return q as DrawerId;
  } catch {
    /* no URL API — ignore */
  }
  return storedView === "risk" ? "risk" : null;
}

export default function App() {
  // A bump counter lets child actions (kill-switch toggle) force an immediate settings refetch.
  const [bump, setBump] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Persisted: reopening the app on the view you were last working in is what every desk tool does.
  const [stored, setStored] = useLocalStorage<string>("ta.view", "desk");
  const view: View = LEGACY_VIEWS[stored] ?? ((VIEWS as string[]).includes(stored) ? (stored as View) : "desk");
  const [drawer, setDrawer] = useState<DrawerId | null>(() => initialDrawer(stored));
  // Rewrite a legacy value once, so the mapping above is a migration and not a permanent alias.
  useEffect(() => {
    if (stored !== view) setStored(view);
  }, [stored, view, setStored]);
  // Monitor, Scanner and Advisor are rendered BY the desk, so they can only be open while the desk is the
  // view (e.g. a ?drawer=scanner link opened while the app was last left on Journal).
  useEffect(() => {
    if (drawer && drawer !== "risk" && view !== "desk") setStored("desk");
  }, [drawer, view, setStored]);

  const { data: settings, error } = usePolling(() => api.settings(), 5000, [bump]);
  const refresh = useCallback(() => setBump((b) => b + 1), []);

  // The desk's asset class lives HERE (same `ta.assetClass` key as before) because the header's
  // account numbers are per broker, and the broker is chosen per asset class.
  const [assetClass, setAssetClass] = useLocalStorage<AssetClass>("ta.assetClass", "forex");

  // The live book, polled once and shared by the header (KPIs), the desk sidebar, the chart and the
  // drawers. `posBump` forces an immediate re-poll after anything that opens/closes/modifies a trade.
  const [posBump, setPosBump] = useState(0);
  const bumpPositions = useCallback(() => setPosBump((b) => b + 1), []);
  const { data: account, error: accountError } = usePolling(() => api.account(assetClass), 4000, [assetClass]);
  const { data: positions, error: positionsError } = usePolling(() => api.livePositions(), 4000, [posBump]);
  const { data: risk } = usePolling(() => api.riskState(), 5000, [bump]);
  // The approval badge. Polled here (not in the desk) so it is still right while you read the Journal.
  const { data: pending } = usePolling(
    () => api.proposals({ status: "pending_approval", limit: 50 }),
    8000,
    [posBump],
  );
  const pendingN = pending?.length ?? 0;
  // Whether the Hybrid auto-pilot is running — it opens trades on its own, so the nav says so even
  // while its controls are tucked away in the Scanner drawer.
  const { data: hybrid } = usePolling(() => api.hybridState(), 30000, [bump]);
  // Price alerts are checked HERE, once, so they fire whichever view is open (not only the desk).
  useAlertWatcher();
  // A trade closing at its take-profit gets a celebration - also watched here, on every view.
  const [targetHits, dismissHit] = useTargetHits(positions);
  // EVERYTHING that can act without a click, in one place. Only the Hybrid had a live dot; RSI-Over
  // auto-watch, the per-pair auto-trader, the advisor's auto-execute and armed setups were each
  // buried in their own panel (full review 2026-10-20).
  const { data: automation } = usePolling(async () => {
    const [rsi, auto, adv, arms] = await Promise.allSettled([
      api.rsiOverConfig(), api.autoTrade(), api.advisorState(), api.conditionals(),
    ]);
    const on: string[] = [];
    if (rsi.status === "fulfilled" && rsi.value.enabled) {
      on.push((rsi.value as { auto_approve?: boolean }).auto_approve ? "RSI-Over (auto-opens)" : "RSI-Over watch");
    }
    if (auto.status === "fulfilled" && auto.value.enabled && (auto.value.pairs ?? []).length) {
      on.push(`Auto-trade ×${auto.value.pairs.length}`);
    }
    if (adv.status === "fulfilled" && adv.value.enabled && adv.value.auto_execute) on.push("Advisor auto-execute");
    if (arms.status === "fulfilled") {
      const n = arms.value.filter((c) => c.status === "armed").length;
      if (n) on.push(`${n} armed`);
    }
    return on;
  }, 30000, [bump]);
  const automationOn = [...(hybrid?.enabled ? ["Hybrid"] : []), ...(automation ?? [])];

  // Publish the sticky header's real height as --hdr-h. The desk sidebar sticks under it and drawers
  // open below it; both would otherwise hard-code a height that changes whenever an alert strip
  // (kill-switch, paused, MT5) appears or the header wraps to two lines on a narrower window.
  const headerRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const set = () => document.documentElement.style.setProperty("--hdr-h", `${el.offsetHeight}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const goView = (v: View) => {
    setDrawer(null);
    setStored(v);
  };
  // Drawers toggle. Scanner and Advisor belong to the desk (they retune its chart), so opening one
  // from Journal/Backtest brings you back to the desk underneath it. Risk opens over any view.
  const toggleDrawer = (d: DrawerId) => {
    if (drawer === d) {
      setDrawer(null);
      return;
    }
    if (d !== "risk" && view !== "desk") setStored("desk");
    setDrawer(d);
  };
  const closeDrawer = useCallback(() => setDrawer(null), []);

  const paused = !!(account?.trading_paused || risk?.trading_paused);
  const nav: NavItem[] = [
    {
      id: "desk",
      label: "Desk",
      icon: "desk",
      kind: "view",
      active: view === "desk",
      onClick: () => goView("desk"),
      alert: pendingN,
      hint: "Chart, analysis, and your open positions and pending approvals beside it",
    },
    {
      id: "monitor",
      label: "Monitor",
      icon: "monitor",
      kind: "drawer",
      active: drawer === "monitor",
      onClick: () => toggleDrawer("monitor"),
      hint: "Every watched pair at a glance: 1H / 4H / 1D direction, RSI, and demand/supply zones (opens over the desk)",
    },
    {
      id: "scanner",
      label: "Scanner",
      icon: "scanner",
      kind: "drawer",
      active: drawer === "scanner",
      onClick: () => toggleDrawer("scanner"),
      live: !!hybrid?.enabled,
      hint:
        (hybrid?.enabled ? "Hybrid auto-pilot is ON. " : "") +
        "Hybrid auto-pilot, opportunities, RSI extremes and the per-pair auto-trader (opens over the desk)",
    },
    {
      id: "risk",
      label: "Risk",
      icon: "risk",
      kind: "drawer",
      active: drawer === "risk",
      onClick: () => toggleDrawer("risk"),
      danger: paused || !!risk?.entry_breaker,
      hint: "Risk limits and breakers, the AI-vs-deterministic scorecard and entry filters (opens over the page)",
    },
    {
      id: "journal",
      label: "Journal",
      icon: "journal",
      kind: "view",
      active: view === "journal",
      onClick: () => goView("journal"),
      hint: "Closed trades and performance by source",
    },
    {
      id: "backtest",
      label: "Backtest",
      icon: "backtest",
      kind: "view",
      active: view === "backtest",
      onClick: () => goView("backtest"),
      hint: "Replay the engine over historical candles",
    },
  ];

  return (
    <div className="min-h-screen">
      {/* ONE sticky stack: the header bar and any alert strips travel together. Sticking them
          separately needs hardcoded pixel offsets, which go wrong the moment a strip appears. */}
      <div ref={headerRef} className="sticky top-0 z-20">
        <Header
          settings={settings}
          account={account}
          risk={risk}
          positions={positions}
          nav={nav}
          automation={automationOn}
          onOpenAutomation={() => toggleDrawer("scanner")}
          onKillSwitchChange={refresh}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </div>
      {settingsOpen && (
        <SettingsPanel
          settings={settings}
          onClose={() => setSettingsOpen(false)}
          onChanged={refresh}
        />
      )}

      {/* The broker could not be read. usePolling keeps the LAST good data, so without this the desk
          showed hours-old positions and equity as if live when MT5 was down (full review 2026-10-20). */}
      {!error && (accountError || positionsError) && (
        <div className="px-3 pt-3">
          <div className="rounded-lg border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
            Broker unreachable - the positions and equity on screen are the LAST KNOWN values, not live.
            Check that MetaTrader 5 is running and logged in. ({positionsError || accountError})
          </div>
        </div>
      )}
      {error && (
        <div className="px-3 pt-3">
          <div className="rounded-lg border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
            Cannot reach backend: {error}. Is the API running?
          </div>
        </div>
      )}

      {/* The desk stays MOUNTED while a drawer is open over it: the chart, the pair you are on and
          the last analysis survive, and its polling is not torn down and restarted. Journal and
          Backtest are separate views and may unmount freely. */}
      {view === "desk" && (
        <Dashboard
          settings={settings}
          onSettingsChanged={refresh}
          assetClass={assetClass}
          setAssetClass={setAssetClass}
          account={account}
          positions={positions}
          posBump={posBump}
          onPositionsChanged={bumpPositions}
          pendingN={pendingN}
          drawer={drawer}
          onOpenDrawer={(d) => setDrawer(d)}
          onCloseDrawer={closeDrawer}
        />
      )}
      {view === "journal" && <JournalView />}
      {view === "backtest" && <BacktestView />}

      {/* Risk is configuration you change rarely and should never meet by accident mid-trade — so it
          is a pop-up you open on purpose, over whatever view you are on. */}
      <Drawer
        open={drawer === "risk"}
        title="Risk"
        subtitle="Limits and breakers, the AI-vs-deterministic scorecard, and the entry filters."
        onClose={closeDrawer}
      >
        <RiskDashboard risk={risk} account={account} settings={settings} onChanged={refresh} />
        <ScorecardPanel />
        <EntryFiltersPanel />
      </Drawer>

      {/* One at a time: two targets hit together celebrate back to back. */}
      <TargetCelebration hit={targetHits[0] ?? null} onDone={dismissHit} />
    </div>
  );
}
