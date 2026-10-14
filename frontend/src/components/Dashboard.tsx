import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { assetLabel, displaySymbol, fmtPrice, fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import { useQuoteSocket } from "../hooks/useQuoteSocket";
import type {
  AccountState,
  AnalyzeResponse,
  AssetClass,
  PositionView,
  ProposalView,
  SettingsResponse,
} from "../types";

// Rebuild the panel's view-model from a stored proposal (so the last analysis persists
// across refresh / pair navigation; the live run returns the same shape directly).
function viewToResult(v: ProposalView): AnalyzeResponse {
  return {
    proposal_id: v.id,
    status: v.status,
    proposal: {
      symbol: v.symbol,
      asset_class: v.asset_class as AssetClass,
      timeframe: v.timeframe,
      direction: v.direction,
      entry: v.entry,
      stop_loss: v.stop_loss,
      take_profit: v.take_profit,
      confidence: v.confidence,
      rationale: v.rationale,
      watch: v.watch ?? false,
      review_decision: v.review_decision ?? null,
      technical: v.reasoning?.technical ?? null,
      fundamental: v.reasoning?.fundamental ?? null,
    },
    // When this analysis actually ran. Only the live analyze call sets `analyzed_at`, so without
    // this a RESTORED proposal (what you see after a reload, or when switching back to a pair)
    // showed no time at all — leaving no way to tell whether the read was 2 minutes or 2 days old.
    analyzed_at: v.created_at,
    risk: {
      decision: (v.risk_decision as "approved" | "resized" | "vetoed") ?? "vetoed",
      approved: ["pending_approval", "approved", "executed"].includes(v.status),
      reason: v.risk_reason ?? "",
      symbol: v.symbol,
      side: null,
      approved_qty: v.approved_qty ?? 0,
      risk_amount: v.risk_amount ?? 0,
      risk_pct_of_equity: 0,
      checks: {},
    },
  };
}
import { AdvisorActivity } from "./AdvisorActivity";
import { Chart } from "./Chart";
import { ChartPositionBar } from "./ChartPositionBar";
import { Drawer, type DrawerId } from "./Drawer";
import { OpenPositionsList, SideSection } from "./LiveBook";
import { RegimeBadge } from "./RegimeBadge";
import { ConditionalsPanel } from "./ConditionalsPanel";
import { OpportunitiesPanel } from "./OpportunitiesPanel";
import { RsiOverPanel } from "./RsiOverPanel";
import { PendingProposalsPanel } from "./PendingProposalsPanel";
import { ProposalPanel } from "./ProposalPanel";
import { AutoTradePanel } from "./AutoTradePanel";
import { PositionAdvicePanel } from "./PositionAdvicePanel";
import { PositionsTable } from "./PositionsTable";
import { SymbolPicker } from "./SymbolPicker";
import { WatchlistPanel } from "./WatchlistPanel";

interface Favorite {
  symbol: string;
  assetClass: AssetClass;
}

interface Props {
  settings: SettingsResponse | null;
  onSettingsChanged?: () => void;
  /** Owned by App (the header's account numbers depend on it); same `ta.assetClass` key as before. */
  assetClass: AssetClass;
  setAssetClass: (a: AssetClass) => void;
  /** The live book, polled once in App and shared with the header. */
  account: AccountState | null;
  positions: PositionView[] | null;
  /** Bumps whenever something opened/closed/modified a trade — panels use it to refresh at once. */
  posBump: number;
  onPositionsChanged: () => void;
  /** Pending approvals (App polls it for the nav badge; reused here for the sidebar badge). */
  pendingN: number;
  /** Which drawer is open. Scanner and Advisor render here because they drive this desk's chart. */
  drawer: DrawerId | null;
  onOpenDrawer: (d: DrawerId) => void;
  onCloseDrawer: () => void;
}

// This selector drives BOTH the chart and the analysis/trade the engine produces. 5m and 15m are
// included so you can drop down to time an entry, but they are marked: measured over 959 signals,
// 15m lost -0.159R per trade in BOTH halves of the data (the clearest negative result on this book)
// because the win rate falls AND the fixed spread eats roughly twice as much of a smaller stop. 5m
// is faster still. 1h is the tested baseline.
const TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"];
const FAST_TF = new Set(["5m", "15m"]);

// A plan is drawn on the chart (dashed lines + PLAN zone) only while it is still current: once 3+ bars
// of its timeframe have closed since the analysis — the panel's own "stale, re-run it" rule — its levels
// may no longer be true, and a 2,000-hour-old PLAN box looked exactly like a live idea.
const TF_MINUTES: Record<string, number> = { "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440 };
function planIsStale(analyzedAt: string | null | undefined, timeframe: string): boolean {
  if (!analyzedAt) return false;
  const iso = /[Z+]|[+-]\d\d:\d\d$/.test(analyzedAt) ? analyzedAt : `${analyzedAt}Z`;
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  return mins / (TF_MINUTES[timeframe] ?? 60) >= 3;
}
const ASSET_CLASSES: AssetClass[] = ["stock", "crypto", "forex", "metal", "energy", "index"];

/**
 * THE DESK — a trading-terminal layout, using the full window width.
 *
 *   ┌──────────────────────────────────────┬───────────────────┐
 *   │ toolbar: pair · class · TF · modes   │ OPEN POSITIONS    │
 *   │ chart (+ the open trade's strip)     │ WAITING FOR YOU   │
 *   │ ANALYSIS of the charted pair         │ WATCHLIST         │
 *   └──────────────────────────────────────┴───────────────────┘
 *
 * Why this shape: the old desk put the chart, then the analysis, then three tabs (Analysis / Scan /
 * Book) in one narrow column, so "what do I hold?" and "what is waiting for me?" were always one tab
 * switch away from whatever you were reading. Those two questions are now answered permanently by
 * the sidebar, which sticks beside the chart while you scroll the analysis. Things you consult
 * rather than work in — scanners, risk, the full advisor — open as drawers OVER the desk, so the
 * chart and the analysis are never unmounted to look at them.
 *
 * Below 1024px the sidebar drops under the chart (before the long analysis), so the book is still
 * the second thing you see.
 */
export function Dashboard({
  settings,
  onSettingsChanged,
  assetClass,
  setAssetClass,
  account,
  positions,
  posBump,
  onPositionsChanged,
  pendingN,
  drawer,
  onOpenDrawer,
  onCloseDrawer,
}: Props) {
  // Persisted across refresh / navigation so the desk reopens on the last pair you used.
  const [symbol, setSymbol] = useLocalStorage("ta.symbol", "EURUSD");
  const [timeframe, setTimeframe] = useLocalStorage("ta.timeframe", "1h");
  const [favorites, setFavorites] = useLocalStorage<Favorite[]>("ta.favorites", []);
  const [result, setResult] = useState<AnalyzeResponse | null>(null);
  const [scenario, setScenario] = useState<import("../types").AiScenarioRead | null>(null);
  const [scenarioBusy, setScenarioBusy] = useState(false);
  // The AI scenario's S/R lines pinned on the chart — one shared state so BOTH scenario cards (the
  // chart's floating one + the Run-analysis one) toggle the same lines. null = hidden.
  const [scenLevels, setScenLevels] = useState<{ support: number | null; resistance: number | null; target: number | null; invalidation: number | null } | null>(null);
  const toggleScenLevels = (lv: { support: number | null; resistance: number | null; target: number | null; invalidation: number | null } | null) =>
    // null clears; the same set clears (toggle off); a DIFFERENT set replaces (so the setup-levels
    // button and the AI-scenario button switch cleanly instead of just clearing each other).
    setScenLevels((prev) => {
      if (lv == null) return null;
      const same = prev != null && prev.support === lv.support && prev.resistance === lv.resistance
        && prev.target === lv.target && prev.invalidation === lv.invalidation;
      return same ? null : lv;
    });
  const [status, setStatus] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A manual stop moved past the per-trade risk: allowed (your call), but shown until dismissed.
  const [slTpWarning, setSlTpWarning] = useState<string | null>(null);
  const [stBandBusy, setStBandBusy] = useState(false);
  const [aiReviewBusy, setAiReviewBusy] = useState(false);

  const [symbols, setSymbols] = useState<string[]>([]);
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});

  // FULL SCREEN for the trading view — the position bar AND the chart block (price + RSI + MACD).
  // Owned here rather than inside <Chart> because the position strip is a sibling component: going
  // full screen without it would hide your live P&L and the Quick-close button at exactly the moment
  // you are studying the trade. Tracks `fullscreenchange` so Esc (which never routes through our
  // button) still leaves the layout correct.
  const chartWrapRef = useRef<HTMLDivElement>(null);
  const [chartFull, setChartFull] = useState(false);
  useEffect(() => {
    const onChange = () => setChartFull(document.fullscreenElement === chartWrapRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);
  // Leaving the desk (Journal / Backtest) unmounts the chart. The browser then drops full screen on
  // its own, but only AFTER the paint — which shows a frame of the next view stretched over the
  // whole monitor. Exiting first keeps the transition clean. (Opening a drawer keeps the desk
  // mounted, so it never reaches this.)
  useEffect(
    () => () => {
      if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    },
    [],
  );

  // Drawers stop at the sidebar's LEFT edge on a wide desk (Drawer reads --book-w), so opening the
  // scanner or the advisor never hides the open positions and their P&L. Measured, not hard-coded:
  // the sidebar is 19, 20 or 25rem depending on the breakpoint. Below lg the sidebar sits under the
  // chart, so there is nothing beside the drawer to keep clear; leaving the desk clears it too.
  const asideRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = asideRef.current;
    const root = document.documentElement;
    if (!el) return;
    const lg = window.matchMedia("(min-width: 1024px)");
    const update = () => {
      if (!lg.matches) {
        root.style.removeProperty("--book-w");
        return;
      }
      // + half the 0.75rem grid gap, so the drawer's edge sits in the gutter, not on the sidebar.
      const w = root.clientWidth - el.getBoundingClientRect().left + 6;
      root.style.setProperty("--book-w", `${Math.max(0, Math.round(w))}px`);
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    window.addEventListener("resize", update);
    lg.addEventListener("change", update);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
      lg.removeEventListener("change", update);
      root.style.removeProperty("--book-w");
    };
  }, []);

  const toggleChartFull = () => {
    const el = chartWrapRef.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    else void el.requestFullscreen?.().catch(() => {});   // denied -> stay inline
  };
  const liveQuote = useQuoteSocket(symbol, assetClass);
  const { data: brokerInfo } = usePolling(() => api.brokerInfo(assetClass), 6000, [assetClass]);
  const stBand = !!settings?.app.st_band_mode;
  const aiReview = !!settings?.app.ai_review_enabled;

  // Load the broker's available symbols for the chosen asset class. If the current symbol
  // isn't offered (e.g. switching to forex while on AAPL), jump to the first available one.
  useEffect(() => {
    let cancelled = false;
    api
      .symbols(assetClass)
      .then((r) => {
        if (cancelled) return;
        setSymbols(r.symbols);
        setDescriptions(r.descriptions ?? {});
        // Only fall back to the first symbol if the (persisted) one truly isn't offered here.
        if (r.symbols.length && !r.symbols.includes(symbol)) {
          setSymbol(r.symbols[0]);
        }
      })
      .catch(() => {
        setSymbols([]);
        setDescriptions({});
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetClass]);

  // Restore the latest saved proposal for this pair/timeframe (persists across refresh and
  // pair navigation). Replaces it with null if none, so stale lines from another pair don't
  // stretch the chart's price scale.
  useEffect(() => {
    let cancelled = false;
    setError(null);
    api
      .proposals({ symbol, timeframe, limit: 1 })
      .then((list) => {
        if (cancelled) return;
        if (list.length) {
          const r = viewToResult(list[0]);
          setResult(r);
          setStatus(r.status);
        } else {
          setResult(null);
          setStatus(null);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setResult(null);
          setStatus(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, assetClass, timeframe]);

  // Armed conditional setups — overlaid on the chart (trigger/SL/TP) for the charted symbol.
  // condKey forces an immediate re-poll after a quick-arm, so the new trigger/SL/TP lines show up
  // at once instead of on the next 10-second tick.
  const [condKey, setCondKey] = useState(0);
  // Live indicator read for the charted position, computed inside Chart (that's where the
  // candles are) and shown in the strip above it.
  const [pulse, setPulse] = useState<import("./Chart").Pulse | null>(null);
  const { data: conditionals } = usePolling(() => api.conditionals(), 10000, [condKey]);
  const armedLevels = (conditionals ?? []).filter((c) => c.status === "armed");
  // The advisor's latest read per position, for the verdict chip on each sidebar card. Read-only
  // GET; the advisor itself runs on the backend scheduler.
  const { data: advisor } = usePolling(() => api.advisorState(), 15000, [posBump]);

  const closePosition = async (p: { symbol: string; asset_class: string }) => {
    try {
      await api.liveClose(p.symbol, p.asset_class as AssetClass);
      onPositionsChanged(); // refresh immediately
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const setSlTp = async (
    p: { symbol: string; asset_class: string },
    sl: number | null,
    tp: number | null,
  ) => {
    try {
      const res = await api.setSlTp(p.symbol, p.asset_class as AssetClass, sl, tp);
      setSlTpWarning(res.warning ? `${p.symbol}: ${res.warning}` : null);
      onPositionsChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // Is the panel's executed setup actually still open at the broker? `positions` is the broker's
  // live (open-only) list, so an executed proposal whose symbol is absent has since closed (hit
  // stop/target, or closed manually/by the advisor). null = positions not loaded yet — keep the
  // stored status until we know, so it doesn't flicker to "closed" on first paint.
  const positionOpen = useMemo<boolean | null>(() => {
    if (!result || !positions) return null;
    const sym = result.proposal.symbol.toUpperCase();
    return positions.some((p) => p.symbol.toUpperCase() === sym);
  }, [positions, result]);

  // The armed 'wait for the break' setup for the analysed symbol, if any — so the analysis panel can
  // say "you already have a setup armed here" instead of looking like it contradicts it.
  const armedForResult = useMemo(() => {
    const sym = result?.proposal.symbol.toUpperCase();
    return sym ? armedLevels.find((c) => c.symbol.toUpperCase() === sym) ?? null : null;
  }, [armedLevels, result]);

  const chartedPosition =
    (positions ?? []).find((p) => p.symbol.toUpperCase() === symbol.toUpperCase()) ?? null;
  const chartedAdvice = chartedPosition
    ? (advisor?.advice ?? []).find(
        (a) => a.symbol.toUpperCase() === chartedPosition.symbol.toUpperCase() && a.direction === chartedPosition.direction,
      ) ?? null
    : null;
  // Per-trade risk cap (fraction of equity) and equity: a trade whose stop now risks more than the
  // cap is flagged on its sidebar card and on the chart strip.
  const riskCap = settings?.risk.risk_per_trade ?? null;
  const equity = account?.equity ?? null;
  // A read 3+ bars of its timeframe old: the chart already hides its plan; the analysis panel folds
  // it to one line and never offers its approval.
  const readStale = !!result && planIsStale(result.analyzed_at, result.proposal.timeframe);
  // The live quote, for "Path to target" — only while it is for the pair the read is about (on a
  // pair switch the previous pair's read is still in hand for a moment).
  const readLivePrice =
    liveQuote && result && liveQuote.symbol.toUpperCase() === result.proposal.symbol.toUpperCase()
      ? liveQuote.price
      : null;
  // The approval chime on/off, owned here so its switch can sit in the WAITING FOR YOU header (the
  // pending panel's own header, which used to carry it, is hidden in the sidebar).
  const [chime, setChime] = useLocalStorage("pending.sound", true);

  const toggleStBand = async () => {
    setStBandBusy(true);
    setError(null);
    try {
      await api.setStBandMode(!stBand);
      onSettingsChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStBandBusy(false);
    }
  };

  const toggleAiReview = async () => {
    setAiReviewBusy(true);
    setError(null);
    try {
      await api.setAiReview(!aiReview);
      onSettingsChanged?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAiReviewBusy(false);
    }
  };

  const runAnalysis = async () => {
    setAnalyzing(true);
    setError(null);
    setScenario(null);
    setScenLevels(null);   // clear pinned scenario lines from the previous read
    try {
      const res = await api.analyze(symbol, assetClass, timeframe);
      setResult(res);
      setStatus(res.status);
      // The AI two-scenario read is INFO-ONLY — the engine's decision never uses it — so it's no longer
      // auto-fetched on every analysis (that burned ~1500 AI tokens each time). It's loaded on demand
      // via the "Show AI scenarios" button below (loadScenarios).
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAnalyzing(false);
    }
  };

  // Opt-in AI scenario read for the CURRENT symbol (info-only; costs tokens) — fetched only on request.
  const loadScenarios = async () => {
    setScenarioBusy(true);
    try {
      setScenario(await api.scenarios(symbol, assetClass));
    } catch {
      setScenario(null);
    } finally {
      setScenarioBusy(false);
    }
  };

  // A 409 means the proposal's status changed under us (rejected / expired / executed). The
  // message carries the real status, so sync the panel to it — the Approve/Reject buttons then
  // disappear (they only show for pending_approval) — and show a calm note, not a raw error.
  const handleActionError = (e: unknown) => {
    const msg = e instanceof Error ? e.message : String(e);
    const m = msg.match(/status '(\w+)'/);
    if (m) {
      setStatus(m[1]);
      setError(`This proposal is no longer pending (it's ${m[1]}). Run a fresh analysis to trade it.`);
    } else {
      setError(msg);
    }
  };

  const approve = async (lots?: number | null) => {
    if (!result) return;
    setActionBusy(true);
    try {
      const updated = await api.approve(result.proposal_id, lots ?? null);
      setStatus(updated.status);
      onPositionsChanged();   // the sidebar and the approval badge should catch up at once
    } catch (e) {
      handleActionError(e);
    } finally {
      setActionBusy(false);
    }
  };

  const reject = async () => {
    if (!result) return;
    setActionBusy(true);
    try {
      const updated = await api.reject(result.proposal_id);
      setStatus(updated.status);
      onPositionsChanged();
    } catch (e) {
      handleActionError(e);
    } finally {
      setActionBusy(false);
    }
  };

  const favForClass = useMemo(
    () => favorites.filter((f) => f.assetClass === assetClass).map((f) => f.symbol),
    [favorites, assetClass],
  );

  const toggleFavorite = (s: string) => {
    setFavorites((prev) => {
      const exists = prev.some((f) => f.symbol === s && f.assetClass === assetClass);
      return exists
        ? prev.filter((f) => !(f.symbol === s && f.assetClass === assetClass))
        : [...prev, { symbol: s, assetClass }];
    });
  };

  const removeFavorite = (f: Favorite) => {
    setFavorites((prev) => prev.filter((x) => !(x.symbol === f.symbol && x.assetClass === f.assetClass)));
  };

  // The top of the chart area. Picking a pair from the sidebar while you are reading the analysis
  // further down scrolls back up to it — "show me this" should end with the chart on screen.
  const chartAreaRef = useRef<HTMLDivElement>(null);
  const revealChart = () => {
    const el = chartAreaRef.current;
    if (!el) return;
    const hdr = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--hdr-h")) || 0;
    if (el.getBoundingClientRect().top < hdr) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const openFavorite = (f: Favorite) => {
    if (f.assetClass !== assetClass) setAssetClass(f.assetClass);
    setSymbol(f.symbol);
  };

  // Every panel's "select a pair" routes through here: a watchlist row, an opportunity, an RSI
  // extreme, an armed setup, a position. Picking one means "show me this" — so it retunes the chart,
  // closes any drawer that was covering it, and brings the chart back on screen.
  const openPositionSymbol = (p: { symbol: string; asset_class: string }) => {
    if (p.asset_class !== assetClass) setAssetClass(p.asset_class as AssetClass);
    setSymbol(p.symbol);
    onCloseDrawer();
    revealChart();
  };

  const openN = (positions ?? []).length;
  const floatingTotal = (positions ?? []).reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
  const armedN = armedLevels.length;
  const maxPos = settings?.risk.max_open_positions ?? null;

  // Errors and the over-risk SL/TP warning. Shown at the top of the desk AND inside the advisor
  // drawer, because that drawer's SL/TP table is where the warning is usually triggered — a warning
  // that only appears after you close the pop-up is a warning you can miss.
  const notices: ReactNode = (
    <>
      {error && (
        <div className="flex items-start justify-between gap-3 rounded-md border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-bear/80 hover:text-bear" aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
      {slTpWarning && (
        <div className="flex items-start justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
          <span>⚠ {slTpWarning}</span>
          <button onClick={() => setSlTpWarning(null)} className="text-amber-300/80 hover:text-amber-200" aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
    </>
  );

  // Segmented timeframe switch: one click instead of a dropdown, used in the toolbar and in full
  // screen (where the toolbar is off-screen). Switching timeframe is the most common thing you do
  // while reading a chart.
  const timeframeSwitch = (
    <span
      className="inline-flex shrink-0 items-center rounded-md border border-neutral-700 bg-neutral-800/60 p-0.5"
      role="group"
      aria-label="Timeframe"
    >
      {TIMEFRAMES.map((t) => (
        <button
          key={t}
          onClick={() => setTimeframe(t)}
          aria-pressed={t === timeframe}
          title={
            FAST_TF.has(t)
              ? `${t} — backtested as loss-making for entries; fine for timing`
              : `Switch the chart and the analysis to ${t}`
          }
          className={`rounded px-2 py-0.5 text-xs font-medium tabular-nums transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50 ${
            t === timeframe
              ? "bg-brand-600 text-white"
              : FAST_TF.has(t)
                ? "text-neutral-500 hover:bg-neutral-700 hover:text-neutral-200"
                : "text-neutral-300 hover:bg-neutral-700 hover:text-white"
          }`}
        >
          {t}
        </button>
      ))}
    </span>
  );

  return (
    <>
      {/* `isolate` keeps the chart's own pop-overs (z-30/40 inside it) beneath the drawers and the
          sticky header, instead of competing with them in the page's stacking order. */}
      <div
        className="isolate grid grid-cols-1 gap-3 p-3 lg:grid-cols-[minmax(0,1fr)_19rem] lg:grid-rows-[auto_1fr]
          xl:grid-cols-[minmax(0,1fr)_20rem] 2xl:grid-cols-[minmax(0,1fr)_25rem]"
      >
        {/* ───────────── Centre, top: toolbar + chart ───────────── */}
        <div
          ref={chartAreaRef}
          className="min-w-0 space-y-3 lg:col-start-1 lg:row-start-1"
          style={{ scrollMarginTop: "calc(var(--hdr-h, 0px) + 12px)" }}
        >
          {notices}

          {/* Toolbar — one line on a desktop window: what you are looking at, how the engine
              decides, and the one action. */}
          <div className="card flex flex-wrap items-center gap-1.5 px-3 py-2 2xl:gap-2">
            <SymbolPicker
              value={symbol}
              symbols={symbols}
              descriptions={descriptions}
              favorites={favForClass}
              onChange={setSymbol}
              onToggleFavorite={toggleFavorite}
            />
            <select
              name="dash-asset-class"
              aria-label="Asset class"
              title="Asset class — picks the broker and the symbol list"
              value={assetClass}
              onChange={(e) => setAssetClass(e.target.value as AssetClass)}
              className="field py-1.5"
            >
              {ASSET_CLASSES.map((a) => (
                <option key={a} value={a}>
                  {assetLabel(a)}
                </option>
              ))}
            </select>
            {timeframeSwitch}
            {/* Named at the point of use, not buried in a doc: the cost of a fast timeframe is
                invisible on the chart but decisive in the results. */}
            {FAST_TF.has(timeframe) && (
              <span
                className="shrink-0 rounded bg-warn/10 px-1.5 py-0.5 text-[11px] text-warn"
                title="Backtested over 959 signals: 15m lost -0.159R per trade in both halves of the data — the win rate drops and the fixed spread eats about twice as much of a smaller stop. Fine for reading price; poor for trading."
              >
                ⚠ {timeframe}: timing only, tested loss-making
              </span>
            )}

            <span className="hidden h-6 w-px bg-neutral-800 2xl:block" aria-hidden="true" />

            <button
              onClick={toggleStBand}
              disabled={stBandBusy}
              aria-pressed={stBand}
              title="SuperTrend Strategy: trade the mechanical SuperTrend + EMA20-band breakout (long on a close above the band in an uptrend / short below it in a downtrend; stop trails the SuperTrend line). Overrides the AI decider while on."
              className={`shrink-0 rounded-md border px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50 ${
                stBand
                  ? "border-emerald-500 bg-emerald-500/15 text-emerald-300"
                  : "border-neutral-700 text-neutral-300 hover:bg-neutral-800"
              }`}
            >
              {/* The emoji marks go on narrower windows so the toolbar keeps to one line. */}
              <span className="hidden 2xl:inline">📈 </span>SuperTrend {stBand ? "ON" : "OFF"}
            </button>
            <button
              onClick={toggleAiReview}
              disabled={aiReviewBusy}
              aria-pressed={aiReview}
              title="AI DECIDES. ON: the deterministic engine does the full analysis (a decision brief with real levels, level strength, two scenarios, trend maturity + its own historical hit-rate) and the AI is the JUDGE — it picks the better scenario and decides open now / arm a pending order / stand aside. The deterministic Risk Manager still sizes + gates, and you approve in Mode A. Best with a non-reasoning model (gpt-4.1) at temp 0 for repeatable decisions. OFF (recommended default): the deterministic engine + 70% confidence gate decide; AI only reads fundamentals."
              className={`shrink-0 rounded-md border px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/50 ${
                aiReview
                  ? "border-violet-500 bg-violet-500/15 text-violet-300"
                  : "border-neutral-700 text-neutral-300 hover:bg-neutral-800"
              }`}
            >
              <span className="hidden 2xl:inline">🤖 </span>AI decides {aiReview ? "ON" : "OFF"}
              {!aiReview && <span className="hidden text-neutral-500 2xl:inline"> (deterministic)</span>}
            </button>

            <div className="ml-auto flex shrink-0 items-center gap-3">
              {brokerInfo &&
                (() => {
                  const configured = settings?.app.broker_map?.[assetClass];
                  const fallback = configured && configured !== brokerInfo.name;
                  return (
                    // The healthy case is shown only on wide windows (the header already says
                    // PAPER/LIVE); a FALLBACK is shown always, because it means orders are going to
                    // the simulator instead of the broker you configured.
                    <span
                      className={`rounded px-2 py-0.5 text-[11px] ${
                        fallback ? "bg-warn/20 text-warn" : "hidden bg-neutral-800 text-neutral-400 2xl:inline"
                      }`}
                      title={
                        fallback
                          ? `Configured '${configured}' is unavailable — using the simulator. Check broker keys / MT5 terminal.`
                          : "Active broker for this asset class"
                      }
                    >
                      {fallback ? `${configured}→${brokerInfo.name} (fallback)` : brokerInfo.name} ·{" "}
                      {brokerInfo.is_paper ? "paper" : "live"}
                    </span>
                  );
                })()}
              {liveQuote && (
                <span className="text-sm tabular-nums font-semibold text-neutral-100" title={`${liveQuote.symbol} live price`}>
                  {fmtPrice(liveQuote.price)}
                </span>
              )}
              <button onClick={runAnalysis} disabled={analyzing} className="btn btn-primary">
                {analyzing ? "Analyzing…" : "Run analysis"}
              </button>
            </div>
          </div>

          {/* Chart card */}
          <div className="card p-3">
            {/* Header + position strip + chart all travel together into full screen — see
                chartWrapRef above. */}
            <div
              ref={chartWrapRef}
              className={chartFull ? "flex h-screen flex-col overflow-hidden bg-neutral-950 p-3" : undefined}
            >
              <div className="mb-2 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2">
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="text-sm font-semibold">
                    {displaySymbol(symbol)} · {timeframe}
                  </span>
                  {descriptions[symbol] && (
                    <span className="hidden truncate text-xs text-neutral-500 md:inline">{descriptions[symbol]}</span>
                  )}
                </span>
                {result?.proposal?.regime && (
                  <RegimeBadge regime={result.proposal.regime} strategy={result.proposal.strategy} />
                )}
                {/* Full screen only: the toolbar with the real timeframe switch is off-screen. */}
                {chartFull && timeframeSwitch}
                {/* Favourites — how you move between the pairs you actually watch. In full screen
                    they are compact with no × : removing a favourite is housekeeping, not something
                    you do mid-read, and every extra × is a mis-click that silently drops a pair. */}
                {favorites.length > 0 && (
                  <span className="flex flex-wrap items-center gap-1">
                    <span className="text-xs text-neutral-500" title="Favourites">★</span>
                    {favorites.map((f) => {
                      const active = f.symbol === symbol && f.assetClass === assetClass;
                      return (
                        <span
                          key={`${f.assetClass}-${f.symbol}`}
                          className={`group flex items-center gap-1 rounded-full px-2 py-0.5 text-xs transition ${
                            active ? "bg-brand-600 text-white" : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"
                          }`}
                        >
                          <button onClick={() => openFavorite(f)} title={`Switch the chart to ${f.symbol} · ${f.assetClass}`}>
                            {displaySymbol(f.symbol)}
                          </button>
                          {!chartFull && (
                            <button
                              onClick={() => removeFavorite(f)}
                              title="Remove favourite"
                              aria-label={`Remove ${f.symbol} from favourites`}
                              className={active ? "text-blue-200 hover:text-white" : "text-neutral-500 hover:text-bear"}
                            >
                              ×
                            </button>
                          )}
                        </span>
                      );
                    })}
                  </span>
                )}
                {/* Open positions — quick-switch the chart between them. On a wide desk the sidebar
                    does this, so these only show in full screen (sidebar hidden) or on a narrow
                    window (sidebar below the chart). */}
                {(positions ?? []).length > 0 && (
                  <span className={`${chartFull ? "flex" : "flex lg:hidden"} flex-wrap items-center gap-1.5`}>
                    <span className="text-xs text-neutral-500">● Open</span>
                    {(positions ?? []).map((p) => {
                      const active = p.symbol.toUpperCase() === symbol.toUpperCase();
                      return (
                        <button
                          // By ticket: two trades on one pair and side (a scale-in) share symbol + direction.
                          key={p.id ?? `${p.symbol}-${p.direction}`}
                          onClick={() => openPositionSymbol(p)}
                          title={`Switch chart to ${p.symbol} (${p.direction})`}
                          className={`flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs ${
                            active ? "bg-brand-600 text-white" : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"
                          }`}
                        >
                          <span className={p.direction === "long" ? "text-bull" : "text-bear"}>
                            {p.direction === "long" ? "▲" : "▼"}
                          </span>
                          <span className="font-medium">{displaySymbol(p.symbol)}</span>
                          <span className={`tabular-nums ${p.unrealized_pnl >= 0 ? "text-bull" : "text-bear"}`}>
                            {fmtUsd(p.unrealized_pnl, { sign: true })}
                          </span>
                        </button>
                      );
                    })}
                  </span>
                )}
              </div>
              {/* Open-position resume for the charted symbol: P&L, risk/reward $, R:R + quick close */}
              <ChartPositionBar
                pos={chartedPosition}
                pulse={pulse}
                adviceR={chartedAdvice?.r_multiple ?? null}
                equity={equity}
                riskCap={riskCap}
                onClose={(p) => closePosition({ symbol: p.symbol, asset_class: p.asset_class })}
              />
              <Chart
                symbol={symbol}
                assetClass={assetClass}
                timeframe={timeframe}
                proposal={result && !readStale ? result.proposal : null}
                liveQuote={liveQuote}
                positions={positions}
                armed={armedLevels}
                onSetSlTp={(sl, tp) => setSlTp({ symbol, asset_class: assetClass }, sl, tp)}
                onSetArmedLevels={async (id, levels) => {
                  try {
                    await api.setConditionalLevels(id, levels);
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  }
                }}
                scenLevels={scenLevels}
                scenLevelsShown={!!scenLevels}
                onToggleScenLevels={toggleScenLevels}
                isFullscreen={chartFull}
                onToggleFullscreen={toggleChartFull}
                onArmed={() => setCondKey((v) => v + 1)}
                onOpened={onPositionsChanged}
                onPulse={setPulse}
              />
            </div>
            <p className="mt-2 text-[11px] text-neutral-600">
              Backtest and paper results do not guarantee live results.
            </p>
          </div>
        </div>

        {/* ───────────── Right sidebar: the live book ───────────── */}
        <aside
          ref={asideRef}
          className="min-w-0 space-y-3 lg:sticky lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:self-start lg:overflow-y-auto lg:overscroll-contain
            lg:top-[calc(var(--hdr-h,0px)_+_0.75rem)] lg:max-h-[calc(100vh_-_var(--hdr-h,0px)_-_1.5rem)]"
          aria-label="Open positions, pending decisions and watchlist"
        >
          <SideSection
            title="Open positions"
            hint="Every position open at the broker, live"
            body="bare"
            badges={[
              { n: openN, label: maxPos != null ? `/ ${maxPos}` : undefined, tone: "neutral", always: true },
            ]}
            actions={
              <>
                {openN > 0 && (
                  <span
                    className={`text-xs font-semibold tabular-nums ${
                      floatingTotal > 0 ? "text-bull" : floatingTotal < 0 ? "text-bear" : "text-neutral-400"
                    }`}
                    title="Total floating P&L of the open positions"
                  >
                    {fmtUsd(floatingTotal, { sign: true })}
                  </span>
                )}
                <button
                  onClick={() => onOpenDrawer("advisor")}
                  className="btn btn-ghost px-1.5 py-0.5 text-xs"
                  title="Position advisor: exit advice, the SL/TP table, and its recent actions"
                >
                  Advisor ›
                </button>
              </>
            }
          >
            <OpenPositionsList
              positions={positions}
              advice={advisor?.advice ?? null}
              activeSymbol={symbol}
              equity={equity}
              riskCap={riskCap}
              onChart={openPositionSymbol}
              onClose={closePosition}
              onAdvice={() => onOpenDrawer("advisor")}
            />
          </SideSection>

          {/* Decisions waiting on you, then setups armed to fire. Opens itself when something
              arrives and folds away when there is nothing, so it never pushes the watchlist down
              for no reason. The pending panel stays MOUNTED while folded — it plays the chime.
              Empty, it says so ONCE: the header already reads "nothing", and the old body added a
              nested "Pending approval 0" header and two more empty messages under it. */}
          <SideSection
            title="Waiting for you"
            hint="Risk-approved setups awaiting your approval, and armed 'wait for the break' setups"
            body="flush"
            autoOpen={pendingN + armedN > 0}
            empty="nothing"
            badges={[
              { n: pendingN, label: "to approve", tone: "warn" },
              { n: armedN, label: "armed", tone: "neutral" },
            ]}
            actions={
              <button
                onClick={() => setChime((v) => !v)}
                className={`rounded px-1.5 py-0.5 text-xs ${
                  chime ? "text-amber-300 hover:bg-neutral-800" : "text-neutral-600 hover:text-neutral-400"
                }`}
                title={chime
                  ? "Sound is ON — a chime plays when a new approval arrives. Click to mute."
                  : "Sound is OFF — new approvals arrive silently. Click to unmute."}
                aria-label={chime ? "Mute the approval chime" : "Unmute the approval chime"}
                aria-pressed={chime}
              >
                {chime ? "🔔" : "🔕"}
              </button>
            }
          >
            <PendingProposalsPanel embedded sound={chime} onSelect={openPositionSymbol} onChanged={onPositionsChanged} />
            {armedN > 0 && <ConditionalsPanel onSelect={openPositionSymbol} />}
            {pendingN + armedN === 0 && (
              <div className="px-3 py-2.5 text-xs text-neutral-500">
                Nothing to approve and nothing armed — new approvals land here{chime ? " with a chime" : ""}.
              </div>
            )}
          </SideSection>

          {/* The re-housed WatchlistPanel opens with its own bold "Watchlist" title, which would sit
              right under this section's WATCHLIST header. The selector hides only a
              `text-sm font-semibold` title div in that exact spot, so if the panel's markup ever
              changes the worst case is the duplicate title coming back — nothing else can vanish. */}
          <SideSection
            title="Watchlist"
            hint="Pairs the Hybrid auto-pilot scans — click one to chart it"
            body="flush"
            bodyClassName="[&>.card>div:first-child>div:first-child>div.text-sm.font-semibold:first-child]:hidden"
          >
            <WatchlistPanel
              currentSymbol={symbol}
              currentAsset={assetClass}
              currentTimeframe={timeframe}
              onSelect={(it) => {
                if (it.asset_class !== assetClass) setAssetClass(it.asset_class as AssetClass);
                setSymbol(it.symbol);
                if (it.timeframe) setTimeframe(it.timeframe);
                revealChart();
              }}
            />
          </SideSection>
        </aside>

        {/* ───────────── Centre, below the chart: the analysis ───────────── */}
        {/* The analysis gets the whole centre column, directly under the chart it explains. */}
        <div className="min-w-0 space-y-1 lg:col-start-1 lg:row-start-2">
          <div className="section-label">
            Analysis
            <span className="font-normal normal-case tracking-normal text-neutral-600">
              {displaySymbol(symbol)} · {timeframe}
            </span>
          </div>
          <ProposalPanel
            result={result}
            status={status}
            positionOpen={positionOpen}
            openPosition={chartedPosition}
            armedSetup={armedForResult}
            livePrice={readLivePrice}
            stale={readStale}
            onOpenAdvisor={() => onOpenDrawer("advisor")}
            busy={actionBusy}
            equity={account?.equity ?? null}
            onApprove={approve}
            onReject={reject}
            onRunAnalysis={runAnalysis}
            analyzing={analyzing}
            scenario={scenario}
            scenarioBusy={scenarioBusy}
            analysisLang={settings?.app.analysis_language}
            onLoadScenarios={loadScenarios}
            scenLevelsShown={!!scenLevels}
            onToggleScenLevels={toggleScenLevels}
          />
        </div>
      </div>

      {/* The advisor log also raises a browser notification when the advisor ACTS on a position
          (closes, trails, banks). As a Book tab it did that whenever the tab was showing; as a
          drawer it would only do it while the drawer is open. So while the drawer is closed, an
          invisible copy keeps polling for those notifications (it renders nothing visible). */}
      {drawer !== "advisor" && (
        <div className="hidden" aria-hidden="true">
          <AdvisorActivity refreshSignal={posBump} />
        </div>
      )}

      {/* ───────────── Drawers that drive this desk ───────────── */}
      {/* SCANNER — where candidates come from. Nothing here opens a trade by itself unless you
          tell it to; picking a pair charts it on the desk and closes the drawer. */}
      <Drawer
        open={drawer === "scanner"}
        title="Scanner"
        subtitle="Hybrid auto-pilot, opportunities, RSI extremes and the per-pair auto-trader. Picking a pair charts it on the desk."
        onClose={onCloseDrawer}
      >
        <OpportunitiesPanel onSelect={openPositionSymbol} onOpened={onPositionsChanged} />
        <RsiOverPanel onStaged={onPositionsChanged} onSelect={openPositionSymbol} />
        {/* No extra section label above it: the card's own header already reads "Auto-trade — pair",
            and a second, differently styled title made three header styles in one drawer. */}
        <AutoTradePanel symbol={symbol} assetClass={assetClass} timeframe={timeframe} onSelect={openPositionSymbol} />
      </Drawer>

      {/* ADVISOR — the full detail behind the sidebar's position cards. */}
      <Drawer
        open={drawer === "advisor"}
        title="Position advisor"
        subtitle="Exit advice for every open trade, the SL/TP table, and what the advisor has done."
        onClose={onCloseDrawer}
        widthClass="max-w-[56rem]"
      >
        {notices}
        <PositionAdvicePanel refreshSignal={posBump} lang={settings?.app.analysis_language} />
        <PositionsTable
          positions={positions}
          onClose={closePosition}
          onSetSlTp={setSlTp}
          onSelect={openPositionSymbol}
        />
        <AdvisorActivity refreshSignal={posBump} />
      </Drawer>
    </>
  );
}
