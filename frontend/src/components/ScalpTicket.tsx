import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { api } from "../api/client";
import { playCelebration } from "../celebrate";
import { useScalpRsiPeriod } from "../rsiPeriod";
import { fmtPrice, fmtUsd } from "../format";
import { play } from "../hooks/useApprovalChime";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import {
  SCALP_MAX_RISK, SCALP_PRESETS, STOP_ATR_CHOICES, TARGET_R_CHOICES, dailyStop, fmtDur, fmtR, planScalp, resolvePreOrder, sameSymbol,
  scalpReadings, scalpStop, tradeWatch, untilUtcMidnight,
  type CueTone, type PreOrder, type PreOrderView, type ReadTone, type ScalpHud, type ScalpLevel, type ScalpReading,
  type TradeWatch, type VerdictTone,
} from "../scalp";
import { analysisTimeframe, isSecondsTf } from "../timeframes";
import type { AssetClass, PositionView, ScalpSpec } from "../types";
import { ErrorBoundary } from "./ErrorBoundary";
import { TugConfetti, TugOfWar, TugParty, winnerOf, type Kind } from "./TugOfWar";

const LEVEL_TONE: Record<ScalpLevel, string> = {
  low: "border-emerald-500 bg-emerald-500/20 text-emerald-300",
  medium: "border-amber-500 bg-amber-500/20 text-amber-300",
  high: "border-rose-500 bg-rose-500/20 text-rose-300",
};
const READ_TONE: Record<ReadTone, string> = {
  bull: "bg-bull/15 text-bull",
  bear: "bg-bear/15 text-bear",
  warn: "bg-amber-500/15 text-amber-300",
  flat: "bg-neutral-800 text-neutral-400",
};
const CUE_TONE: Record<CueTone, string> = {
  ok: "bg-bull/15 text-bull",
  warn: "bg-amber-500/15 text-amber-300",
  exit: "bg-bear/20 text-bear ring-1 ring-inset ring-bear/50",
  flat: "bg-neutral-800 text-neutral-400",
};
const VERDICT_TONE: Record<VerdictTone, string> = {
  hold: "bg-bull/20 text-bull ring-bull/50",
  target: "bg-amber-400/20 text-amber-200 ring-amber-400/60",
  watch: "bg-amber-500/15 text-amber-300 ring-amber-500/50",
  exit: "bg-bear/20 text-bear ring-bear/60",
  danger: "animate-pulse bg-bear/30 text-bear ring-bear",
};
const FILL_NOTES = [{ hz: 783.99, at: 0, len: 0.09 }, { hz: 1046.5, at: 0.08, len: 0.18 }];   // G5 -> C6
const CLOSE_NOTES = [{ hz: 1046.5, at: 0, len: 0.08 }, { hz: 659.25, at: 0.07, len: 0.16 }];  // C6 -> E5
const STAGE_NOTES = [{ hz: 587.33, at: 0, len: 0.07 }];
const REFUSED_NOTES = [{ hz: 233.08, at: 0, len: 0.22 }];
const R1_NOTES = [{ hz: 1174.66, at: 0, len: 0.06 }, { hz: 1567.98, at: 0.06, len: 0.12 }];  // D6 -> G6
const TARGET_NOTES = [{ hz: 783.99, at: 0, len: 0.08 }, { hz: 987.77, at: 0.08, len: 0.08 }, { hz: 1318.5, at: 0.16, len: 0.24 }];

// Pips for FX (a pip is 10 points on a 5- or 3-digit quote), plain price distance for everything else.
function distText(d: number, spec: ScalpSpec, assetClass: AssetClass): string {
  if (assetClass === "forex" && spec.point > 0) {
    const pip = spec.digits % 2 === 1 ? spec.point * 10 : spec.point;
    return `${(d / pip).toFixed(1)} pips`;
  }
  return fmtPrice(d, spec.digits);
}

