"""Pass 1 of the confidence-factor test: replay every 1h bar through the live engine (trend-only ON, AI
off) with and without its MACD gates, recording at each decision the MACD state, the supply/demand zones
and last week's high/low. No model calls.

Variants, all on the live filter set:
  B    live: the 1h MACD-histogram gate ("momentum") and the flat-momentum guard both on
  NG   without the MACD-histogram gate
  NGF  without the gate and without the flat-momentum guard

Every state is signed to the trade, so evaluate.py can score the gates, the existing MACD confidence
points and the candidate factors (MACD cross, supply/demand, last week's high/low) without re-running
the engine.

Usage (from backend/):  python ../analysis/macd/replay.py <bars.pkl> <out_dir> [--procs 6]
"""
import json
import os
import pickle
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())
os.environ.setdefault("LOG_LEVEL", "WARNING")
import logging  # noqa: E402

logging.disable(logging.WARNING)

from app.agents.indicators import _ema_full  # noqa: E402
from app.agents.order_blocks import find_order_blocks  # noqa: E402
from app.agents.orchestrator import _deterministic_decision  # noqa: E402
from app.agents.pipeline import _UNPROVEN_FILTERS, _timeframes_for  # noqa: E402
from app.agents.technical import run_technical  # noqa: E402
from app.backtest.simulator import _WINDOW, _context_window, _neutral_fundamental, _simulate_trade  # noqa: E402
from app.models.enums import AssetClass  # noqa: E402
from app.models.schemas import Candle, OHLCVSeries  # noqa: E402

TF = "1h"
LIVE = frozenset(_UNPROVEN_FILTERS)      # the live set: settings.disabled_filters is empty
VARIANTS = {"B": LIVE, "NG": LIVE | {"momentum"}, "NGF": LIVE | {"momentum", "flat_momentum"}}
MAX_HOLD = 96
TOUCH_BARS = 12        # how far back a touch of a zone / last week's level is looked for
LEVEL_TOL_ATR = 0.2    # within this of last week's high/low counts as touching it


def candles(rows):
    return [Candle(ts=datetime.fromtimestamp(r[0], tz=timezone.utc), open=r[1], high=r[2], low=r[3],
                   close=r[4], volume=r[5]) for r in rows]


def hist_series(closes, fast=12, slow=26, signal=9):
    """The MACD histogram (line - signal) per bar, computed as indicators.macd does; warm-up dropped."""
    ef, es = _ema_full(closes, fast), _ema_full(closes, slow)
    line = [a - b for a, b in zip(ef, es) if a is not None and b is not None]
    sig = _ema_full(line, signal)
    return [m - s for m, s in zip(line, sig) if s is not None]


def macd_state(window, technical, long):
    """MACD facts at the decision bar, signed so +1 means 'with the trade', -1 'against it'."""
    side = 1 if long else -1

    def sgn(x):
        return 0 if not x else (1 if x > 0 else -1) * side

    h = hist_series([c.close for c in window])
    since = None                      # bars since the histogram last changed sign (0 = crossed this bar)
    for k in range(len(h) - 1, 0, -1):
        if (h[k] > 0) != (h[k - 1] > 0):
            since = len(h) - 1 - k
            break
    ind = {t.timeframe: t.indicators for t in technical.timeframes}
    i1 = ind.get(TF, {})
    hist, prev, atr = i1.get("macd_hist"), i1.get("macd_hist_prev"), i1.get("atr14")
    return dict(
        h=sgn(hist), since=since,
        h_atr=round(hist / atr * side, 4) if hist is not None and atr else None,
        rising=(None if hist is None or prev is None else (hist > prev) if long else (hist < prev)),
        line=sgn(i1.get("macd")),
        h4=sgn(ind.get("4h", {}).get("macd_hist")), d1=sgn(ind.get("1d", {}).get("macd_hist")),
    )


def zone_state(window, long, atr):
    """Supply/demand zones (the app's 1h order blocks) on the trade's side: demand under a long, supply
    over a short.

    zone_since: bars since price last touched such a zone while the decision bar closes back out of it
                on the trade's side (a bounce from demand / a rejection from supply); None if not within
                TOUCH_BARS. zone_str is that zone's strength (0-100).
    zone_near:  ATR from the close to the nearest such zone (0 = inside it); None if there is none.
    room:       ATR from the close to the nearest opposing zone ahead (supply over a long); None if none.
    """
    if not atr:
        return dict(zone_since=None, zone_str=None, zone_near=None, room=None)
    blocks = find_order_blocks(window, max_blocks=1000)
    mine = [b for b in blocks if b.kind == ("bullish" if long else "bearish")]
    other = [b for b in blocks if b.kind == ("bearish" if long else "bullish")]
    n, last = len(window), window[-1].close
    since = strength = None
    for k in range(n - 1, max(n - 1 - TOUCH_BARS, 0) - 1, -1):           # newest first
        c = window[k]
        hit = [z for z in mine if z.confirmed_index <= k and c.low <= z.top and c.high >= z.bottom
               and (last > z.top if long else last < z.bottom)]
        if hit:
            since, strength = n - 1 - k, max(z.strength for z in hit)
            break
    near = [(0.0 if z.bottom <= last <= z.top else (last - z.top if long else z.bottom - last) / atr)
            for z in mine if (z.bottom <= last if long else z.top >= last)]
    ahead = [((z.bottom - last) if long else (last - z.top)) / atr
             for z in other if (z.bottom > last if long else z.top < last)]
    return dict(zone_since=since, zone_str=strength, zone_near=round(min(near), 3) if near else None,
                room=round(min(ahead), 3) if ahead else None)


