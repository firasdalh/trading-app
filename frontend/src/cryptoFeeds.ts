// The crypto futures crowd, read straight from the browser (2026-11-21, asked for manual BTC trading: "add the
// best thing from those sites to our chart"). Exness has no futures book, so this is what its crypto CFDs follow:
// the big perpetual-futures markets, on their free public feeds - no key, nothing about the account is sent.
//
//   Liquidations - OKX BTC-USDT perpetual (its REST list is complete and goes back hours; Binance's public feed
//                  sends only the largest one per second and has no history, Bybit's has no history).
//   Open interest and funding - Binance BTCUSDT perpetual, the biggest of them.
//
// Display only: none of it is tested on our trades, and nothing here feeds the strategy.

export type Perp = { base: string; binance: string; okx: string };
/** One forced close. ``long`` = a LONG was liquidated (a forced SELL, it pushes the price down). */
export type Liq = { ts: number; px: number; usd: number; long: boolean };
/** Open interest in coins (not dollars, so a price move alone does not move it). */
export type OiPoint = { ts: number; coins: number };
export type Funding = { rate: number; next: number; hours: number; mark: number };
export type Side = { long: number; short: number; n: number };
/** Of the volume traded in the last finished 1m candles, the share that was market BUYING (taker buys). */
export type Flow = { buy: number; bars: number; to: number };

const OKX = "https://www.okx.com/api/v5/public";
const BINANCE = "https://fapi.binance.com";
const TIMEOUT_MS = 10_000;

/** Exness "BTCUSDm" -> the BTC perpetuals; null for a symbol that is not COIN + USD. */
export function perpOf(symbol: string): Perp | null {
  const m = /^([A-Z0-9]{2,10})USD$/.exec(symbol.trim().replace(/m$/, "").toUpperCase());
  if (!m) return null;
  const base = m[1];
  return { base, binance: `${base}USDT`, okx: `${base}-USDT-SWAP` };
}

async function getJson(url: string, signal?: AbortSignal): Promise<unknown> {
  const ctl = new AbortController();
  const stop = () => ctl.abort();
  signal?.addEventListener("abort", stop);
  const t = setTimeout(stop, TIMEOUT_MS);
  try {
    const r = await fetch(url, { signal: ctl.signal, credentials: "omit" });
    if (!r.ok) throw new Error(`${r.status} from ${new URL(url).host}`);
    return await r.json();
  } finally {
    clearTimeout(t);
    signal?.removeEventListener("abort", stop);
  }
}

export const liqKey = (l: Liq) => `${l.ts}|${l.px}|${l.usd.toFixed(2)}|${l.long ? "L" : "S"}`;

type OkxDetail = { ts?: string | number; time?: string | number; bkPx?: string; sz?: string; side?: string };

/** OKX's liquidation list (REST or socket shape) for one contract. ``sz`` is in contracts of ``ctVal`` coins. */
export function parseOkx(json: unknown, inst: string, ctVal: number): Liq[] {
  const out: Liq[] = [];
  const blocks = ((json as { data?: unknown })?.data ?? []) as { instId?: string; details?: OkxDetail[] }[];
  if (!Array.isArray(blocks)) return out;
  for (const b of blocks) {
    if (b.instId && b.instId !== inst) continue;
    for (const d of b.details ?? []) {
      const ts = Number(d.ts ?? d.time), px = Number(d.bkPx), sz = Number(d.sz);
      if (!(ts > 0 && px > 0 && sz > 0)) continue;
      // In both of OKX's position modes the closing order of a long is a sell.
      out.push({ ts, px, usd: sz * ctVal * px, long: d.side === "sell" });
    }
  }
  return out;
}

const ctVals = new Map<string, number>();
async function okxCtVal(p: Perp, signal?: AbortSignal): Promise<number> {
  const hit = ctVals.get(p.okx);
  if (hit) return hit;
  const j = (await getJson(`${OKX}/instruments?instType=SWAP&instId=${p.okx}`, signal)) as { data?: { ctVal?: string }[] };
  const v = Number(j.data?.[0]?.ctVal);
  if (!(v > 0)) throw new Error(`OKX has no ${p.okx}`);
  ctVals.set(p.okx, v);
  return v;
}

const okxPage = (p: Perp, after?: number) =>
  `${OKX}/liquidation-orders?instType=SWAP&uly=${p.base}-USDT&state=filled&limit=100${after ? `&after=${after}` : ""}`;

