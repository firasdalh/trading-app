import { useState } from "react";
import { api } from "../api/client";
import { assetLabel, displaySymbol, fmtPrice } from "../format";
import type { AssetClass, BacktestResult } from "../types";
import { EquityChart } from "./EquityChart";

const TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"];
const ASSET_CLASSES: AssetClass[] = ["stock", "crypto", "forex", "metal", "energy", "index"];

// Read a value the Dashboard persisted, so the backtest opens on the pair you were viewing.
function lsGet<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw != null ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

// The symbol as the API should get it. A mixed-case name (AUDCADm, US30_x10m) is a real broker
// name: send it exactly, so MT5 matches it on the first, exact lookup. One typed all in one case
// ("aapl", "XAUUSDM") is normalised to upper case as before — the MT5 resolver still finds the
// suffixed instrument from that.
function apiSymbol(raw: string): string {
  const s = raw.trim();
  return /[a-z]/.test(s) && /[A-Z]/.test(s) ? s : s.toUpperCase();
}

export function BacktestView() {
  const [symbol, setSymbol] = useState(() => lsGet("ta.symbol", "AAPL"));
  const [assetClass, setAssetClass] = useState<AssetClass>(() => lsGet("ta.assetClass", "stock"));
  const [timeframe, setTimeframe] = useState(() => lsGet("ta.timeframe", "1h"));
  const [bars, setBars] = useState(400);
  const [equity, setEquity] = useState(100000);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.backtest(apiSymbol(symbol), assetClass, timeframe, bars, equity));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const m = result?.metrics;

  return (
    // Full width with the desk's gutter, like the desk and the Journal.
    <div className="space-y-3 p-3">
      <div className="card flex flex-wrap items-end gap-3">
        <Field label="Symbol">
          <input
            name="bt-symbol"
            autoComplete="off"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="w-32 rounded bg-neutral-800 px-2 py-1.5"
            title="Broker symbol, e.g. XAUUSDm or EURUSDm (Exness), or a stock ticker"
          />
        </Field>
        <Field label="Asset class">
          <select name="bt-asset-class" value={assetClass} onChange={(e) => setAssetClass(e.target.value as AssetClass)} className="rounded bg-neutral-800 px-2 py-1.5">
            {ASSET_CLASSES.map((a) => <option key={a} value={a}>{assetLabel(a)}</option>)}
          </select>
        </Field>
        <Field label="Timeframe">
          <select name="bt-timeframe" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} className="rounded bg-neutral-800 px-2 py-1.5">
            {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="Bars">
          <input name="bt-bars" type="number" value={bars} min={50} max={2000} onChange={(e) => setBars(Number(e.target.value))} className="w-24 rounded bg-neutral-800 px-2 py-1.5" />
        </Field>
        <Field label="Starting equity">
          <input name="bt-equity" type="number" value={equity} min={1} onChange={(e) => setEquity(Number(e.target.value))} className="w-32 rounded bg-neutral-800 px-2 py-1.5" />
        </Field>
        <button onClick={run} disabled={busy} className="btn btn-primary">
          {busy ? "Running…" : "Run backtest"}
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">{error}</div>
      )}

      {!result && !busy && !error && (
        <div className="card text-sm text-neutral-400">
          Press <span className="font-semibold text-neutral-200">Run backtest</span> to replay the
          deterministic strategy over historical candles and see the equity curve, win rate, R-multiples,
          drawdown and every trade.
          <div className="mt-1 text-xs text-neutral-500">
            Use <code>metal</code> / <code>forex</code> / <code>crypto</code> for your Exness pairs
            (e.g. <code>XAUUSDm</code>) to test on real MT5 history; <code>stock</code> uses the simulator.
            Results are hypothetical and don’t guarantee live results.
          </div>
        </div>
      )}

      {result && m && (
        <>
          <div className="card">
            <div className="mb-2 text-sm font-semibold">
              Equity curve · {result.asset_class === "stock" ? result.symbol : displaySymbol(result.symbol)} {result.timeframe} · {result.bars} bars
            </div>
            <EquityChart points={result.equity_curve} />
            <p className="mt-2 text-xs text-warn">{result.disclaimer}</p>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
            <Metric label="Trades" value={`${m.total_trades}`} />
            <Metric label="Win rate" value={`${(m.win_rate * 100).toFixed(1)}%`} />
            <Metric label="Avg R" value={m.avg_r_multiple.toFixed(2)} valueClass={m.avg_r_multiple >= 0 ? "text-bull" : "text-bear"} />
            <Metric label="Profit factor" value={m.profit_factor == null ? "∞" : m.profit_factor.toFixed(2)} />
            <Metric label="Max DD" value={`${(m.max_drawdown_pct * 100).toFixed(1)}%`} valueClass="text-bear" />
            <Metric label="Net P&L" value={`${m.net_pnl >= 0 ? "+" : ""}${m.net_pnl.toFixed(0)}`} valueClass={m.net_pnl >= 0 ? "text-bull" : "text-bear"} />
            <Metric label="Return" value={`${(m.return_pct * 100).toFixed(1)}%`} valueClass={m.return_pct >= 0 ? "text-bull" : "text-bear"} />
          </div>

          <div className="card">
            <div className="mb-2 text-sm font-semibold">Trades ({result.trades.length})</div>
            {result.trades.length === 0 ? (
              <div className="text-sm text-neutral-500">No trades were taken (no qualifying setups).</div>
            ) : (
              // Every column gets the same right gutter (as in the Journal): without it the right-
              // aligned R ran straight into the left-aligned Bars, so "1.8" and "7" read as "1.87".
              <table className="w-full whitespace-nowrap text-sm">
                <thead className="text-left text-xs text-neutral-400">
                  <tr>
                    <th className="py-1 pr-3">Dir</th><th className="pr-3">Entry</th><th className="pr-3">Exit</th>
                    <th className="pr-3 text-right">Qty</th><th className="pr-3 text-right">P&amp;L</th>
                    <th className="pr-3 text-right">R</th><th className="pr-3 text-right">Bars</th><th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t, i) => (
                    <tr key={i} className="border-t border-neutral-800">
                      <td className={`py-1 pr-3 ${t.direction === "long" ? "text-bull" : "text-bear"}`}>{t.direction}</td>
                      <td className="pr-3 tabular-nums">{fmtPrice(t.entry)}</td>
                      <td className="pr-3 tabular-nums">{fmtPrice(t.exit)}</td>
                      <td className="pr-3 text-right tabular-nums">{t.qty}</td>
                      <td className={`pr-3 text-right tabular-nums ${t.pnl >= 0 ? "text-bull" : "text-bear"}`}>{t.pnl.toFixed(2)}</td>
                      <td className="pr-3 text-right tabular-nums">{t.r_multiple}</td>
                      <td className="pr-3 text-right tabular-nums">{t.bars_held}</td>
                      <td className="text-neutral-400">{t.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="text-sm">
      <div className="mb-1 text-xs text-neutral-400">{label}</div>
      {children}
    </label>
  );
}

function Metric({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="card py-2">
      <div className="text-xs text-neutral-400">{label}</div>
      <div className={`text-lg tabular-nums ${valueClass ?? ""}`}>{value}</div>
    </div>
  );
}