// Keys typed into a field, or pressed while a drawer / dialog / pop-over is open, are never trading
// keys: Space scrolls a drawer, Enter presses its buttons. (Those are only in the page while open.)
function notForUs(e: KeyboardEvent): boolean {
  if (document.querySelector("[role='dialog'], [aria-modal='true']")) return true;
  const el = e.target instanceof HTMLElement ? e.target : null;
  if (!el) return false;
  if (el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
  return el.tagName === "INPUT" && !["range", "checkbox", "radio", "button"].includes((el as HTMLInputElement).type);
}

// Each open trade's best and worst P&L (by ticket) and whether its +1R chime has rung - kept for the
// tab's life, so a reload doesn't forget how good the trade once was.
type Seen = Record<string, { peak: number; trough: number; rang?: boolean }>;
const SEEN_KEY = "scalp.seen";
function loadSeen(): Seen {
  try {
    return JSON.parse(sessionStorage.getItem(SEEN_KEY) ?? "{}") as Seen;
  } catch {
    return {};
  }
}

/**
 * SCALP TICKET: market orders for the fast charts, sized from a leverage you pick and a risk preset,
 * each sent with its stop and target (see ../scalp.ts for the rules it holds to).
 *
 * Two steps. BUY or SELL STAGES a pre-order: it is drawn on the chart with its stop and target, which
 * you drag where you want them, and the dollars at each level follow the leverage bar. A second click
 * on the same side sends it. With the keys armed, Enter is BUY and 0 is SELL, pressed the same way: the
 * first press stages, the same key again sends. Space closes the newest trade on this chart, Delete or
 * Esc drops the pre-order, B / S stage a side.
 *
 * In a trade, the ticket turns into its dashboard: the live P&L in dollars and R, where price sits
 * between the stop and the target, the best and worst so far, and a hold-or-close checklist.
 *
 * Sending re-reads the bid/ask first, so the size is worked out at the price the order really fills
 * at, not from the last 1.5-second poll. The levels you placed are sent exactly as placed.
 */
export function ScalpTicket({ symbol, assetClass, timeframe, spec, specError, positions, pre, setPre, onView, onOpened }: {
  symbol: string;
  assetClass: AssetClass;
  timeframe: string;
  spec: ScalpSpec | null;
  specError: string | null;
  positions: PositionView[] | null;
  pre: PreOrder | null;
  setPre: Dispatch<SetStateAction<PreOrder | null>>;
  onView: (v: { preOrder: PreOrderView | null; hud: ScalpHud | null }) => void;
  onOpened: () => void;   // a trade was opened or closed: re-read the positions
}) {
  const [level, setLevel] = useLocalStorage<ScalpLevel>("scalp.level", "medium");
  const [stopAtr, setStopAtr] = useLocalStorage("scalp.stopAtr", 1.5);
  const [targetR, setTargetR] = useLocalStorage("scalp.targetR", 1.5);
  // The leverage asked for on each preset; absent = the most the preset allows at the current stop,
  // which then follows the stop.
  const [levBy, setLevBy] = useLocalStorage<Partial<Record<ScalpLevel, number>>>("scalp.leverage", {});
  const [sound, setSound] = useLocalStorage("scalp.sound", true);
  // Each preset's risk per trade, in %, when changed from the preset's own (up to SCALP_MAX_RISK, the
  // Risk Manager's scalp ceiling - RISK.md override 2026-11-02).
  const [riskBy, setRiskBy] = useLocalStorage<Partial<Record<ScalpLevel, number>>>("scalp.riskPct", {});
  // The daily stop: on or off, and its % per preset when changed from the preset's own (RISK.md,
  // the user's choice 2026-11-01). Off keeps the meter and never locks.
  const [stopOn, setStopOn] = useLocalStorage("scalp.dailyStopOn", true);
  const [stopPct, setStopPct] = useLocalStorage<Partial<Record<ScalpLevel, number>>>("scalp.dailyStopPct", {});
  // Enter / 0 / Space / Delete / Esc / B / S trade only once armed here: Space is the key most pressed by accident.
  const [keys, setKeys] = useLocalStorage("scalp.keys", false);
  // Folded: one line (state, size, SELL / BUY) and the trade or pre-order under it - the rest of the
  // window goes to the chart.
  const [fold, setFold] = useLocalStorage("scalp.fold", false);
  // The bull vs bear tug of war on the trade bar, and its ending when the trade closes (TugOfWar.tsx).
  const [tug, setTug] = useLocalStorage("scalp.tug", true);
  const [busy, setBusy] = useState<"long" | "short" | "close" | null>(null);
  const [flash, setFlash] = useState<{
    dir: "long" | "short" | "close"; text: string; tone?: "win" | "loss" | "target";
    party?: { win: Kind; kind: "target" | "stop" | "close" };   // a close: who won the tug of war
  } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const inFlight = useRef(false);

  // A seconds bar is too small to set a stop by; those charts take their stop from the 1m.
  const stopTf = isSecondsTf(timeframe) ? "1m" : timeframe;
  const { data: series } = usePolling(() => api.ohlcv(symbol, assetClass, stopTf, 120), 10000, [symbol, assetClass, stopTf]);
  const { data: risk } = usePolling(() => api.riskState(), 15000);
  const { data: settings } = usePolling(() => api.settings(), 30000);
  const { data: review } = usePolling(() => api.journalReview(), 20000);

  const candles = series && series.timeframe === stopTf && sameSymbol(series.symbol, symbol) ? series.candles : null;
  const rsiP = useScalpRsiPeriod();     // the scalp chart's RSI period, so the gauges read the same RSI
  const { atr, items } = useMemo(
    () => (candles ? scalpReadings(candles, spec?.spread ?? null, stopTf, rsiP) : { atr: null, items: [] }),
    [candles, spec?.spread, stopTf, rsiP],
  );
  const spreadGate = settings?.risk.spread_gate_enabled ? settings.risk.max_spread_r_fraction : null;
  const preset = SCALP_PRESETS[level];
  const riskOf = (k: ScalpLevel) => Math.min(SCALP_MAX_RISK, (riskBy[k] ?? SCALP_PRESETS[k].risk * 100) / 100);
  const riskFrac = riskOf(level);
  const asked = levBy[level] ?? null;
  const base = { atr, stopAtr, targetR, level, leverage: asked, spreadGate, risk: riskFrac };

  const live = pre && spec ? resolvePreOrder(spec, pre) : null;
  const plan = spec ? planScalp({ spec, ...base, stopDist: live?.stopDist, targetDist: live?.targetDist }) : null;
  const day = dailyStop(risk ?? null, spec?.equity ?? null, level, { pct: stopPct[level] ?? null, on: stopOn });
  const unconfirmed = settings?.app.broker_env === "live" && settings.live_re_confirm_required;

  useEffect(() => {
    if (!flash) return;
    // How a trade ended stays up long enough to be read after looking away from the screen.
    const t = setTimeout(() => setFlash(null), flash.dir === "close" ? 9000 : 2500);
    return () => clearTimeout(t);
  }, [flash]);

  // What Space closes: the newest trade on this chart's pair - and what the trade dashboard shows.
  const mine = (positions ?? []).filter((p) => p.symbol.toUpperCase() === symbol.toUpperCase());
  const newest = mine.reduce<PositionView | null>((a, p) => (!a || (p.opened_at ?? "") > (a.opened_at ?? "") ? p : a), null);
  const seenRef = useRef<Seen | null>(null);
  seenRef.current ??= loadSeen();
  const watch: TradeWatch | null = newest
    ? tradeWatch({ pos: newest, spec, candles, atr, tf: stopTf, seen: seenRef.current[String(newest.id)], rsiPeriod: rsiP })
    : null;

  // Why an order would not go out now. Staging stays possible regardless - seeing the trade and its
  // dollars is the point of a pre-order - so this only gates SENDING.
  const blocked =
    specError ? `No live quote for ${symbol}: ${specError}`
      : !spec ? "Connecting to the broker..."
        : !(spec.equity && spec.equity > 0) ? "Account equity is $0 - deposit to scalp."
          : day?.locked
            ? `Daily stop hit: ${fmtUsd(-day.lost)} today, ${preset.label}'s limit is ${fmtUsd(-day.limit)} (${day.pct}% of the day's start). Sending unlocks at 00:00 UTC, in ${untilUtcMidnight()} - or raise the % / switch the daily stop off next to the Day meter.`
            : plan?.blocked
              ?? (unconfirmed ? "Live trading isn't confirmed since the backend restarted - Settings › type the phrase › Re-confirm." : null);
  // The Risk Manager never opens a second trade on the side you already hold on this pair; say so
  // before the click rather than after it.
  const stacked = pre && mine.some((p) => p.direction === pre.dir)
    ? `You're already ${pre.dir} ${symbol}: the Risk Manager never stacks a second trade on the same side. Close it first${keys ? " (Space)" : ""}, or stage the other side.`
    : null;
  const why = blocked ?? stacked;

  // Today's scalps, from the journal (trades tagged "scalp"), newest last.
  const scalps = (review?.trades ?? []).filter((t) => t.tags.includes("scalp"));
  const wins = scalps.filter((t) => t.pnl > 0).length;
  const losses = scalps.filter((t) => t.pnl < 0).length;
  const net = scalps.reduce((a, t) => a + t.pnl, 0);
  let streak = 0;
  for (let i = scalps.length - 1; i >= 0; i--) {
    const s = Math.sign(scalps[i].pnl);
    if (!s || (streak && Math.sign(streak) !== s)) break;
    streak += s;
  }

  // The trade's best / worst are kept, +1R rings once, and trades no longer open are forgotten. Only
  // a trade first seen BELOW +1R rings: opening the page on a trade already past it is no news.
  useEffect(() => {
    const seen = (seenRef.current ??= loadSeen());
    let changed = false;
    if (positions) {
      const open = new Set(positions.map((p) => String(p.id)));
      for (const id of Object.keys(seen)) {
        if (!open.has(id)) {
          delete seen[id];
          changed = true;
        }
      }
    }
    if (newest && watch) {
      const id = String(newest.id);
      const was = seen[id];
      const rang = !!was?.rang || (watch.r != null && watch.r >= 1);
      if (!was || was.peak !== watch.peak || was.trough !== watch.trough || rang !== !!was.rang) {
        if (was && rang && !was.rang && sound) play(R1_NOTES, 0.08);
        seen[id] = { peak: watch.peak, trough: watch.trough, rang };
        changed = true;
      }
    }
    if (changed) {
      try {
        sessionStorage.setItem(SEEN_KEY, JSON.stringify(seen));
      } catch {
        /* private mode - not kept */
      }
    }
  });

  // A trade that leaves the book without this ticket closing it hit its stop or its target (or was
  // closed elsewhere): say which, with the last P&L seen. A failed positions read is not a close,
  // and neither is switching the chart to another pair.
  const lastTrade = useRef<{ id: string; sym: string; dir: "long" | "short"; pnl: number; at: number | null } | null>(null);
  const closedHere = useRef<string | null>(null);
  useEffect(() => {
    const last = lastTrade.current;
    if (positions && last && last.sym === symbol && !positions.some((p) => String(p.id) === last.id) && closedHere.current !== last.id) {
      const atTarget = last.at != null && last.at >= 0.9;
      const atStop = last.at != null && last.at <= 0.1;
      const usd = `≈ ${fmtUsd(last.pnl, { sign: true })}`;
      setFlash({
        dir: "close",
        text: atTarget ? `🎯 TARGET HIT ${usd}` : atStop ? `${last.pnl >= 0 ? "stopped in profit" : "stopped out"} ${usd}` : `closed ${usd} (at the broker or elsewhere)`,
        tone: atTarget ? "target" : last.pnl >= 0 ? "win" : "loss",
        party: { win: winnerOf(last.dir, last.pnl), kind: atTarget ? "target" : atStop ? "stop" : "close" },
      });
      // A win gets a celebration clip for its amount; a loss, or no clips, the ticket's own notes.
      if (sound) {
        const notes = atTarget ? TARGET_NOTES : CLOSE_NOTES;
        void playCelebration(last.pnl, { target: atTarget }).then((ok) => { if (!ok) play(notes, 0.1); });
      }
    }
    if (newest && watch) lastTrade.current = { id: String(newest.id), sym: symbol, dir: watch.dir, pnl: watch.pnl, at: watch.at };
    else if (positions) lastTrade.current = null;
  });

  const say = (text: string) => {
    setMsg(text);
    if (sound) play(REFUSED_NOTES, 0.08);
  };

  // Stage at the ATR stop and the chosen R (both travelling with the price until you drag them), or
  // move a staged one to the other side at the distances it has now.
  const stage = (dir: "long" | "short") => {
    if (!spec) return say("No live quote yet.");
    setMsg(null);
    if (pre && live) {
      setPre({ dir, sl: { dist: live.stopDist }, tp: { r: live.targetDist / live.stopDist } });
    } else {
      const d = scalpStop(spec, atr, stopAtr, spreadGate);
      if (!d) return say("Reading the candles for the stop...");
      setPre({ dir, sl: { dist: d }, tp: { r: targetR } });
    }
    if (sound) play(STAGE_NOTES, 0.06);
  };

  // The stop / target chips re-place a staged order's level (un-pinning it); otherwise they set the
  // next one. A target left to travel follows a new stop at its R.
  const pickStop = (x: number) => {
    setStopAtr(x);
    if (!pre || !spec) return;
    const d = scalpStop(spec, atr, x, spreadGate);
    if (d) setPre({ ...pre, sl: { dist: d } });
  };
  const pickTarget = (x: number) => {
    setTargetR(x);
    if (pre) setPre({ ...pre, tp: { r: x } });
  };

  const send = async () => {
    if (inFlight.current || !pre) return;
    if (why) return say(why);
    inFlight.current = true;
    setBusy(pre.dir);
    setMsg(null);
    try {
      const fresh = await api.scalpSpec(symbol, assetClass);
      const d = resolvePreOrder(fresh, pre);
      const p = planScalp({ spec: fresh, ...base, stopDist: d.stopDist, targetDist: d.targetDist });
      if (p.blocked) throw new Error(p.blocked);
      // A dragged level is wherever the pointer let go; MT5 takes prices in the symbol's own digits.
      const px = (v: number) => Number(v.toFixed(fresh.digits));
      const res = await api.manualTrade({
        symbol, asset_class: assetClass, direction: pre.dir, stop_loss: px(d.sl), take_profit: px(d.tp),
        lots: p.lots, risk_pct: riskFrac, timeframe: analysisTimeframe(timeframe), execute: true,
        tags: ["scalp", level], note: `scalp ${timeframe} ${p.leverage.toFixed(1)}x ${preset.label}`,
      });
      if (res.status !== "executed") throw new Error(`Refused by the Risk Manager: ${res.risk?.reason ?? res.status}`);
      setFlash({ dir: pre.dir, text: `✓ ${p.lots} lot` });
      setPre(null);
      if (sound) play(FILL_NOTES, 0.12);
      onOpened();
    } catch (e) {
      say(e instanceof Error ? e.message : String(e));
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  };

  const closeNewest = async () => {
    if (inFlight.current || !newest) return;
    inFlight.current = true;
    closedHere.current = String(newest.id);
    setBusy("close");
    setMsg(null);
    const pnl = watch?.pnl ?? newest.unrealized_pnl;
    try {
      await api.liveClose(newest.symbol, newest.asset_class as AssetClass, newest.direction);
      setFlash({
        dir: "close", text: `closed ≈ ${fmtUsd(pnl, { sign: true })}`, tone: pnl >= 0 ? "win" : "loss",
        party: { win: winnerOf(newest.direction === "short" ? "short" : "long", pnl), kind: "close" },
      });
      if (sound) void playCelebration(pnl).then((ok) => { if (!ok) play(CLOSE_NOTES, 0.1); });
      onOpened();
    } catch (e) {
      closedHere.current = null;
      say(`Close refused: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  };

  // The second click on the staged side sends; any other side click stages.
  const side = (dir: "long" | "short") => (pre?.dir === dir ? void send() : stage(dir));

  // The keyboard. Read through a ref, so the listener is attached once per arming and always acts on
  // the current pre-order. Captured before anything else: a focused button would otherwise "click"
  // on the same Enter or Space - and a focused BUY clicking on Space is the opposite of closing.
  const act = useRef<{ side: (d: "long" | "short") => void; space: () => void; drop: () => boolean; pick: (d: "long" | "short") => void }>(
    { side: () => {}, space: () => {}, drop: () => false, pick: () => {} },
  );
  act.current = {
    // Enter = BUY, 0 = SELL, as the buttons: the first press stages, the same key again sends. A key
    // never sends the other side: Enter on a staged SELL turns it into a BUY pre-order.
    side,
    space: () => void closeNewest(),
    drop: () => { if (!pre) return false; setPre(null); setMsg(null); return true; },
    // The side already staged keeps the levels you placed; the other side flips to it.
    pick: (d) => { if (pre?.dir !== d) stage(d); },
  };
  useEffect(() => {
    if (!keys) return;
    const onDown = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey || notForUs(e)) return;
      const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      // Delete (the numpad's Del / . too, beside its Enter and 0) or Esc drops the pre-order. Taken
      // only when there is one, so Esc still closes whatever else it closes.
      if (k === "Escape" || k === "Delete" || e.code === "NumpadDecimal") {
        if (act.current.drop()) { e.preventDefault(); e.stopPropagation(); }
        return;
      }
      const zero = k === "0" || e.code === "Numpad0";    // either 0 - the numpad's with Num Lock off too
      if (k !== "Enter" && k !== " " && k !== "b" && k !== "s" && !zero) return;
      e.preventDefault();
      e.stopPropagation();
      if (e.repeat) return;           // a held key fires once
      if (k === "Enter") act.current.side("long");
      else if (zero) act.current.side("short");
      else if (k === " ") act.current.space();
      else act.current.pick(k === "b" ? "long" : "short");
    };
    const onUp = (e: KeyboardEvent) => {
      // A focused button clicks on Space's keyUP; swallow it so Space only ever closes.
      if (e.key === " " && !notForUs(e)) { e.preventDefault(); e.stopPropagation(); }
    };
    window.addEventListener("keydown", onDown, true);
    window.addEventListener("keyup", onUp, true);
    return () => {
      window.removeEventListener("keydown", onDown, true);
      window.removeEventListener("keyup", onUp, true);
    };
  }, [keys]);

  // What the chart draws: the staged order and the banner saying what the next key does.
  const view: PreOrderView | null = pre && spec && plan && live ? {
    dir: pre.dir, entry: live.entry, sl: live.sl, tp: live.tp, lots: plan.lots, leverage: plan.leverage,
    slUsd: -plan.riskUsd, tpUsd: plan.rewardUsd, rr: plan.rr, bid: spec.bid, ask: spec.ask, why,
    hint: (kind, price) => {
      const sign = pre.dir === "long" ? 1 : -1;
      const sd = kind === "sl" ? (live.entry - price) * sign : live.stopDist;
      const td = kind === "tp" ? (price - live.entry) * sign : live.targetDist;
      if ((kind === "sl" ? sd : td) <= 0) return "wrong side of the market";
      const p = planScalp({ spec, ...base, stopDist: sd, targetDist: td });
      return kind === "sl"
        ? `${fmtUsd(-p.riskUsd)} · ${p.lots} lot · ${p.leverage.toFixed(1)}x`
        : `+${fmtUsd(p.rewardUsd)} · R:R ${p.rr.toFixed(2)}`;
    },
  } : null;
  const word = (d: "long" | "short") => (d === "long" ? "BUY" : "SELL");
  // A staged order speaks first; then the trade you're in, with its money; then the idle keys.
  const hud: ScalpHud | null = view
    ? {
        tone: view.dir,
        title: view.why
          ? `PRE-ORDER ${word(view.dir)} · not sendable (see the ticket)`
          : `${keys ? (view.dir === "long" ? "⏎ ENTER" : "0") : "Click again"} = ${word(view.dir)} ${view.lots} lot · ${view.leverage.toFixed(1)}x`,
        detail: `TP +${fmtUsd(view.tpUsd)} · SL ${fmtUsd(view.slUsd)} · R:R ${view.rr.toFixed(2)}${keys ? " · Del cancels" : ""}`,
      }
    : watch
      ? {
          tone: watch.pnl >= 0 ? "win" : "loss",
          big: fmtUsd(watch.pnl, { sign: true }),
          title: `${watch.dir === "long" ? "▲ LONG" : "▼ SHORT"} ${watch.lots}${watch.r != null ? ` · ${fmtR(watch.r)}` : ""} · ${watch.verdict.title}`,
          detail: `⏱ ${fmtDur(watch.secs)} · ${keys ? "SPACE closes · Enter buy · 0 sell" : "✕ CLOSE is in the ticket"}`,
        }
      : keys
        ? { tone: "idle", title: "⏎ ENTER = BUY · 0 = SELL", detail: "the first press stages the pre-order, the same key again sends it" }
        : null;
  const viewKey = JSON.stringify([view && { ...view, hint: null }, hud]);
  const onViewRef = useRef(onView);
  onViewRef.current = onView;
  useEffect(() => { onViewRef.current({ preOrder: view, hud }); }, [viewKey]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => onViewRef.current({ preOrder: null, hud: null }), []);

  const shownLev = plan ? Math.min(asked ?? plan.maxLeverage, plan.maxLeverage) : 0;
  const step = plan && plan.maxLeverage >= 10 ? 0.5 : 0.1;
  const sideButton = (dir: "long" | "short") => {
    const long = dir === "long";
    const armed = pre?.dir === dir;
    const flashing = flash?.dir === dir;
    const key = long ? "Enter" : "0";
    return (
      <button
        onClick={(e) => { e.currentTarget.blur(); side(dir); }}
        disabled={!spec || !!busy}
        title={
          armed
            ? `Send this ${word(dir)} now${keys ? ` (or press ${key})` : ""}: ${plan?.lots ?? 0} lot at market, with the stop and target on the chart. ${why ? `Not sendable: ${why}` : ""}`
            : `Stage a ${word(dir)} pre-order on the chart${keys ? ` (key: ${key})` : ""} - drag its stop and target, then click again${keys ? ` or press ${key}` : ""} to send`
        }
        className={`relative flex min-w-[6.75rem] flex-col items-center justify-center rounded-lg border-2 px-3 py-0.5 font-bold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ${
          long
            ? "border-bull bg-bull/20 text-bull hover:bg-bull/30 hover:shadow-[0_0_18px_-4px_rgba(38,166,154,0.8)]"
            : "border-bear bg-bear/20 text-bear hover:bg-bear/30 hover:shadow-[0_0_18px_-4px_rgba(239,83,80,0.8)]"
        } ${armed ? `animate-pulse ring-4 ${long ? "ring-bull/50 bg-bull/35" : "ring-bear/50 bg-bear/35"}` : ""} ${flashing ? "scale-105 ring-4 ring-amber-400/60" : ""}`}
      >
        <span className="text-[9px] uppercase leading-tight tracking-widest">
          {busy === dir ? "sending..." : flashing ? "filled"
            : armed ? `${keys ? (long ? "⏎ " : "0 ") : ""}send ${word(dir)}`
              : keys ? `${word(dir)} ${long ? "⏎" : "0"}` : word(dir)}
        </span>
        <span className="text-base tabular-nums leading-tight">
          {flashing ? flash.text : spec ? fmtPrice(long ? spec.ask : spec.bid, spec.digits) : "—"}
        </span>
      </button>
    );
  };

  // The one-glance state of the ticket, at the top of it.
  const state = day?.locked
    ? { cls: "bg-amber-500/20 text-amber-300", text: "🔒 LOCKED FOR TODAY" }
    : watch
      ? { cls: watch.pnl >= 0 ? "bg-bull/20 text-bull" : "bg-bear/20 text-bear", text: `● IN TRADE ${watch.dir === "long" ? "▲" : "▼"} ${fmtUsd(watch.pnl, { sign: true })}` }
      : pre
        ? { cls: pre.dir === "long" ? "bg-bull/20 text-bull" : "bg-bear/20 text-bear", text: `🎯 PRE-ORDER ${word(pre.dir)}` }
        : { cls: "bg-neutral-800 text-neutral-400", text: "READY · pick a side" };

  return (
    <div className="relative mb-1.5 shrink-0 overflow-hidden rounded-lg border border-amber-500/40 bg-gradient-to-br from-amber-500/[0.08] via-neutral-900/80 to-neutral-900/80 text-xs shadow-[0_0_28px_-14px_rgba(245,158,11,0.7)]">
      {/* Line 1: the mode and its state, how big the next order is, and the two sides. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-2.5 py-1">
        <span className="font-black uppercase italic tracking-wider text-amber-300" title="Scalp mode: manual trading on the fast charts. Every order goes with its stop and target, through the Risk Manager.">
          ⚡ Scalp
        </span>
        <span className={`rounded px-1.5 py-0.5 text-[11px] font-bold tracking-wide tabular-nums ${state.cls}`}>{state.text}</span>
        <span className="flex items-center gap-1" role="group" aria-label="Risk preset">
          {(Object.keys(SCALP_PRESETS) as ScalpLevel[]).map((k) => {
            const p = SCALP_PRESETS[k];
            return (
              <button
                key={k}
                onClick={(e) => { e.currentTarget.blur(); setLevel(k); }}
                aria-pressed={level === k}
                title={`${p.label}: a stopped-out trade loses ${+(riskOf(k) * 100).toFixed(2)}% of equity${spec?.equity ? ` (${fmtUsd(-(riskOf(k) * spec.equity))})` : ""}, the trade's leverage is capped at ${p.maxLev}x, and sending locks for the day after a ${stopPct[k] ?? p.dailyStop * 100}% loss${stopOn ? "" : " (daily stop OFF)"}.`}
                className={`rounded-md border px-1.5 py-0.5 font-semibold transition ${
                  level === k ? LEVEL_TONE[k] : "border-neutral-700 text-neutral-400 hover:text-neutral-200"
                }`}
              >
                {p.label} <span className="font-normal opacity-80">{+(riskOf(k) * 100).toFixed(2)}%</span>
              </button>
            );
          })}
        </span>
        <label
          className="flex items-center gap-1 text-[11px] text-neutral-400"
          title={`${preset.label}'s risk per trade: what a stopped-out trade loses, as a % of equity${spec?.equity ? ` (${fmtUsd(-(riskFrac * spec.equity))} now)` : ""}. Up to ${SCALP_MAX_RISK * 100}% - scalp mode's ceiling (RISK.md); its default is ${preset.risk * 100}%. Kept per preset.`}
        >
          risk
          <input
            type="number"
            min={0.1}
            max={SCALP_MAX_RISK * 100}
            step={0.1}
            value={+(riskFrac * 100).toFixed(2)}
            onChange={(e) => {
              const v = Number(e.target.value);
              if (Number.isFinite(v) && v >= 0.1 && v <= SCALP_MAX_RISK * 100) setRiskBy((m) => ({ ...m, [level]: v }));
            }}
            aria-label="Risk per trade percent"
            className={`w-12 rounded border bg-neutral-900 px-1 text-center tabular-nums ${riskFrac > 0.03 ? "border-amber-500/70 text-amber-200" : "border-neutral-700 text-neutral-200"}`}
          />
          %
        </label>
        <label className="flex min-w-[12rem] flex-1 items-center gap-2" title="Leverage: the trade's size against your equity. The size is capped so a stopped-out trade stays inside the preset, so a wider stop means a smaller size.">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500">Lev</span>
          <input
            type="range"
            name="scalp-leverage"
            min={step}
            max={Math.max(step, plan?.maxLeverage ?? step)}
            step={step}
            value={Math.max(step, shownLev)}
            disabled={!plan || plan.maxLeverage <= 0}
            onChange={(e) => {
              const v = Number(e.target.value);
              // At the top it follows the maximum, which moves with the stop.
              setLevBy((m) => {
                const next = { ...m };
                if (plan && v >= plan.maxLeverage - step / 2) delete next[level];
                else next[level] = v;
                return next;
              });
            }}
            onPointerUp={(e) => e.currentTarget.blur()}
            className="min-w-0 flex-1 accent-amber-500"
          />
          <span className="text-sm font-bold tabular-nums text-amber-200">{plan ? `${plan.leverage.toFixed(1)}x` : "—"}</span>
          <span className="text-[10px] text-neutral-500">{asked == null ? "max" : "of"} {plan ? `${plan.maxLeverage.toFixed(1)}x` : ""}</span>
        </label>
        <span
          className="tabular-nums text-neutral-400"
          title="The size the next order opens, its notional value and the margin it holds. The account leverage (1:…) Exness gives you only decides how much margin a position holds; it is set in the Exness Personal Area - the app can read it, not change it. The leverage bar is the trade's own size against your equity."
        >
          <b className="text-neutral-100">{plan ? plan.lots : "—"} lot</b>
          {plan && plan.lots > 0 && <> · {fmtUsd(plan.notional)}{plan.margin != null && <> · mgn {fmtUsd(plan.margin)}</>}</>}
          {spec && <span className="text-neutral-500"> · 1:{spec.account_leverage ?? "?"} · free {fmtUsd(spec.margin_free)}</span>}
        </span>
        <span className="ml-auto flex items-stretch gap-1.5">
          {sideButton("short")}
          <span className="flex flex-col items-center justify-center text-[9px] leading-tight text-neutral-500" title="Ask minus bid: what every scalp pays up front, win or lose">
            spread
            <b className="text-[11px] tabular-nums text-neutral-300">{spec ? distText(spec.spread, spec, assetClass) : "—"}</b>
          </span>
          {sideButton("long")}
        </span>
        <span className="flex items-center gap-2">
          <button
            onClick={(e) => { e.currentTarget.blur(); setKeys((v) => !v); }}
            aria-pressed={keys}
            title={
              keys
                ? "Keys armed on this tab: Enter = BUY · 0 = SELL (the first press stages the pre-order on the chart, the same key again sends it) · Space closes the newest trade on this chart · Delete or Esc drops the pre-order · B / S stage a side. Never while typing in a field or inside a drawer. Click to disarm."
                : "Arm the trading keys: Enter = buy · 0 = sell (press twice: stage, then send) · Space = close the newest trade on this chart · Delete / Esc = cancel · B / S = side"
            }
            className={`rounded border px-1.5 py-0.5 font-semibold transition ${
              keys ? "border-amber-400 bg-amber-500/20 text-amber-200" : "border-neutral-700 text-neutral-500 hover:text-neutral-200"
            }`}
          >
            ⌨ {keys ? "ON" : "off"}
          </button>
          <button
            onClick={(e) => { e.currentTarget.blur(); setSound((v) => !v); }}
            title={sound ? "Sounds on - click to mute" : "Sounds muted - click to turn them on"}
            className="text-neutral-500 hover:text-neutral-200"
          >
            {sound ? "🔊" : "🔇"}
          </button>
          <button
            onClick={(e) => { e.currentTarget.blur(); setTug((v) => !v); }}
            aria-pressed={tug}
            title={tug
              ? "Bull vs bear: a tug of war for the price on the trade bar - they tire as it nears their end, and the winner celebrates when the trade closes. Click to switch it off."
              : "Bull vs bear tug of war on the trade bar is off - click to switch it on"}
            className={`transition ${tug ? "" : "opacity-40 grayscale hover:opacity-80"}`}
          >
            🐂
          </button>
          <button
            onClick={(e) => { e.currentTarget.blur(); setFold((v) => !v); }}
            aria-expanded={!fold}
            title={fold ? "Show the whole ticket: stop, target, today's score and the readings" : "Fold the ticket to this one line - more room for the chart. The trade and the pre-order stay in view."}
            className="rounded border border-neutral-700 px-1.5 py-0.5 text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
          >
            {fold ? "▾" : "▴"}
          </button>
        </span>
      </div>

      {/* Line 2: the stop and the target, and how the day is going. Folds away with the readings. */}
      {!fold && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-neutral-800/60 px-2.5 py-1">
          <span
            className="flex flex-wrap items-center gap-1"
            title={`Stops in ATR of the ${stopTf} bars${atr && spec ? ` (ATR ${fmtPrice(atr, spec.digits)})` : ""}. BUY / SELL stages the order on the chart first; nothing is sent until the second click${keys ? " or Enter" : ""}.`}
          >
            <span className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500">Stop</span>
            {STOP_ATR_CHOICES.map((x) => (
              <button
                key={x}
                onClick={(e) => { e.currentTarget.blur(); pickStop(x); }}
                aria-pressed={stopAtr === x}
                title={`Stop ${x} × the ATR of the ${stopTf} bars${pre ? " - moves the staged stop and target there" : ""}`}
                className={`rounded px-1.5 py-0.5 tabular-nums ${stopAtr === x ? "bg-neutral-700 text-white" : "text-neutral-400 hover:bg-neutral-800"}`}
              >
                {x}×
              </button>
            ))}
            {plan && spec && plan.stopDist > 0 && (
              <span className="ml-1 tabular-nums text-neutral-300">
                {distText(plan.stopDist, spec, assetClass)}
                {plan.lots > 0 && <> · <b className="text-bear">{fmtUsd(-plan.riskUsd)}</b> ({(plan.riskPct * 100).toFixed(2)}%)</>}
                {plan.widened && (
                  <span
                    className="ml-1 rounded bg-amber-500/15 px-1 text-amber-300"
                    title={`Widened past ${stopAtr}× ATR: the Risk Manager refuses a trade whose spread is over ${Math.round((spreadGate ?? 0) * 100)}% of the stop, and the broker needs a minimum distance. A wider stop means a smaller size at the same risk.`}
                  >
                    widened for the spread
                  </span>
                )}
                {pre && <span className="ml-1 text-neutral-500">(yours, on the chart)</span>}
              </span>
            )}
          </span>
          <span className="flex flex-wrap items-center gap-1">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-neutral-500">Target</span>
            {TARGET_R_CHOICES.map((x) => (
              <button
                key={x}
                onClick={(e) => { e.currentTarget.blur(); pickTarget(x); }}
                aria-pressed={!pre && targetR === x}
                title={`Target ${x} × the stop distance${pre ? " - moves the staged target there" : ""}`}
                className={`rounded px-1.5 py-0.5 tabular-nums ${!pre && targetR === x ? "bg-neutral-700 text-white" : "text-neutral-400 hover:bg-neutral-800"}`}
              >
                {x}R
              </button>
            ))}
            {plan && plan.lots > 0 && (
              <span className="ml-1 tabular-nums">
                <b className="text-bull">+{fmtUsd(plan.rewardUsd)}</b>
                <span className="ml-1.5 text-neutral-400">R:R {plan.rr.toFixed(2)}</span>
              </span>
            )}
          </span>
          <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 tabular-nums text-neutral-400">
            <span title="Today's trades tagged scalp (UTC day), from the journal">
              Today <b className="text-neutral-200">{scalps.length}</b>
              {scalps.length > 0 && (
                <>
                  {" "}· <span className="text-bull">{wins}W</span> <span className="text-bear">{losses}L</span> ·{" "}
                  <b className={net >= 0 ? "text-bull" : "text-bear"}>{fmtUsd(net, { sign: true })}</b>
                  {streak >= 2 && <span className="ml-1 text-amber-300">🔥 {streak}</span>}
                  {streak <= -2 && <span className="ml-1 text-sky-300" title="Losses in a row. The daily stop is there for exactly this - and the next trade is not owed a win.">❄ {-streak}</span>}
                </>
              )}
            </span>
            {day && (
              <span
                className={`flex items-center gap-1.5 ${!day.on ? "text-neutral-500" : day.locked ? "text-bear" : day.lost > day.limit * 0.5 ? "text-amber-300" : ""}`}
                title={`Today's realized P&L on the whole account (your strategies' trades included) against ${preset.label}'s daily stop (${day.pct}% of the equity the day started with). Past it, sending locks until 00:00 UTC.`}
              >
                Day {fmtUsd(-day.lost)} / {fmtUsd(-day.limit)}
                <span className="h-1.5 w-10 overflow-hidden rounded-full bg-neutral-800">
                  <span
                    className={`block h-full ${!day.on ? "bg-neutral-500" : day.locked ? "bg-bear" : day.lost > day.limit * 0.5 ? "bg-amber-400" : "bg-emerald-500/70"}`}
                    style={{ width: `${Math.min(1, day.limit > 0 ? day.lost / day.limit : 0) * 100}%` }}
                  />
                </span>
                <input
                  type="number"
                  min={0.5}
                  max={30}
                  step={0.5}
                  value={day.pct}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    if (Number.isFinite(v) && v >= 0.5 && v <= 30) setStopPct((m) => ({ ...m, [level]: v }));
                  }}
                  className="w-12 rounded border border-neutral-700 bg-neutral-900 px-1 text-center tabular-nums text-neutral-200"
                  title={`${preset.label}'s daily stop, % of the day's starting equity (its default is ${preset.dailyStop * 100}%). Kept per preset.`}
                  aria-label="Daily stop percent"
                />
                %
                <button
                  onClick={(e) => { e.currentTarget.blur(); setStopOn((v) => !v); }}
                  aria-pressed={day.on}
                  className={`rounded border px-1.5 font-semibold ${day.on ? "border-neutral-700 text-neutral-300 hover:text-white" : "border-amber-400 bg-amber-500/20 text-amber-200"}`}
                  title={day.on
                    ? "Daily stop ON: past the limit, sending locks until 00:00 UTC. Click to switch it off."
                    : "Daily stop OFF: nothing stops manual scalping on a bad day (the backend daily-loss breaker is a separate switch, in Risk). Click to switch it back on."}
                >
                  {day.on ? "🔒 on" : "⚠ OFF"}
                </button>
              </span>
            )}
          </span>
        </div>
      )}

      {pre && plan && spec && live && (() => {
        // The pre-order: both outcomes in dollars, and a bar whose two halves are their sizes.
        const loss = plan.riskUsd, gain = plan.rewardUsd;
        const total = loss + gain || 1;
        const pin = (lv: PreOrder["sl"] | PreOrder["tp"]) =>
          "price" in lv
            ? <span className="ml-0.5" title="Placed by you: it stays at this price while the market moves">📌</span>
            : <span className="ml-0.5 text-neutral-600" title={"dist" in lv ? "Follows the price at this distance until you drag it" : "Stays at this R from the stop until you drag it"}>↕</span>;
        return (
          <div className={`flex flex-wrap items-center gap-x-4 gap-y-1 border-t px-2.5 py-1 ${pre.dir === "long" ? "border-bull/40 bg-bull/[0.07]" : "border-bear/40 bg-bear/[0.07]"}`}>
            <span className={`font-black italic tracking-wide ${pre.dir === "long" ? "text-bull" : "text-bear"}`}>
              🎯 PRE-ORDER {pre.dir === "long" ? "▲ BUY" : "▼ SELL"} {plan.lots} lot
            </span>
            <span className="tabular-nums text-neutral-400">fill ≈ <b className="text-neutral-200">{fmtPrice(live.entry, spec.digits)}</b></span>
            <span className="tabular-nums text-neutral-400">
              SL <b className="text-neutral-200">{fmtPrice(live.sl, spec.digits)}</b>{pin(pre.sl)} <b className="text-sm text-bear">{fmtUsd(-loss)}</b>
            </span>
            <span className="tabular-nums text-neutral-400">
              TP <b className="text-neutral-200">{fmtPrice(live.tp, spec.digits)}</b>{pin(pre.tp)} <b className="text-sm text-bull">+{fmtUsd(gain)}</b>
            </span>
            <span className="flex min-w-[8rem] flex-1 items-center gap-2" title={`Risk against reward: ${fmtUsd(loss)} at the stop, ${fmtUsd(gain)} at the target`}>
              <span className="flex h-2 flex-1 overflow-hidden rounded-full bg-neutral-800">
                <span className="h-full bg-bear/80" style={{ width: `${(loss / total) * 100}%` }} />
                <span className="h-full bg-bull/80" style={{ width: `${(gain / total) * 100}%` }} />
              </span>
              <b className="tabular-nums text-neutral-200">R:R {plan.rr.toFixed(2)}</b>
            </span>
            <span className={why ? "text-amber-300" : "text-neutral-500"}>
              {stacked ?? (blocked ? "" : `${keys ? `${pre.dir === "long" ? "⏎ Enter" : "0"} sends · Del cancels` : "click the lit side again to send"} · drag SL / TP on the chart`)}
            </span>
            <button
              onClick={(e) => { e.currentTarget.blur(); setPre(null); setMsg(null); }}
              className="rounded border border-neutral-700 px-1.5 py-0.5 text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
              title={`Drop this pre-order${keys ? " (Delete or Esc)" : ""}`}
            >
              ✕ cancel
            </button>
          </div>
        );
      })()}

      {watch && (
        <TradePanel
          w={watch}
          digits={spec?.digits ?? null}
          more={mine.length - 1}
          keys={keys}
          busy={busy === "close"}
          tug={tug}
          onClose={() => void closeNewest()}
        />
      )}

      {!fold && (
        <div className="flex flex-wrap items-center gap-1 border-t border-neutral-800/80 bg-neutral-950/40 px-2.5 py-1">
          <span
            className="cursor-help pr-1 text-[10px] uppercase tracking-wide text-neutral-500"
            title="Readings of the candles, not signals: none of them was tested as an entry rule here. Colour only says which side each leans to. Hover any one for what it measures."
          >
            {stopTf} read
            {atr != null && spec && <b className="ml-1 normal-case tracking-normal text-neutral-300">ATR {fmtPrice(atr, spec.digits)}</b>}
          </span>
          {items.map((it) => <Gauge key={it.label} it={it} />)}
          {!items.length && <span className="text-neutral-600">Reading the {stopTf} candles…</span>}
        </div>
      )}

      {blocked && (
        <div className="border-t border-amber-500/30 bg-amber-500/10 px-2.5 py-1 text-amber-300">🔒 {blocked}</div>
      )}
      {msg && (
        <div className="flex items-start justify-between gap-3 border-t border-bear/30 bg-bear/10 px-2.5 py-1 text-bear">
          <span>⚠ {msg}</span>
          <button onClick={() => setMsg(null)} className="text-bear/80 hover:text-bear" aria-label="Dismiss">×</button>
        </div>
      )}
      {flash?.dir === "close" && (() => {
        const row = (
          <div
            className={`border-t px-2.5 py-1 text-sm font-black tracking-wide ${
              flash.tone === "target"
                ? "border-amber-400/50 bg-amber-400/15 text-amber-200"
                : flash.tone === "win"
                  ? "border-bull/40 bg-bull/10 text-bull"
                  : flash.tone === "loss"
                    ? "border-bear/40 bg-bear/10 text-bear"
                    : "border-sky-500/30 bg-sky-500/10 text-sky-300"
            }`}
          >
            ✓ Trade {flash.text}
          </div>
        );
        // The bull vs bear ending, or - with it off, another trade still open here, or if it ever breaks -
        // the plain line.
        return tug && flash.party && !watch ? (
          <ErrorBoundary name="trade ending" fallback={row}>
            <TugParty win={flash.party.win} kind={flash.party.kind} text={flash.text} />
            {flash.party.kind === "target" && <TugConfetti win={flash.party.win} />}
          </ErrorBoundary>
        ) : row;
      })()}
    </div>
  );
}