/** Every liquidation back to ``sinceMs`` (or ``maxPages`` pages of 100, whichever comes first), oldest first. */
export async function okxHistory(p: Perp, sinceMs: number, maxPages: number, signal?: AbortSignal): Promise<Liq[]> {
  const ct = await okxCtVal(p, signal);
  const all: Liq[] = [];
  let after: number | undefined;
  for (let i = 0; i < maxPages; i++) {
    const page = parseOkx(await getJson(okxPage(p, after), signal), p.okx, ct);
    if (!page.length) break;
    all.push(...page);
    const oldest = Math.min(...page.map((l) => l.ts));
    if (oldest <= sinceMs || (after != null && oldest >= after)) break;
    after = oldest;
  }
  return all.filter((l) => l.ts >= sinceMs).sort((a, b) => a.ts - b.ts);
}

/** The newest ones, paging back (up to 5 pages) until it meets ``seenMs`` - a cascade can pass 100 in seconds. */
export async function okxLatest(p: Perp, seenMs: number, signal?: AbortSignal): Promise<Liq[]> {
  const ct = await okxCtVal(p, signal);
  const all: Liq[] = [];
  let after: number | undefined;
  for (let i = 0; i < 5; i++) {
    const page = parseOkx(await getJson(okxPage(p, after), signal), p.okx, ct);
    if (!page.length) break;
    all.push(...page);
    const oldest = Math.min(...page.map((l) => l.ts));
    if (oldest <= seenMs || page.length < 100) break;
    after = oldest;
  }
  return all;
}

/** Binance keeps open-interest history at 5m and up, for the last 30 days. */
export function oiPeriod(tfSec: number): string {
  if (tfSec >= 86_400) return "1d";
  if (tfSec >= 14_400) return "4h";
  if (tfSec >= 3_600) return "1h";
  if (tfSec >= 1_800) return "30m";
  if (tfSec >= 900) return "15m";
  return "5m";
}

export async function oiHistory(p: Perp, period: string, signal?: AbortSignal): Promise<OiPoint[]> {
  const j = (await getJson(`${BINANCE}/futures/data/openInterestHist?symbol=${p.binance}&period=${period}&limit=500`, signal)) as
    { timestamp?: number; sumOpenInterest?: string }[];
  if (!Array.isArray(j)) throw new Error(`Binance has no ${p.binance}`);
  return j.map((x) => ({ ts: Number(x.timestamp), coins: Number(x.sumOpenInterest) }))
    .filter((x) => x.ts > 0 && x.coins > 0).sort((a, b) => a.ts - b.ts);
}

export async function oiNow(p: Perp, signal?: AbortSignal): Promise<OiPoint> {
  const j = (await getJson(`${BINANCE}/fapi/v1/openInterest?symbol=${p.binance}`, signal)) as { time?: number; openInterest?: string };
  return { ts: Number(j.time) || Date.now(), coins: Number(j.openInterest) };
}

let fundingHours: Map<string, number> | null = null;
export async function funding(p: Perp, signal?: AbortSignal): Promise<Funding> {
  if (!fundingHours) {
    try {
      const info = (await getJson(`${BINANCE}/fapi/v1/fundingInfo`, signal)) as { symbol: string; fundingIntervalHours?: number }[];
      fundingHours = new Map(info.map((x) => [x.symbol, Number(x.fundingIntervalHours) || 8]));
    } catch {
      fundingHours = new Map();   // listed only when changed from the usual 8 h
    }
  }
  const j = (await getJson(`${BINANCE}/fapi/v1/premiumIndex?symbol=${p.binance}`, signal)) as
    { lastFundingRate?: string; nextFundingTime?: number; markPrice?: string };
  return { rate: Number(j.lastFundingRate), next: Number(j.nextFundingTime), hours: fundingHours.get(p.binance) ?? 8, mark: Number(j.markPrice) };
}

export const FLOW_BARS = 5;       // the learner's too: learning.FLOW_BARS (15 min made no difference in the study)
export const FLOW_CUT = 0.025;    // within 50% +-2.5 points = balanced (the learner's line too: learning.FLOW_CUT)

/** The aggressive flow on Binance's perpetual: the taker-buy share of the last ``bars`` FINISHED 1m candles
 * (2026-11-21: the learner watches the same reading on the strategy's crypto trades). */
export async function takerFlow(p: Perp, bars = FLOW_BARS, signal?: AbortSignal): Promise<Flow | null> {
  const rows = (await getJson(`${BINANCE}/fapi/v1/klines?symbol=${p.binance}&interval=1m&limit=${bars + 1}`, signal)) as
    (string | number)[][];
  return flowOf(rows, Date.now(), bars);
}