def week_state(window, d1, long, atr):
    """Last week's high/low (from the daily context), this week's 1h bars only.

    pw_break:  bars since the close crossed last week's high (long) / low (short) in the trade's
               direction, with the decision bar still beyond it; None if it hasn't.
    pw_bounce: bars since price came at last week's high or low from the trade's side (long: from above),
               touched it (within LEVEL_TOL_ATR) and the decision bar still closes on that side; None if
               not within TOUCH_BARS. The bar that breaks a level comes from the other side, so a break is
               not counted as a bounce.
    """
    pwh, pwl = d1.get("prior_week_high"), d1.get("prior_week_low")
    if not (pwh and pwl and atr):
        return dict(pw_break=None, pw_bounce=None)
    n, last = len(window), window[-1].close
    wk = window[-1].ts.isocalendar()[:2]
    first = next(k for k in range(n) if window[k].ts.isocalendar()[:2] == wk)
    level = pwh if long else pwl
    beyond = (lambda x: x > level) if long else (lambda x: x < level)
    brk = None
    if beyond(last):
        for k in range(n - 1, first - 1, -1):
            if k == first or not beyond(window[k - 1].close):
                brk = n - 1 - k
                break
    tol = LEVEL_TOL_ATR * atr
    bounce = None
    for k in range(n - 1, max(n - 1 - TOUCH_BARS, first, 1) - 1, -1):
        c, prev = window[k], window[k - 1].close
        if any((c.low <= lv + tol and prev > lv and last > lv) if long
               else (c.high >= lv - tol and prev < lv and last < lv) for lv in (pwh, pwl)):
            bounce = n - 1 - k
            break
    return dict(pw_break=brk, pw_bounce=bounce)


def run_symbol(job):
    path, sym = job
    data = pickle.load(open(path, "rb"))
    ac = AssetClass(data["asset_class"][sym])
    tfs = _timeframes_for(TF)
    # Drop the bar still forming at fetch time.
    series = {t: candles(data["bars"][(sym, t)][:-1]) for t in tfs}
    spreads = {r[0]: r[6] for r in data["bars"][(sym, TF)][:-1]}
    spec = data["spec"][sym]
    entry = series[TF]
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(sym)

    rows, trades, tid_of = [], {}, {}
    t0 = time.time()
    limit = int(os.environ.get("MACD_LIMIT", "0"))     # quick check: only the last N bars
    for i in range(_WINDOW - 1 if not limit else len(entry) - limit, len(entry)):
        window = []
        for t in tfs:
            w = (entry[max(0, i - _WINDOW + 1): i + 1] if t == TF
                 else _context_window(series[t], ts_index[t], entry, ts_index[TF], i, t, TF))
            if not w:
                break
            window.append(OHLCVSeries(symbol=sym, timeframe=t, candles=w))
        else:
            technical = run_technical(sym, window, use_llm=False)
            tf0 = next((x for x in technical.timeframes if x.timeframe == TF), None)
            atr = tf0.indicators.get("atr14") if tf0 else None
            out = {}
            for name, disable in VARIANTS.items():
                prop = _deterministic_decision(
                    sym, ac, TF, technical, fund, now=entry[i].ts, trend_only=True, disable=disable,
                    entry_candles=entry[max(0, i - _WINDOW + 1): i + 1])
                if not (prop.is_actionable and prop.take_profit is not None):
                    continue
                sig = (prop.direction.value, prop.entry, prop.stop_loss, prop.take_profit, prop.confidence,
                       prop.strategy)
                tid = tid_of.get((i, sig))
                if tid is None:
                    tr = _simulate_trade(sym, entry, i, prop, max_hold=MAX_HOLD, cost_r=0.0, atr=atr)
                    if tr is None:
                        continue
                    tid = f"{sym}:{i}:{len(tid_of)}"
                    tid_of[(i, sig)] = tid
                    sp = max(spec.get("spread") or 0, spreads.get(int(entry[i].ts.timestamp()), 0))
                    cost = max(sp * (spec.get("point") or 0) / abs(tr.entry - tr.stop), 0.01)
                    long = prop.direction.value == "long"
                    d1 = next((x.indicators for x in technical.timeframes if x.timeframe == "1d"), {})
                    trades[tid] = dict(
                        symbol=sym, i=i, direction=tr.direction, strategy=prop.strategy, conf=prop.confidence,
                        entry_time=tr.entry_time.isoformat(), exit_time=tr.exit_time.isoformat(),
                        outcome=tr.outcome, r=tr.r, cost=round(cost, 4), net=round(tr.r - cost, 4),
                        bars_held=tr.bars_held, planned_rr=tr.planned_rr,
                        **macd_state(window[0].candles, technical, long),
                        **zone_state(window[0].candles, long, atr),
                        **week_state(window[0].candles, d1, long, atr))
                out[name] = tid
            if out:
                rows.append(dict(i=i, ts=entry[i].ts.isoformat(), out=out))
    print(f"{sym}: {len(entry)} bars, {len(rows)} rows, {len(trades)} trades, {time.time() - t0:.0f}s",
          flush=True)
    return dict(symbol=sym, asset_class=ac.value, n_bars=len(entry), rows=rows, trades=trades)


def main():
    import argparse
    import multiprocessing as mp

    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("out_dir")
    ap.add_argument("--procs", type=int, default=6)
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    syms = [s for s in a.symbols.split(",") if s] or list(pickle.load(open(a.data, "rb"))["asset_class"])
    os.makedirs(a.out_dir, exist_ok=True)
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        for res in pool.imap_unordered(run_symbol, [(a.data, s) for s in syms]):
            json.dump(res, open(os.path.join(a.out_dir, f"replay_{res['symbol']}.json"), "w"))
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