// One reading as a small instrument: its name and value on one line, and a hairline under them for
// where that value sits on its scale.
function Gauge({ it }: { it: ScalpReading }) {
  const m = it.meter;
  const frac = m ? Math.max(0, Math.min(1, (m.v - m.lo) / (m.hi - m.lo))) : null;
  return (
    <span title={it.tip} className={`relative flex cursor-help items-baseline gap-1.5 rounded px-1.5 pb-1 pt-0.5 ${READ_TONE[it.tone]}`}>
      <span className="text-[10px] uppercase tracking-wide opacity-70">{it.label}</span>
      <b className="tabular-nums">{it.value}</b>
      {m && frac != null && (
        <span className="absolute inset-x-1.5 bottom-0.5 h-[2px] rounded-full bg-neutral-950/70">
          <span
            className="absolute inset-y-0 rounded-full bg-current opacity-80"
            style={m.centre ? { left: `${Math.min(0.5, frac) * 100}%`, width: `${Math.abs(frac - 0.5) * 100}%` } : { left: 0, width: `${frac * 100}%` }}
          />
          {(m.centre ? [(m.lo + m.hi) / 2] : m.marks ?? []).map((k) => (
            <span key={k} className="absolute -top-px h-1 w-px bg-neutral-400/70" style={{ left: `${((k - m.lo) / (m.hi - m.lo)) * 100}%` }} />
          ))}
        </span>
      )}
    </span>
  );
}

