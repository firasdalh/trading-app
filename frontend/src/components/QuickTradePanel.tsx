import { useState } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import type { AssetClass } from "../types";

/**
 * Manual QUICK trade ticket. The user places a trade directly, but it ALWAYS runs through the
 * deterministic Risk Manager (sizing + 3% cap, exposure, correlation, cooldown, daily-loss breaker,
 * anti-stacking) and the execution gates (kill-switch, live-confirmation). Nothing here bypasses risk.
 */
export function QuickTradePanel({
  symbol,
  assetClass,
  onPlaced,
}: {
  symbol: string;
  assetClass: AssetClass;
  onPlaced?: () => void;
}) {
  const [dir, setDir] = useState<"long" | "short">("long");
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const [lots, setLots] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [maxInfo, setMaxInfo] = useState<{ lots: number; risk: number; approved: boolean; reason: string } | null>(null);

  // Risk-size the ticket (no placement) so we can suggest the max lots at the 3% cap.
  const refreshMax = async (nextDir = dir, nextStop = stop) => {
    const sl = Number(nextStop);
    if (!sl || sl <= 0) {
      setMaxInfo(null);
      return;
    }
    try {
      const p = await api.manualPreview({ symbol, asset_class: assetClass, direction: nextDir, stop_loss: sl });
      setMaxInfo({ lots: p.max_lots, risk: p.risk_amount, approved: p.approved, reason: p.reason });
    } catch {
      setMaxInfo(null); // wrong-side stop etc. — just hide the suggestion
    }
  };

  const place = async () => {
    const sl = Number(stop);
    if (!sl || sl <= 0) {
      setResult({ ok: false, text: "Enter a stop-loss price (the Risk Manager sizes off it)." });
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const res = await api.manualTrade({
        symbol,
        asset_class: assetClass,
        direction: dir,
        stop_loss: sl,
        take_profit: target ? Number(target) : null,
        lots: lots ? Number(lots) : null,
        execute: true,
      });
      if (res.status === "executed") {
        setResult({
          ok: true,
          text: `Opened ${dir.toUpperCase()} ${symbol} · ${res.risk.approved_qty} lots · risk ${fmtUsd(
            res.risk.risk_amount,
          )}`,
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
        <span className="text-[10px] text-neutral-500">market entry</span>
      </div>

      <div className="mb-2 flex gap-2">
        <button
          onClick={() => {
            setDir("long");
            void refreshMax("long");
          }}
          className={`flex-1 rounded px-3 py-1.5 text-sm font-semibold ${
            dir === "long" ? "bg-bull/20 text-bull ring-1 ring-bull/50" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          ▲ Long
        </button>
        <button
          onClick={() => {
            setDir("short");
            void refreshMax("short");
          }}
          className={`flex-1 rounded px-3 py-1.5 text-sm font-semibold ${
            dir === "short" ? "bg-bear/20 text-bear ring-1 ring-bear/50" : "bg-neutral-800 text-neutral-400"
          }`}
        >
          ▼ Short
        </button>
      </div>

      <div className="mb-2 grid grid-cols-3 gap-2">
        <label className="text-[10px] uppercase text-neutral-500">
          Stop <span className="text-bear">*</span>
          <input className={inputCls} value={stop} onChange={(e) => setStop(e.target.value)}
                 onBlur={(e) => void refreshMax(dir, e.target.value)}
                 inputMode="decimal" placeholder="req." />
        </label>
        <label className="text-[10px] uppercase text-neutral-500">
          Target
          <input className={inputCls} value={target} onChange={(e) => setTarget(e.target.value)}
                 inputMode="decimal" placeholder="opt." />
        </label>
        <label className="text-[10px] uppercase text-neutral-500">
          Lots
          <input className={inputCls} value={lots} onChange={(e) => setLots(e.target.value)}
                 inputMode="decimal" placeholder="auto" />
        </label>
      </div>

      {maxInfo &&
        (maxInfo.approved && maxInfo.lots > 0 ? (
          <button
            type="button"
            onClick={() => setLots(String(maxInfo.lots))}
            className="mb-2 text-xs text-neutral-400 hover:text-neutral-200"
            title="Fill the maximum size allowed by the 3% per-trade risk cap"
          >
            max <span className="font-semibold text-neutral-200">{maxInfo.lots} lots</span> (3% cap) ·
            risk {fmtUsd(maxInfo.risk)}
          </button>
        ) : (
          <div className="mb-2 text-xs text-bear">Risk Manager would veto: {maxInfo.reason}</div>
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

      {result && (
        <div className={`mt-2 text-xs ${result.ok ? "text-bull" : "text-bear"}`}>{result.text}</div>
      )}
      <div className="mt-1.5 text-[10px] text-neutral-600">
        Always sized + gated by the Risk Manager (3% cap, exposure, cooldown, daily‑loss). Kill‑switch and
        live‑confirmation still apply. Leave Lots blank for the auto 3% size.
      </div>
    </div>
  );
}
