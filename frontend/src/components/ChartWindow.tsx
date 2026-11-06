import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { displaySymbol, fmtPrice } from "../format";
import { usePolling } from "../hooks/usePolling";
import { useQuoteSocket } from "../hooks/useQuoteSocket";
import { useScalpSpec, type PreOrder, type PreOrderView, type ScalpHud } from "../scalp";
import { SCALP_FAST, SCALP_TFS, TIMEFRAMES } from "../timeframes";
import type { AssetClass, PositionView } from "../types";
import { Chart, type Pulse } from "./Chart";
import { ChartPositionBar } from "./ChartPositionBar";
import { DESK_CHANNEL } from "./Drawer";
import { MarketHoursChip } from "./MarketHoursChip";
import { PairMonitor } from "./PairMonitor";
import { ScalpTicket } from "./ScalpTicket";
import { StrategyWidget } from "./StrategyWidget";
import { TimeframeBadge, TimeframeSwitch } from "./TimeframeSwitch";

/**
 * A CHART WINDOW: one pair's chart alone in its own browser tab, at /?chart=XAUUSDm&ac=metal&tf=1h.
 *
 * Opened from the desk's "⧉ New tab", so a pair can be watched on several timeframes at once (the 1h,
 * the 15m and the 5s side by side) - and with a second screen connected it opens there, as a window
 * filling it. It is what the desk's full-screen chart is - the pair's timeframe strip, the open trade's
 * strip, its lines, tickets and drawings, the strategies pop-up - and nothing else: no header, no book,
 * no analysis. Picking a pair in the strategies pop-up switches this window to it.
 *
 * Two things it deliberately does NOT do:
 * - Touch the desk's saved pair and timeframe (ta.symbol / ta.timeframe). The pair and timeframe live
 *   in this tab's URL, so a window can never move the desk, and a reload reopens the same chart.
 * - Watch price alerts or target hits. The desk tab does that, once; a watcher in every window would
 *   chime every alert once per window. Alerts set or moved here still reach it (shared storage).
 * Scalp mode is per window too (scalp=1 in the URL): a 10s scalp window next to a plain 1h one.
 */
export function chartWindowUrl(symbol: string, assetClass: string, timeframe: string, scalp = false): string {
  const q = new URLSearchParams({ chart: symbol, ac: assetClass, tf: timeframe });
  if (scalp) q.set("scalp", "1");
  return `${window.location.pathname}?${q}`;
}

function readParams() {
  const q = new URLSearchParams(window.location.search);
  const tf = q.get("tf") ?? "";
  return {
    symbol: q.get("chart") ?? "",
    assetClass: (q.get("ac") || "forex") as AssetClass,
    timeframe: TIMEFRAMES.includes(tf) ? tf : "1h",
    scalp: q.get("scalp") === "1",
  };
}

// The Strategies board is a drawer of the desk: ask the desk tab (on the other screen, say) to open it,
// and open the desk with it when no desk tab answers.
function openDeskBoard() {
  const desk = `${window.location.pathname}?drawer=strategy`;
  if (typeof BroadcastChannel === "undefined") {
    window.open(desk, "_blank");
    return;
  }
  const ch = new BroadcastChannel(DESK_CHANNEL);
  let heard = false;
  ch.onmessage = (e: MessageEvent<{ type?: string }>) => { if (e.data?.type === "opened") heard = true; };
  ch.postMessage({ type: "open-drawer", drawer: "strategy" });
  window.setTimeout(() => {
    ch.close();
    if (!heard) window.open(desk, "_blank");
  }, 600);
}

