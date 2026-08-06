import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { assetLabel, displaySymbol, fmtPrice, fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import { useQuoteSocket } from "../hooks/useQuoteSocket";
import type { AnalyzeResponse, AssetClass, ProposalView, SettingsResponse } from "../types";

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
import { AccountBar } from "./AccountBar";
import { AdvisorActivity } from "./AdvisorActivity";
import { Chart } from "./Chart";
import { ChartPositionBar } from "./ChartPositionBar";
import { RegimeBadge } from "./RegimeBadge";
import { ConditionalsPanel } from "./ConditionalsPanel";
import { OpportunitiesPanel } from "./OpportunitiesPanel";
import { RsiOverPanel } from "./RsiOverPanel";
import { EntryFiltersPanel } from "./EntryFiltersPanel";
import { ScorecardPanel } from "./ScorecardPanel";
import { PendingProposalsPanel } from "./PendingProposalsPanel";
import { ProposalPanel } from "./ProposalPanel";
import { QuickTradePanel } from "./QuickTradePanel";
import { AutoTradePanel } from "./AutoTradePanel";
import { PositionAdvicePanel } from "./PositionAdvicePanel";
import { PositionsTable } from "./PositionsTable";
import { RiskDashboard } from "./RiskDashboard";
import { SymbolPicker } from "./SymbolPicker";
import { WatchlistPanel } from "./WatchlistPanel";

interface Favorite {
  symbol: string;
  assetClass: AssetClass;
}

interface Props {
  settings: SettingsResponse | null;
  onSettingsChanged?: () => void;
}

const TIMEFRAMES = ["15m", "1h", "4h", "1d"];
const ASSET_CLASSES: AssetClass[] = ["stock", "crypto", "forex", "metal", "energy", "index"];

export function Dashboard({ settings, onSettingsChanged }: Props) {
  // Persisted across refresh / navigation so the desk reopens on the last pair you used.
  const [symbol, setSymbol] = useLocalStorage("ta.symbol", "EURUSD");
  const [assetClass, setAssetClass] = useLocalStorage<AssetClass>("ta.assetClass", "forex");
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
  const [stBandBusy, setStBandBusy] = useState(false);
  const [aiReviewBusy, setAiReviewBusy] = useState(false);

  const [symbols, setSymbols] = useState<string[]>([]);
  const [descriptions, setDescriptions] = useState<Record<string, string>>({});
  const [posBump, setPosBump] = useState(0);
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

  const { data: account } = usePolling(() => api.account(assetClass), 4000, [assetClass]);
  const { data: positions } = usePolling(() => api.livePositions(), 4000, [posBump]);
  // Armed conditional setups — overlaid on the chart (trigger/SL/TP) for the charted symbol.
  const { data: conditionals } = usePolling(() => api.conditionals(), 10000, []);
  const armedLevels = (conditionals ?? []).filter((c) => c.status === "armed");

  const closePosition = async (p: { symbol: string; asset_class: string }) => {
    try {
      await api.liveClose(p.symbol, p.asset_class as AssetClass);
      setPosBump((b) => b + 1); // refresh immediately
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
      await api.setSlTp(p.symbol, p.asset_class as AssetClass, sl, tp);
      setPosBump((b) => b + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const { data: risk } = usePolling(() => api.riskState(), 5000, []);

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

  const openFavorite = (f: Favorite) => {
    if (f.assetClass !== assetClass) setAssetClass(f.assetClass);
    setSymbol(f.symbol);
  };

  const openPositionSymbol = (p: { symbol: string; asset_class: string }) => {
    if (p.asset_class !== assetClass) setAssetClass(p.asset_class as AssetClass);
    setSymbol(p.symbol);
  };

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-4">
      {/* Always-visible account header: equity, day P&L, slots/exposure used, paused/kill-switch */}
      <AccountBar account={account} risk={risk} settings={settings} positions={positions} />

      {/* Controls */}
      <div className="card flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Symbol</div>
          <SymbolPicker
            value={symbol}
            symbols={symbols}
            descriptions={descriptions}
            favorites={favForClass}
            onChange={setSymbol}
            onToggleFavorite={toggleFavorite}
          />
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Asset class</div>
          <select
            name="dash-asset-class"
            value={assetClass}
            onChange={(e) => setAssetClass(e.target.value as AssetClass)}
            className="field"
          >
            {ASSET_CLASSES.map((a) => (
              <option key={a} value={a}>
                {assetLabel(a)}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Timeframe</div>
          <select
            name="dash-timeframe"
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
            className="field disabled:opacity-50"
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={toggleStBand}
          disabled={stBandBusy}
          title="SuperTrend Strategy: trade the mechanical SuperTrend + EMA20-band breakout (long on a close above the band in an uptrend / short below it in a downtrend; stop trails the SuperTrend line). Overrides the AI decider while on."
          className={`self-end rounded-md border px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${
            stBand
              ? "border-emerald-500 bg-emerald-500/15 text-emerald-300"
              : "border-neutral-700 text-neutral-300 hover:bg-neutral-800"
          }`}
        >
          📈 SuperTrend {stBand ? "ON" : "OFF"}
        </button>
        <button
          onClick={toggleAiReview}
          disabled={aiReviewBusy}
          title="AI DECIDES. ON: the deterministic engine does the full analysis (a decision brief with real levels, level strength, two scenarios, trend maturity + its own historical hit-rate) and the AI is the JUDGE — it picks the better scenario and decides open now / arm a pending order / stand aside. The deterministic Risk Manager still sizes + gates, and you approve in Mode A. Best with a non-reasoning model (gpt-4.1) at temp 0 for repeatable decisions. OFF (recommended default): the deterministic engine + 70% confidence gate decide; AI only reads fundamentals."
          className={`self-end rounded-md border px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${
            aiReview
              ? "border-violet-500 bg-violet-500/15 text-violet-300"
              : "border-neutral-700 text-neutral-300 hover:bg-neutral-800"
          }`}
        >
          🤖 AI decides {aiReview ? "ON" : "OFF (deterministic)"}
        </button>
        <div className="ml-auto flex items-center gap-3">
          {brokerInfo &&
            (() => {
              const configured = settings?.app.broker_map?.[assetClass];
              const fallback = configured && configured !== brokerInfo.name;
              return (
                <span
                  className={`rounded px-2 py-0.5 text-xs ${
                    fallback ? "bg-warn/20 text-warn" : "bg-neutral-800 text-neutral-300"
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
            <span className="text-sm text-neutral-300">
              {liveQuote.symbol}{" "}
              <span className="tabular-nums font-semibold">{fmtPrice(liveQuote.price)}</span>
            </span>
          )}
          <button
            onClick={runAnalysis}
            disabled={analyzing}
            className="btn btn-primary"
          >
            {analyzing ? "Analyzing…" : "Run analysis"}
          </button>
        </div>
      </div>

      {favorites.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-neutral-500">★ Favourites</span>
          {favorites.map((f) => {
            const active = f.symbol === symbol && f.assetClass === assetClass;
            return (
              <span
                key={`${f.assetClass}-${f.symbol}`}
                className={`group flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                  active ? "bg-brand-600 text-white" : "bg-neutral-800 text-neutral-300 hover:bg-neutral-700"
                }`}
              >
                <button onClick={() => openFavorite(f)} title={`${f.symbol} · ${f.assetClass}`}>
                  {f.symbol}
                </button>
                <button
                  onClick={() => removeFavorite(f)}
                  title="Remove favourite"
                  className="text-neutral-500 hover:text-bear"
                >
                  ×
                </button>
              </span>
            );
          })}
        </div>
      )}

      {error && (
        <div className="rounded-md border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
          {error}
        </div>
      )}

      {/* Chart — full width so it gets the whole row (bigger, bordered) */}
      <div className="card border-2 border-neutral-700">
        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="text-sm font-semibold">
            {symbol} · {timeframe}
          </span>
          {result?.proposal?.regime && (
            <RegimeBadge regime={result.proposal.regime} strategy={result.proposal.strategy} />
          )}
          {/* Open positions — quick-switch the chart between them */}
          {(positions ?? []).length > 0 && (
            <span className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-neutral-500">● Open</span>
              {(positions ?? []).map((p) => {
                const active = p.symbol.toUpperCase() === symbol.toUpperCase();
                return (
                  <button
                    key={`${p.symbol}-${p.direction}`}
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
          pos={(positions ?? []).find((p) => p.symbol.toUpperCase() === symbol.toUpperCase()) ?? null}
          onClose={(p) => closePosition({ symbol: p.symbol, asset_class: p.asset_class })}
        />
        <Chart
          symbol={symbol}
          assetClass={assetClass}
          timeframe={timeframe}
          proposal={result?.proposal ?? null}
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
        />
        <p className="mt-2 text-xs text-neutral-500">
          Backtest and paper results do not guarantee live results.
        </p>
      </div>

      {/* Analysis (left) + Armed/pending setups (right), side by side below the chart */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ProposalPanel
          result={result}
          status={status}
          positionOpen={positionOpen}
          openPosition={
            (positions ?? []).find((p) => p.symbol.toUpperCase() === symbol.toUpperCase()) ?? null
          }
          armedSetup={armedForResult}
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
        <div className="space-y-4">
          <QuickTradePanel
            symbol={symbol}
            assetClass={assetClass}
            timeframe={timeframe}
            onPlaced={() => setPosBump((b) => b + 1)}
          />
          <AutoTradePanel symbol={symbol} assetClass={assetClass} timeframe={timeframe} onSelect={openPositionSymbol} />
          <ConditionalsPanel onSelect={openPositionSymbol} />
        </div>
      </div>

      <WatchlistPanel
        currentSymbol={symbol}
        currentAsset={assetClass}
        currentTimeframe={timeframe}
        onSelect={(it) => {
          if (it.asset_class !== assetClass) setAssetClass(it.asset_class as AssetClass);
          setSymbol(it.symbol);
          if (it.timeframe) setTimeframe(it.timeframe);
        }}
      />

      <div className="section-label pt-1">Automation &amp; scanners</div>

      <OpportunitiesPanel
        onSelect={openPositionSymbol}
        onOpened={() => setPosBump((b) => b + 1)}
      />

      <RsiOverPanel onStaged={() => setPosBump((b) => b + 1)} onSelect={openPositionSymbol} />

      <PendingProposalsPanel
        onSelect={openPositionSymbol}
        onChanged={() => setPosBump((b) => b + 1)}
      />

      <div className="section-label pt-1">Positions &amp; risk</div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <PositionAdvicePanel refreshSignal={posBump}
                               lang={settings?.app.analysis_language} />
          <PositionsTable
            positions={positions}
            onClose={closePosition}
            onSetSlTp={setSlTp}
            onSelect={openPositionSymbol}
          />
        </div>
        <div className="space-y-4">
          <RiskDashboard risk={risk} account={account} settings={settings} onChanged={onSettingsChanged} />
          <AdvisorActivity refreshSignal={posBump} />
        </div>
      </div>

      <ScorecardPanel />
      <EntryFiltersPanel />
    </div>
  );
}
