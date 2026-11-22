import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import type { ManualFollowSettings as Cfg } from "../types";

/** The manual trades' follow-the-target settings (2026-11-22, the user: "a place to set the first and second stop loss
 * and take profit, and switch it on by itself when I open a trade; cancelled on a trade, it stays manual"). A chip
 * by Open positions ("🎯 Auto") that opens the settings. The backend applies them (manual_follow.py): SL1 the trade's
 * stop, TP1 the target the app keeps, TP2 the broker's safety take profit, SL2 the lock at TP1 - then the stop trails
 * the 30s SuperTrend. */

function Num({ value, onChange, min, max, step, disabled }: {
  value: number; onChange: (v: number) => void; min: number; max: number; step: number; disabled?: boolean;
}) {
  return (
    <input
      type="number"
      className="field w-20 py-0.5 text-right tabular-nums disabled:opacity-40"
      value={Number.isFinite(value) ? value : ""}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      onChange={(e) => onChange(Number(e.target.value))}
    />
  );
}

function Row({ name, what, children, note }: { name: string; what: string; children: ReactNode; note?: string }) {
  return (
    <div className="grid grid-cols-[3.25rem_1fr] items-start gap-x-2 gap-y-0.5 border-t border-neutral-800 py-2">
      <div className="pt-0.5 text-sm font-bold text-neutral-100">{name}</div>
      <div>
        <div className="text-[11px] uppercase tracking-wide text-neutral-500">{what}</div>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-neutral-300">{children}</div>
        {note && <div className="mt-1 text-[11px] text-neutral-500">{note}</div>}
      </div>
    </div>
  );
}

function Choice({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={`rounded-md border px-2 py-0.5 text-xs ${on ? "border-amber-500/70 bg-amber-500/15 text-amber-200" : "border-neutral-700 text-neutral-400 hover:text-neutral-200"}`}
    >
      {children}
    </button>
  );
}

