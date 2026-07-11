import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtPrice, fmtUsd } from "../format";
import type { AssetClass } from "../types";

/**
 * Manual QUICK trade ticket — one click to open at market. No stop/target needed up front: the
 * Risk Manager sizes the lots at the 3% cap off an AUTO ATR stop, and both the stop and a default
 * target are placed on the chart as draggable bars you fine-tune after the fill. It still ALWAYS
 * runs through the deterministic Risk Manager (sizing + 3% cap, exposure, correlation, cooldown,
 * daily-loss breaker, anti-stacking) and the execution gates (kill-switch, live-confirmation).
 * Nothing here bypasses risk. An "advanced" disclosure lets you type exact levels if you want.
 */
export function QuickTradePanel({
  symbol,
  assetClass,
  timeframe,
  onPlaced,
}: {
  symbol: string;
  assetClass: AssetClass;
  timeframe: string;
  onPlaced?: () => void;
}) {
  const [dir, setDir] = useState<"long" | "short">("long");
  const [advanced, setAdvanced] = useState(false);
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const [lots, setLots] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [preview, setPreview] = useState<{
    entry: number;
    stop_loss: number;
    take_profit: number;
    auto_levels: boolean;
    lots: number;
    risk: number;
    approved: boolean;
    reason: string;
  } | null>(null);

  // Risk-size the ticket (no placement) so we can show the auto stop/target + max lots at the 3% cap.
  // With no manual stop, the backend derives an ATR stop and a default target — mirror that here.
  const refreshPreview = async (nextDir = dir, nextStop = stop) => {
    const sl = advanced && Number(nextStop) > 0 ? Number(nextStop) : undefined;
    try {
      const p = await api.manualPreview({
        symbol,
        asset_class: assetClass,
        direction: nextDir,
        stop_loss: sl,
        timeframe,
      });
      setPreview({
        entry: p.entry,
        stop_loss: p.stop_loss,
        take_profit: p.take_profit,
        auto_levels: p.auto_levels,
        lots: p.max_lots,
        risk: p.risk_amount,
        approved: p.approved,
        reason: p.reason,
      });
    } catch {
      setPreview(null); // wrong-side stop etc. — just hide the suggestion
    }
  };

  // Refresh the preview whenever the pair/timeframe/direction changes (and on mount).
  useEffect(() => {
    void refreshPreview(dir, stop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, assetClass, timeframe, dir, advanced]);

  const place = async () => {
    const sl = advanced && Number(stop) > 0 ? Number(stop) : undefined;
    if (advanced && stop && (!sl || sl <= 0)) {
      setResult({ ok: false, text: "Enter a valid stop price, or clear it to use the auto ATR stop." });
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const res = await api.manualTrade({
        symbol,
        asset_class: assetClass,
        direction: dir,
        stop_loss: sl, // omit → auto ATR stop
        take_profit: advanced && target ? Number(target) : null, // omit → default target
        lots: lots ? Number(lots) : null,
        timeframe,
        execute: true,
      });
      if (res.status === "executed") {
        setResult({
          ok: true,
          text: `Opened ${dir.toUpperCase()} ${symbol} · ${res.risk.approved_qty} lots · risk ${fmtUsd(
            res.risk.risk_amount,
          )} — drag the SL/TP bars on the chart to adjust.`,
        });
        setStop("");
        setTarget("");
        setLots("");
      } else if (!res.risk.approved) {
        setResult({ ok: false, text: `Risk Manager vetoed: ${res.risk.reason}` });
      } else {
        setResult({ ok: true, text: `Queued for approval (${res.risk.approved_qty} lots).` });
      }
      onPlaced?.();
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const inputCls =
    "w-full rounded bg-neutral-800 px-2 py-1.5 text-sm tabular-nums text-neutral-100 placeholder:text-neutral-600";

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-semibold text-neutral-200">⚡ Quick trade — {symbol}</span>
        <span className="text-[10px] text-neutral-500">market entry · auto-sized</span>
      </div>

      <div className="mb-2 flex gap-2">
        <button
          onClick={() => setDir("long")}
          className={`flex-1 rounded px-3 py-1.5 text-sm font-semibold ${
            dir === "long" ? "bg-bull/20 text-bull ring-1 ring-bull/50" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          ▲ Long
        </button>
        <button
          onClick={() => setDir("short")}
          className={`flex-1 rounded px-3 py-1.5 text-sm font-semibold ${
            dir === "short" ? "bg-bear/20 text-bear ring-1 ring-bear/50" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          ▼ Short
        </button>
      </div>

      {/* What the one-click trade will use — auto stop/target + the 3%-capped size. */}
      {preview &&
        (preview.approved && preview.lots > 0 ? (
          <div className="mb-2 rounded bg-neutral-800/60 px-2 py-1.5 text-[11px] text-neutral-400">
            <div className="flex flex-wrap gap-x-3 gap-y-0.5">
              <span>
                {preview.auto_levels ? "Auto stop" : "Stop"}{" "}
                <span className="tabular-nums text-bear">{fmtPrice(preview.stop_loss)}</span>
              </span>
              <span>
                {preview.auto_levels ? "Auto target" : "Target"}{" "}
                <span className="tabular-nums text-bull">{fmtPrice(preview.take_profit)}</span>
              </span>
              <span>
                Size{" "}
                <button
                  type="button"
                  onClick={() => setLots(String(preview.lots))}
                  className="font-semibold text-neutral-200 underline decoration-dotted hover:text-white"
                  title="Fill the maximum size allowed by the 3% per-trade risk cap"
                >
                  {preview.lots} lots
                </button>{" "}
                · risk {fmtUsd(preview.risk)}
              </span>
            </div>
          </div>
        ) : (
          <div className="mb-2 text-xs text-bear">Risk Manager would veto: {preview.reason}</div>
        ))}

      <button
        onClick={place}
        disabled={busy}
        className={`w-full rounded py-2 text-sm font-semibold text-white disabled:opacity-50 ${
          dir === "long" ? "bg-bull hover:bg-green-700" : "bg-bear hover:bg-red-700"
        }`}
      >
        {busy ? "Placing…" : `Place ${dir === "long" ? "LONG" : "SHORT"} (risk-managed)`}
      </button>

      {/* Advanced: type exact levels + size instead of the auto stop/target. */}
      <button
        type="button"
        onClick={() => setAdvanced((a) => !a)}
        className="mt-2 text-[11px] text-neutral-500 hover:text-neutral-300"
      >
        {advanced ? "▾ Hide manual levels" : "▸ Set levels manually"}
      </button>
      {advanced && (
        <div className="mt-1.5 grid grid-cols-3 gap-2">
          <label className="text-[10px] uppercase text-neutral-500">
            Stop
            <input
              className={inputCls}
              value={stop}
              onChange={(e) => setStop(e.target.value)}
              onBlur={(e) => void refreshPreview(dir, e.target.value)}
              inputMode="decimal"
              placeholder="auto"
            />
          </label>
          <label className="text-[10px] uppercase text-neutral-500">
            Target
            <input
              className={inputCls}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              inputMode="decimal"
              placeholder="auto"
            />
          </label>
          <label className="text-[10px] uppercase text-neutral-500">
            Lots
            <input
              className={inputCls}
              value={lots}
              onChange={(e) => setLots(e.target.value)}
              inputMode="decimal"
              placeholder="auto"
            />
          </label>
        </div>
      )}

      {result && (
        <div className={`mt-2 text-xs ${result.ok ? "text-bull" : "text-bear"}`}>{result.text}</div>
      )}
      <div className="mt-1.5 text-[10px] text-neutral-600">
        One click opens at market, auto-sized to the 3% cap off an ATR stop. Adjust the SL/TP by dragging
        their bars on the chart after the fill. Always gated by the Risk Manager; kill‑switch and
        live‑confirmation still apply.
      </div>
    </div>
  );
}