// The trade you are in, in two lines: the money and the stop-to-target track, then hold-or-close.
function TradePanel({ w, digits, more, keys, busy, tug, onClose }: {
  w: TradeWatch;
  digits: number | null;
  more: number;          // other trades open on this pair
  keys: boolean;
  busy: boolean;
  tug: boolean;          // the bull vs bear tug of war along the bar
  onClose: () => void;
}) {
  const long = w.dir === "long";
  const win = w.pnl >= 0;
  const clamp = (x: number) => Math.max(0, Math.min(1, x));
  const pct = (x: number) => `${clamp(x) * 100}%`;
  const px = (v: number | null) => fmtPrice(v, digits);
  return (
    <div className={`border-t px-2.5 py-1 ${win ? "border-bull/40 bg-bull/[0.06]" : "border-bear/40 bg-bear/[0.06]"}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`font-black italic tracking-wide ${long ? "text-bull" : "text-bear"}`}>
          ● {long ? "▲ LONG" : "▼ SHORT"} {w.lots}
        </span>
        <span className="tabular-nums text-neutral-400" title={`Entry, then the price it would close at now (${long ? "the bid: where a long closes" : "the ask: where a short closes"})`}>
          {px(w.entry)} → <b className="text-neutral-200">{px(w.price)}</b>
        </span>
        <span className="tabular-nums text-neutral-400" title="Time in the trade">⏱ {fmtDur(w.secs)}</span>
        {more > 0 && <span className="text-neutral-500" title="Space and CLOSE take the newest">+{more} more open</span>}
        {w.at != null && w.entryAt != null ? (
          // Stop on the left, target on the right, whichever side the trade is: left is always the
          // loss, right the gain. The bright stretch is how far price has gone from the entry.
          <div
            className={`${tug ? "min-w-[22rem]" : "min-w-[14rem]"} flex-1`}
            title={`Where price sits between your stop (left) and your target (right). White = now, grey line = entry, gold tick = the best so far.${tug ? ` Bull vs bear: your ${long ? "bull" : "bear"} holds the target end; each one tires as price nears its end.` : ""}`}
          >
            {(() => {
              const track = (
                <div className="relative h-4">
                  <div className="absolute inset-x-0 top-1 h-2 overflow-hidden rounded-full bg-neutral-800">
                    <div className="absolute inset-y-0 left-0 bg-bear/35" style={{ width: pct(w.entryAt) }} />
                    <div className="absolute inset-y-0 right-0 bg-bull/25" style={{ left: pct(w.entryAt) }} />
                    <div
                      className={`absolute inset-y-0 transition-all duration-500 ${w.at >= w.entryAt ? "bg-bull" : "bg-bear"}`}
                      style={{ left: pct(Math.min(w.at, w.entryAt)), width: `${Math.abs(clamp(w.at) - clamp(w.entryAt)) * 100}%` }}
                    />
                    {tug && <div className="tug-rope" />}
                  </div>
                  {w.troughAt != null && <span className="absolute top-0.5 h-3 w-0.5 -translate-x-1/2 bg-neutral-500" style={{ left: pct(w.troughAt) }} />}
                  {w.peakAt != null && <span className="absolute top-0.5 h-3 w-0.5 -translate-x-1/2 bg-amber-300" style={{ left: pct(w.peakAt) }} />}
                  <span className="absolute top-0 h-4 w-0.5 -translate-x-1/2 bg-neutral-300" style={{ left: pct(w.entryAt) }} />
                  <span
                    className="absolute -top-0.5 h-5 w-1.5 -translate-x-1/2 rounded-sm bg-white shadow-[0_0_10px_rgba(255,255,255,0.9)] transition-[left] duration-500"
                    style={{ left: pct(w.at) }}
                  />
                </div>
              );
              // Dressing only: if the tug of war ever breaks, the plain bar stands in for it.
              return tug ? (
                <ErrorBoundary name="tug of war" fallback={track}>
                  <TugOfWar dir={w.dir} at={w.at} entryAt={w.entryAt}>{track}</TugOfWar>
                </ErrorBoundary>
              ) : track;
            })()}
            <div className="flex items-center justify-between gap-2 text-[10px] leading-tight tabular-nums">
              <span className="text-bear">SL {px(w.sl)} {w.slUsd != null && <b>{fmtUsd(w.slUsd, { sign: true })}</b>}</span>
              <span className="text-neutral-500">
                best <b className="text-amber-300">{fmtUsd(w.peak, { sign: true })}</b> · worst <b className="text-neutral-400">{fmtUsd(w.trough, { sign: true })}</b>
              </span>
              <span className="text-bull">TP {px(w.tp)} {w.tpUsd != null && <b>{fmtUsd(w.tpUsd, { sign: true })}</b>}</span>
            </div>
          </div>
        ) : (
          <span className="min-w-[14rem] flex-1 text-[11px] text-amber-300">
            {w.sl == null ? "No stop on this trade" : "No target on this trade"} - drag {w.sl == null ? "an SL" : "a TP"} line onto the chart to set one.
          </span>
        )}
        <span className="flex items-baseline gap-2">
          <span
            className={`text-2xl font-black tabular-nums leading-none ${win ? "text-bull drop-shadow-[0_0_10px_rgba(38,166,154,0.55)]" : "text-bear drop-shadow-[0_0_10px_rgba(239,83,80,0.55)]"}`}
            title="Live P&L at the price it would close at now (the bid for a long, the ask for a short), so the spread is already in it. Swap and commission are not."
          >
            {fmtUsd(w.pnl, { sign: true })}
          </span>
          {w.r != null && (
            <span
              className={`text-sm font-bold tabular-nums ${w.r >= 0 ? "text-bull" : "text-bear"}`}
              title="Profit in R: multiples of the risk taken at entry (entry to the stop the trade opened with). -1R is a full stop-out."
            >
              {fmtR(w.r)}
            </span>
          )}
        </span>
        <button
          onClick={(e) => { e.currentTarget.blur(); onClose(); }}
          disabled={busy}
          title={`Close this ${long ? "long" : "short"} at market now - one click, no confirm${keys ? " (or Space)" : ""}`}
          className="rounded-lg border-2 border-sky-400/70 bg-sky-500/15 px-3 py-0.5 text-sm font-black tracking-wide text-sky-200 transition hover:bg-sky-500/30 active:scale-95 disabled:opacity-50"
        >
          {busy ? "closing..." : `✕ CLOSE${keys ? " ␣" : ""}`}
        </button>
      </div>

      <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px]">
        <span
          className={`cursor-help rounded px-1.5 py-px text-xs font-black tracking-wide ring-1 ring-inset ${VERDICT_TONE[w.verdict.tone]}`}
          title="Hold or close, from the checklist beside it: the usual scalping rules of thumb, each judged against YOUR side (green = supports holding). None of them was backtested on this account, so it is what to look at, not a signal. Your stop and target close the trade on their own."
        >
          {w.verdict.title}
        </span>
        <span className="mr-1 text-neutral-400">{w.verdict.detail}</span>
        {w.cues.map((c) => (
          <span key={c.key} title={c.tip} className={`cursor-help rounded px-1.5 py-px tabular-nums ${CUE_TONE[c.tone]}`}>
            <span className="opacity-70">{c.label}</span> {c.value}
          </span>
        ))}
      </div>
    </div>
  );
}