export function ManualFollowSettings() {
  const [open, setOpen] = useState(false);
  const [bump, setBump] = useState(0);
  const { data: cfg } = usePolling(() => api.followSettings(), 30_000, [bump]);
  const [draft, setDraft] = useState<Cfg | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // The form starts from the saved settings once they are in - also when the chip was clicked before the first read
  // came back - and a later poll never overwrites what is being typed.
  useEffect(() => {
    if (!open) {
      setDraft(null);
      setErr(null);
      return;
    }
    if (cfg && !draft) setDraft({ ...cfg });
  }, [open, cfg, draft]);

  const set = (patch: Partial<Cfg>) => setDraft((d) => (d ? { ...d, ...patch } : d));
  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setErr(null);
    try {
      await api.saveFollowSettings(draft);
      setBump((b) => b + 1);
      setOpen(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message.replace(/^\d+: /, "") : String(e));
    } finally {
      setBusy(false);
    }
  };

  const auto = !!cfg?.auto;
  // A worked example on round numbers: a long at 100.00 with its stop at 98.00 (1R = 2.00).
  const d = draft;
  const ex = d && (() => {
    const r = 2;
    const tp1 = d.tp1_mode === "r" ? 100 + d.tp1_r * r : null;
    const base = tp1 ?? 102;
    return {
      tp1: tp1 != null ? tp1.toFixed(2) : `your TP (say 102.00)`,
      tp2: (base + d.tp2_r * r).toFixed(2),
      sl2: (base - d.sl2_r * r).toFixed(2),
      sl1: d.sl1_mode === "atr" ? `100.00 - ${d.sl1_atr} x ATR` : "98.00 (yours)",
    };
  })();

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={`rounded px-1.5 py-0.5 text-xs font-semibold ${auto ? "bg-amber-500/90 text-neutral-950 hover:bg-amber-400" : "text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200"}`}
        title={auto
          ? "Auto follow is ON: every new manual trade (scalp ticket, MT5, phone) follows past its target on your levels. Click for the settings."
          : "Follow past the target on manual trades: your SL1 / TP1 / TP2 / SL2, and switching it on by itself. Click for the settings."}
      >
        🎯 {auto ? "Auto ON" : "Auto"}
      </button>

      {/* On document.body: inside the sidebar the chart's layers were drawn over it. */}
      {open && d && createPortal(
        <div className="fixed inset-0 z-40 flex items-start justify-center bg-black/60 p-4" onClick={() => setOpen(false)}>
          <div
            role="dialog"
            aria-label="Manual trades - follow past the target"
            className="mt-16 w-full max-w-md rounded-xl border border-neutral-700 bg-neutral-900 p-4 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-base font-semibold text-neutral-100">🎯 Manual trades - follow past the target</div>
                <div className="mt-0.5 text-xs text-neutral-500">
                  The same rule as the strategy's: at TP1, with the 30s SuperTrend on the trade's side the stop locks at SL2
                  and then trails the line; otherwise the profit is taken at TP1.
                </div>
              </div>
              <button onClick={() => setOpen(false)} className="text-neutral-500 hover:text-neutral-200" aria-label="Close">✕</button>
            </div>

            <label className="mt-3 flex cursor-pointer items-start gap-3 rounded-lg border border-neutral-800 bg-neutral-950/50 p-2.5">
              <input type="checkbox" className="mt-1" checked={d.auto} onChange={(e) => set({ auto: e.target.checked })} />
              <span>
                <span className="block text-sm font-semibold text-neutral-100">Auto: switch it on for every new manual trade</span>
                <span className="block text-[11px] text-neutral-500">
                  From the scalp ticket, MT5 or the phone - 15 s after it opens (time to set its levels by hand). Trades open
                  when you switch it on are left as they are. Switched off on a trade (its 🎯 button), that trade stays manual.
                </span>
              </span>
            </label>

            <div className="mt-2">
              <Row name="SL 1" what="the trade's stop"
                   note={d.sl1_mode === "keep" ? `A trade opened without a stop gets ${d.sl1_atr} x ATR (1m).` : "Set on every trade it follows, replacing the stop it had."}>
                <Choice on={d.sl1_mode === "keep"} onClick={() => set({ sl1_mode: "keep" })}>keep my stop</Choice>
                <Choice on={d.sl1_mode === "atr"} onClick={() => set({ sl1_mode: "atr" })}>always</Choice>
                <Num value={d.sl1_atr} min={0.3} max={10} step={0.1} onChange={(v) => set({ sl1_atr: v })} />
                <span>x ATR (1m)</span>
              </Row>
              <Row name="TP 1" what="the target the app keeps (you drag it on the chart)"
                   note={d.tp1_mode === "keep" ? `A trade opened without a take profit gets ${d.tp1_r}R.` : "Set on every trade it follows, in R = the entry-to-stop distance."}>
                <Choice on={d.tp1_mode === "keep"} onClick={() => set({ tp1_mode: "keep" })}>keep my TP</Choice>
                <Choice on={d.tp1_mode === "r"} onClick={() => set({ tp1_mode: "r" })}>always</Choice>
                <Num value={d.tp1_r} min={0.2} max={10} step={0.1} onChange={(v) => set({ tp1_r: v })} />
                <span>R from the entry</span>
              </Row>
              <Row name="TP 2" what="the broker's safety take profit" note="Only a safety net, for when the app can't act (PC or MT5 link down).">
                <Num value={d.tp2_r} min={0.5} max={10} step={0.1} onChange={(v) => set({ tp2_r: v })} />
                <span>R past TP 1</span>
              </Row>
              <Row name="SL 2" what="the stop locked at TP 1" note="Then it trails the 30s SuperTrend, and the trade closes when the line turns.">
                <Num value={d.sl2_r} min={0} max={1} step={0.05} onChange={(v) => set({ sl2_r: v })} />
                <span>R short of TP 1</span>
              </Row>
            </div>

            {ex && (
              <div className="mt-2 rounded-md bg-neutral-950/60 px-2.5 py-1.5 text-[11px] tabular-nums text-neutral-400">
                Example - long at 100.00, stop {ex.sl1}: TP1 <b className="text-amber-300">{ex.tp1}</b> · TP2 <b className="text-bull">{ex.tp2}</b> · SL2 <b className="text-neutral-200">{ex.sl2}</b>
              </div>
            )}
            {err && <div className="mt-2 rounded border border-bear/40 bg-bear/10 px-2.5 py-1.5 text-xs text-bear">{err}</div>}
            <div className="mt-3 flex justify-end gap-2">
              <button onClick={() => setOpen(false)} className="btn btn-ghost text-sm">Cancel</button>
              <button onClick={() => void save()} disabled={busy} className="btn btn-primary text-sm">{busy ? "Saving…" : "Save"}</button>
            </div>
          </div>
        </div>
        , document.body)}
    </>
  );
}