export function flowOf(rows: (string | number)[][], nowMs: number, bars = FLOW_BARS): Flow | null {
  if (!Array.isArray(rows)) return null;
  const done = rows.filter((r) => Number(r[0]) + 60_000 <= nowMs).slice(-bars);
  if (done.length < bars) return null;
  let vol = 0, buy = 0;
  for (const r of done) {
    vol += Number(r[5]);
    buy += Number(r[9]);
  }
  return vol > 0 ? { buy: buy / vol, bars, to: Number(done[done.length - 1][0]) + 60_000 } : null;
}

/** Liquidations summed per candle (keyed by the candle's open time, UTC seconds). */
export function liqByBar(liqs: Liq[], tfSec: number): Map<number, Side> {
  const out = new Map<number, Side>();
  for (const l of liqs) {
    const k = Math.floor(l.ts / 1000 / tfSec) * tfSec;
    const s = out.get(k) ?? { long: 0, short: 0, n: 0 };
    if (l.long) s.long += l.usd;
    else s.short += l.usd;
    s.n++;
    out.set(k, s);
  }
  return out;
}

/** The last reading at or before ``ms`` (points oldest first). */
export function oiAt(pts: OiPoint[], ms: number): OiPoint | null {
  let lo = 0, hi = pts.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].ts <= ms) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return best >= 0 ? pts[best] : null;
}

export function liqSum(liqs: Liq[], fromMs: number, toMs: number): Side {
  const s: Side = { long: 0, short: 0, n: 0 };
  for (const l of liqs) {
    if (l.ts < fromMs || l.ts > toMs) continue;
    if (l.long) s.long += l.usd;
    else s.short += l.usd;
    s.n++;
  }
  return s;
}

export type CrowdRead = { text: string; tip: string; tone: "bull" | "bear" | "flat" };

/** What the open interest and the price did together over the window - who is coming in or getting out. */
export function crowdRead(oiPct: number | null, pricePct: number | null, winMin: number): CrowdRead | null {
  if (oiPct == null || pricePct == null) return null;
  const oiFlat = winMin <= 15 ? 0.2 : 0.4;          // % - smaller than this is the usual drift
  const pxFlat = winMin <= 15 ? 0.05 : 0.1;
  const oiUp = oiPct >= oiFlat, oiDn = oiPct <= -oiFlat;
  const pxUp = pricePct >= pxFlat, pxDn = pricePct <= -pxFlat;
  if (!oiUp && !oiDn) {
    return { text: "no new money", tone: "flat", tip: "Open interest about flat: traders are neither piling in nor getting out." };
  }
  if (oiUp && pxUp) return { text: "new longs coming in", tone: "bull", tip: "Open interest up with the price: new buyers are opening positions." };
  if (oiUp && pxDn) return { text: "new shorts coming in", tone: "bear", tip: "Open interest up while the price falls: new sellers are opening positions." };
  if (oiDn && pxUp) return { text: "shorts getting out (squeeze)", tone: "bull", tip: "Open interest down while the price rises: shorts are closing or being liquidated - buying they were forced into, which tends to fade once it is done." };
  if (oiDn && pxDn) return { text: "longs getting out (flush)", tone: "bear", tip: "Open interest down with the price: longs are closing or being liquidated - selling they were forced into, which tends to fade once it is done." };
  return oiUp
    ? { text: "positions building, price flat", tone: "flat", tip: "Open interest up but the price has not moved: both sides are adding. When it breaks, the losing side's stops and liquidations can make the move run." }
    : { text: "positions closing, price flat", tone: "flat", tip: "Open interest down with the price flat: traders are taking money off the table." };
}

export type FundingRead = { text: string; tone: "warn" | "flat" };

/** Funding per interval: about +0.01% is the resting rate; well above it the longs are crowded and paying. */
export function fundingRead(rate: number): FundingRead {
  const pct = rate * 100;
  if (pct >= 0.03) return { text: "longs crowded", tone: "warn" };
  if (pct <= -0.01) return { text: "shorts crowded", tone: "warn" };
  return { text: "normal", tone: "flat" };
}

export function fmtUsdShort(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k`;
  return `$${Math.round(v)}`;
}

export function fmtCoins(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k`;
  return v.toFixed(a >= 100 ? 0 : 2);
}

export function fmtIn(ms: number): string {
  const m = Math.max(0, Math.round(ms / 60_000));
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}` : `${m} min`;
}