export function ChartWindow() {
  // The pair comes from the link (or the strategies pop-up); the timeframe and scalp mode are this
  // window's own.
  const [{ symbol, assetClass }, setPair] = useState(readParams);
  const [timeframe, setTimeframe] = useState(() => readParams().timeframe);
  const [scalp, setScalp] = useState(() => readParams().scalp);
  const [notice, setNotice] = useState<string | null>(null);
  const [pulse, setPulse] = useState<Pulse | null>(null);
  const [posBump, setPosBump] = useState(0);
  const [condKey, setCondKey] = useState(0);

  // The URL and the tab title follow the timeframe: a reload reopens this chart, and a row of tabs
  // reads "XAUUSD · 1h", "XAUUSD · 15m", "⚡ XAUUSD · 5s".
  useEffect(() => {
    window.history.replaceState(null, "", chartWindowUrl(symbol, assetClass, timeframe, scalp));
    document.title = `${scalp ? "⚡ " : ""}${displaySymbol(symbol)} · ${timeframe}`;
  }, [symbol, assetClass, timeframe, scalp]);

  const toggleScalp = () => {
    if (!scalp && !SCALP_FAST.includes(timeframe)) setTimeframe("1m");
    setScalp(!scalp);
  };

  const liveQuote = useQuoteSocket(symbol, assetClass);
  const { spec: scalpSpec, error: scalpSpecErr } = useScalpSpec(symbol, assetClass, scalp);
  // The staged scalp order and what the ticket says the chart should draw for it (see the desk).
  const [preOrder, setPreOrder] = useState<PreOrder | null>(null);
  const [scalpView, setScalpView] = useState<{ preOrder: PreOrderView | null; hud: ScalpHud | null }>({ preOrder: null, hud: null });
  useEffect(() => setPreOrder(null), [scalp, symbol]);
  const { data: positions } = usePolling(() => api.livePositions(), 4000, [posBump]);
  const { data: conditionals } = usePolling(() => api.conditionals(), 10000, [condKey]);
  const { data: account } = usePolling(() => api.account(assetClass), 10000, [assetClass]);
  const { data: settings } = usePolling(() => api.settings(), 30000);
  const armed = (conditionals ?? []).filter((c) => c.status === "armed");
  const position = (positions ?? []).find((p) => p.symbol.toUpperCase() === symbol.toUpperCase()) ?? null;

  // Browser full screen of the whole window - on a second screen that is the chart and nothing else.
  const [full, setFull] = useState(false);
  useEffect(() => {
    const onChange = () => setFull(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);
  const toggleFull = () => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    else void document.documentElement.requestFullscreen().catch(() => {});
  };

  const refused = (what: string, e: unknown) =>
    setNotice(`${what}: ${e instanceof Error ? e.message : String(e)}`);

  const closePosition = async (p: PositionView) => {
    try {
      await api.liveClose(p.symbol, p.asset_class as AssetClass, p.direction);
    } catch (e) {
      refused(`Close ${p.symbol} refused`, e);
    }
    setPosBump((b) => b + 1);
  };

  const setSlTp = async (sl: number | null, tp: number | null, direction?: string) => {
    try {
      const res = await api.setSlTp(symbol, assetClass, sl, tp, direction);
      setNotice(res.warning ? `${symbol}: ${res.warning}` : null);
    } catch (e) {
      refused(`SL/TP change on ${symbol} refused`, e);
    }
    setPosBump((b) => b + 1);     // a refused drag snaps back to the stop the broker really has
  };

  if (!symbol) {
    return <div className="p-6 text-sm text-neutral-400">No pair in this link. Open a chart window from the desk's ⧉ New tab.</div>;
  }

  return (
    <div className={`flex h-screen flex-col overflow-hidden bg-neutral-950 p-3${scalp ? " shadow-[inset_0_0_0_1px_rgba(245,158,11,0.45)]" : ""}`}>
      <div className={`${scalp ? "mb-1.5" : "mb-2"} flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2`}>
        <span className="text-sm font-semibold">
          {displaySymbol(symbol)} · {timeframe}
        </span>
        {liveQuote && (
          <span className="text-sm font-semibold tabular-nums text-neutral-100" title={`${liveQuote.symbol} live price`}>
            {fmtPrice(liveQuote.price)}
          </span>
        )}
        <MarketHoursChip symbol={symbol} assetClass={assetClass} />
        <ScalpToggle on={scalp} onToggle={toggleScalp} />
        <span className="ml-auto text-[11px] text-neutral-600" title="This tab has its own pair and timeframe. The desk keeps yours, and watches the price alerts.">
          Chart window
        </span>
        <span className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          <TimeframeBadge tf={timeframe} compact={scalp} />
          <TimeframeSwitch value={timeframe} onChange={setTimeframe} options={scalp ? SCALP_TFS : TIMEFRAMES} scalp={scalp} />
        </span>
      </div>
      {notice && (
        <div className={`flex shrink-0 items-start justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 text-amber-300 ${scalp ? "mb-1.5 py-1 text-xs" : "mb-2 py-1.5 text-sm"}`}>
          <span>⚠ {notice}</span>
          <button onClick={() => setNotice(null)} className="text-amber-300/80 hover:text-amber-200" aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
      <PairMonitor
        symbol={symbol}
        assetClass={assetClass}
        timeframe={timeframe}
        selectable={scalp ? SCALP_TFS : TIMEFRAMES}
        onTimeframe={setTimeframe}
        position={position}
      />
      {scalp && (
        <ScalpTicket
          symbol={symbol}
          assetClass={assetClass}
          timeframe={timeframe}
          spec={scalpSpec}
          specError={scalpSpecErr}
          positions={positions}
          pre={preOrder}
          setPre={setPreOrder}
          onView={setScalpView}
          onOpened={() => setPosBump((b) => b + 1)}
        />
      )}
      {/* In scalp mode the ticket's trade panel shows the trade instead. */}
      {!scalp && (
        <div className="shrink-0">
          <ChartPositionBar
            pos={position}
            pulse={pulse}
            equity={account?.equity ?? null}
            riskCap={settings?.risk.risk_per_trade ?? null}
            onClose={closePosition}
          />
        </div>
      )}
      <Chart
        symbol={symbol}
        assetClass={assetClass}
        timeframe={timeframe}
        proposal={null}
        liveQuote={liveQuote}
        positions={positions}
        armed={armed}
        onSetSlTp={setSlTp}
        onSetArmedLevels={async (id, levels) => {
          try {
            await api.setConditionalLevels(id, levels);
          } catch (e) {
            refused("Setup change refused", e);
          }
          setCondKey((v) => v + 1);
        }}
        fill
        isFullscreen={full}
        onToggleFullscreen={toggleFull}
        onArmed={() => setCondKey((v) => v + 1)}
        onOpened={() => setPosBump((b) => b + 1)}
        onPulse={setPulse}
        scalp={scalp}
        bidAsk={scalpSpec ? { bid: scalpSpec.bid, ask: scalpSpec.ask } : null}
        preOrder={scalp ? scalpView.preOrder : null}
        onPreOrderDrag={(kind, price) => setPreOrder((p) => (p ? { ...p, [kind]: { price } } : p))}
        hud={scalp ? scalpView.hud : null}
        strategies={
          <StrategyWidget
            positions={positions}
            chartSymbol={symbol}
            onPick={(sym, ac) => setPair((p) => ({ ...p, symbol: sym, assetClass: ac }))}
            onOpenBoard={openDeskBoard}
          />
        }
      />
    </div>
  );
}

/** The ⚡ Scalp switch, beside the pair on the desk and in every chart window. */
export function ScalpToggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      aria-pressed={on}
      title={
        on
          ? "Leave scalp mode: back to the normal chart and timeframes"
          : "Scalp mode: the fast charts (5s-10m), fast indicators, and a one-click BUY/SELL ticket sized from a leverage and a risk preset - every order with its stop and target"
      }
      className={`shrink-0 rounded border px-2 py-0.5 text-xs font-bold italic tracking-wide transition ${
        on
          ? "border-amber-400 bg-amber-500/20 text-amber-200 shadow-[0_0_14px_-3px_rgba(245,158,11,0.8)]"
          : "border-neutral-700 text-neutral-300 hover:border-amber-500/70 hover:text-amber-200"
      }`}
    >
      ⚡ {on ? "SCALP ON" : "Scalp"}
    </button>
  );
}

// The browser's multi-screen window placement (Chrome): which screens there are, and where.
type ScreenArea = { left: number; top: number; availLeft: number; availTop: number; availWidth: number; availHeight: number };
type ScreenDetails = { screens: ScreenArea[]; currentScreen: ScreenArea };
const screenDetails = () => (window as Window & { getScreenDetails?: () => Promise<ScreenDetails> }).getScreenDetails;
const twoScreens = () => (window.screen as Screen & { isExtended?: boolean }).isExtended === true;

// A chart window filling ANOTHER screen. Chrome asks once to let the desk "manage windows on all your
// displays"; false when it can't (one screen, refused, another browser) - the caller opens a tab.
async function openOnOtherScreen(url: string): Promise<boolean> {
  const get = screenDetails();
  if (!twoScreens() || !get) return false;
  try {
    const { screens, currentScreen } = await get.call(window);
    const other = screens.find((x) => x.left !== currentScreen.left || x.top !== currentScreen.top);
    if (!other) return false;
    const opened = window.open(url, "_blank",
      `popup,left=${other.availLeft},top=${other.availTop},width=${other.availWidth},height=${other.availHeight}`);
    return !!opened;
  } catch {
    return false;
  }
}

/**
 * The desk's "⧉ New tab": opens the charted pair alone in a chart window, on any timeframe - on the
 * other screen, filling it, when one is connected. Real links, so a middle-click, Ctrl+click or
 * Shift+click does what it does on any link.
 */
export function ChartWindowMenu({ symbol, assetClass, timeframe, scalp = false }: {
  symbol: string;
  assetClass: string;
  timeframe: string;
  scalp?: boolean;   // open the windows in scalp mode, as the desk is
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const other = twoScreens() && !!screenDetails();
  useEffect(() => {
    if (!open) return;
    // Ask for the screens as the menu opens (a click), so Chrome's one-time "manage windows" question
    // comes now and not in the middle of opening the window.
    if (other) void screenDetails()?.call(window).catch(() => {});
    const away = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open, other]);

  return (
    <span ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title="Open this pair's chart alone - on your other screen when one is connected - on any timeframe, with the strategies pop-up: watch several timeframes at once, or keep the desk on one screen and the chart on the other"
        className="rounded border border-neutral-700 px-2 py-0.5 text-xs text-neutral-300 transition hover:border-sky-600 hover:text-white"
      >
        ⧉ New tab
      </button>
      {open && (
        <div className="absolute left-0 top-full z-40 mt-1 w-max rounded-md border border-neutral-700 bg-neutral-900 p-2 shadow-xl">
          <div className="mb-1.5 text-[11px] text-neutral-400">
            Open {displaySymbol(symbol)} alone in a new tab{scalp ? ", in scalp mode" : ""}, on:
          </div>
          <div className="flex gap-1">
            {(scalp ? SCALP_TFS : TIMEFRAMES).map((t) => (
              <a
                key={t}
                href={chartWindowUrl(symbol, assetClass, t, scalp)}
                target="_blank"
                rel="noopener"
                onClick={(e) => {
                  setOpen(false);
                  // A modified click keeps the browser's own link behaviour (a tab, a window).
                  if (!other || e.button !== 0 || e.ctrlKey || e.shiftKey || e.metaKey || e.altKey) return;
                  e.preventDefault();
                  const url = chartWindowUrl(symbol, assetClass, t, scalp);
                  void openOnOtherScreen(url).then((ok) => { if (!ok) window.open(url, "_blank"); });
                }}
                className={`rounded px-2 py-0.5 text-xs font-medium tabular-nums ${
                  t === timeframe ? "bg-neutral-700 text-white" : "text-neutral-300 hover:bg-neutral-700 hover:text-white"
                }`}
              >
                {t}
              </a>
            ))}
          </div>
          <div className="mt-1.5 max-w-[22rem] text-[11px] leading-snug text-neutral-500">
            {other
              ? "Opens on your other screen, filling it (the first time, Chrome asks to manage windows on all your displays - allow it). Ctrl+click for a tab here instead. "
              : "Drag the tab out of the browser to put it on another screen (Shift+click opens a new window). "}
            Each window keeps its own timeframe; the desk keeps yours.
          </div>
        </div>
      )}
    </span>
  );
}
