import { useEffect, useState } from "react";
import { api } from "../api/client";
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
import { Chart } from "./Chart";
import { ProposalPanel } from "./ProposalPanel";
import { PositionAdvicePanel } from "./PositionAdvicePanel";
import { PositionsTable } from "./PositionsTable";
import { RiskDashboard } from "./RiskDashboard";
import { WatchlistPanel } from "./WatchlistPanel";

interface Props {
  settings: SettingsResponse | null;
}

const TIMEFRAMES = ["15m", "1h", "4h", "1d"];
const ASSET_CLASSES: AssetClass[] = ["stock", "crypto", "forex", "metal"];

export function Dashboard({ settings }: Props) {
  const [symbol, setSymbol] = useState("EURUSD");
  const [symbolInput, setSymbolInput] = useState("EURUSD");
  const [assetClass, setAssetClass] = useState<AssetClass>("forex");
  const [timeframe, setTimeframe] = useState("1h");
  const [result, setResult] = useState<AnalyzeResponse | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [symbols, setSymbols] = useState<string[]>([]);
  const [posBump, setPosBump] = useState(0);
  const liveQuote = useQuoteSocket(symbol, assetClass);
  const { data: brokerInfo } = usePolling(() => api.brokerInfo(assetClass), 6000, [assetClass]);

  // Load the broker's available symbols for the chosen asset class. If the current symbol
  // isn't offered (e.g. switching to forex while on AAPL), jump to the first available one.
  useEffect(() => {
    let cancelled = false;
    api
      .symbols(assetClass)
      .then((r) => {
        if (cancelled) return;
        setSymbols(r.symbols);
        if (r.symbols.length && !r.symbols.includes(symbol)) {
          setSymbol(r.symbols[0]);
          setSymbolInput(r.symbols[0]);
        }
      })
      .catch(() => setSymbols([]));
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
  const { data: advice } = usePolling(() => api.positionAdvice(), 8000, [posBump]);

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

  const runAnalysis = async () => {
    setAnalyzing(true);
    setError(null);
    try {
      const res = await api.analyze(symbol, assetClass, timeframe);
      setResult(res);
      setStatus(res.status);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAnalyzing(false);
    }
  };

  const approve = async () => {
    if (!result) return;
    setActionBusy(true);
    try {
      const updated = await api.approve(result.proposal_id);
      setStatus(updated.status);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
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
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActionBusy(false);
    }
  };

  const applySymbol = () => {
    const s = symbolInput.trim().toUpperCase();
    if (s) setSymbol(s);
  };

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-4">
      {/* Controls */}
      <div className="card flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Symbol</div>
          {symbols.length > 0 ? (
            <select
              value={symbols.includes(symbol) ? symbol : symbols[0]}
              onChange={(e) => {
                setSymbol(e.target.value);
                setSymbolInput(e.target.value);
              }}
              className="w-48 rounded bg-neutral-800 px-2 py-1.5"
            >
              {symbols.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && applySymbol()}
              onBlur={applySymbol}
              placeholder="Type a symbol"
              className="w-44 rounded bg-neutral-800 px-2 py-1.5 uppercase"
            />
          )}
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Asset class</div>
          <select
            value={assetClass}
            onChange={(e) => setAssetClass(e.target.value as AssetClass)}
            className="rounded bg-neutral-800 px-2 py-1.5"
          >
            {ASSET_CLASSES.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <div className="mb-1 text-xs text-neutral-400">Timeframe</div>
          <select
            value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}
            className="rounded bg-neutral-800 px-2 py-1.5"
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
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
              <span className="tabular-nums font-semibold">{liveQuote.price.toFixed(2)}</span>
            </span>
          )}
          <button
            onClick={runAnalysis}
            disabled={analyzing}
            className="btn bg-blue-600 text-white hover:bg-blue-500"
          >
            {analyzing ? "Analyzing…" : "Run analysis"}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
          {error}
        </div>
      )}

      {/* Main grid: chart + side panels */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2">
          <div className="mb-2 text-sm font-semibold">
            {symbol} · {timeframe}
          </div>
          <Chart
            symbol={symbol}
            assetClass={assetClass}
            timeframe={timeframe}
            proposal={result?.proposal ?? null}
            liveQuote={liveQuote}
            positions={positions}
          />
          <p className="mt-2 text-xs text-neutral-500">
            Backtest and paper results do not guarantee live results.
          </p>
        </div>
        <ProposalPanel
          result={result}
          status={status}
          busy={actionBusy}
          equity={account?.equity ?? null}
          onApprove={approve}
          onReject={reject}
        />
      </div>

      <WatchlistPanel
        currentSymbol={symbol}
        currentAsset={assetClass}
        currentTimeframe={timeframe}
        mode={settings?.app.execution_mode}
      />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <PositionAdvicePanel advice={advice} />
          <PositionsTable positions={positions} onClose={closePosition} onSetSlTp={setSlTp} />
        </div>
        <RiskDashboard risk={risk} account={account} settings={settings} />
      </div>
    </div>
  );
}
