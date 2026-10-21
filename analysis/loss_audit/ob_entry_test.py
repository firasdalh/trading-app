"""Order blocks as an ENTRY method - the test registered in ob_entry_preregistration.md.

Offline: reads the saved candle pickles, never touches MT5 or the running app.
Usage:  python ob_entry_test.py <scratchpad dir>      (needs audit/bars.pkl and holdout/bars.pkl)
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import pickle
import random
import sys

sys.path.insert(0, __file__.rsplit("analysis", 1)[0] + "backend")

from app.agents.indicators import _swing_pivots  # noqa: E402
from app.agents.order_blocks import (  # noqa: E402
    _MAX_AGE_BARS, _MAX_IMPULSE_BARS, _MIN_DISPLACEMENT_ATR, _ORIGIN_LOOKBACK, _PIVOT_L, _PIVOT_R,
    OrderBlock, _atr_series, _has_fvg, _score, _vol_at, _zone,
)
from app.models.schemas import Candle  # noqa: E402

WARMUP = 500
MIN_STRENGTH = 70
TARGET_MIN_STRENGTH = 55
STOP_ATR = 0.1
MIN_RR, MAX_RR, DEFAULT_RR = 1.5, 4.0, 3.0
TIME_STOP = 72


def candles_of(rows):
    return [Candle(ts=dt.datetime.fromtimestamp(r[0], dt.timezone.utc), open=r[1], high=r[2], low=r[3],
                   close=r[4], volume=r[5]) for r in rows]


def zones(cs):
    """Every zone the detector's creation rule produces, with its creation strength."""
    n = len(cs)
    atrs = _atr_series(cs)
    highs, lows = _swing_pivots(cs[:-1], _PIVOT_L, _PIVOT_R, 0)
    out = []
    for kind, pivots in (("bullish", highs), ("bearish", lows)):
        for p in pivots:
            level = cs[p].high if kind == "bullish" else cs[p].low
            b = next((i for i in range(p + _PIVOT_R + 1, n - 1)
                      if (cs[i].close > level if kind == "bullish" else cs[i].close < level)), None)
            if b is None:
                continue
            o = next((i for i in range(b, max(b - _ORIGIN_LOOKBACK, 0) - 1, -1)
                      if (cs[i].close < cs[i].open if kind == "bullish" else cs[i].close > cs[i].open)), None)
            if o is None or b - o > _MAX_IMPULSE_BARS:
                continue
            a0 = atrs[o]
            if a0 <= 0:
                continue
            disp = ((cs[b].close - cs[o].low) if kind == "bullish" else (cs[o].high - cs[b].close)) / a0
            if disp < _MIN_DISPLACEMENT_ATR:
                continue
            top, bottom = _zone(cs[o], a0)
            ob = OrderBlock(kind=kind, top=top, bottom=bottom, origin_index=o, origin_ts=cs[o].ts,
                            confirmed_index=b, confirmed_ts=cs[b].ts, broke_level=level,
                            displacement_atr=disp, fvg=_has_fvg(cs, o, b, bullish=kind == "bullish"),
                            age_bars=b - o)
            _score(ob, 0.0, _vol_at(cs, b))
            out.append(ob)
    # de-duplicate: two pivots can resolve to the same origin and break
    seen, uniq = set(), []
    for z in out:
        k = (z.kind, z.origin_index, z.confirmed_index)
        if k not in seen:
            seen.add(k)
            uniq.append(z)
    return uniq


def first_touch(cs, z):
    """Index of the first bar after confirmation whose BID range overlaps the zone (None = never)."""
    for j in range(z.confirmed_index + 1, min(len(cs), z.origin_index + _MAX_AGE_BARS + 1)):
        if cs[j].low <= z.top and cs[j].high >= z.bottom:
            return j
    return None


def ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def trend4h_fn(rows4):
    t4 = [r[0] for r in rows4]
    c4 = [r[4] for r in rows4]
    e20, e50 = ema(c4, 20), ema(c4, 50)

    def trend_at(close_time):
        # last COMPLETED 4h bar: start + 4h <= close_time
        i = bisect.bisect_right(t4, close_time - 4 * 3600) - 1
        if i < 50:
            return None
        return "up" if e20[i] > e50[i] else "down" if e20[i] < e50[i] else None
    return trend_at


def simulate(cs, spr, e, entry, stop, target, long_, entry_bar_intrabar):
    """Walk from entry bar e. Returns (exit_price, exit_index, reason)."""
    n = len(cs)
    last = min(n - 1, e + TIME_STOP)
    for k in range(e, last + 1):
        c, s = cs[k], spr[k]
        if long_:
            if c.low <= stop:
                return (min(stop, c.open) if k > e or not entry_bar_intrabar else stop), k, "stop"
            if k > e and c.high >= target:
                return max(target, c.open), k, "target"
        else:
            if c.high + s >= stop:
                return (max(stop, c.open + s) if k > e or not entry_bar_intrabar else stop), k, "stop"
            if k > e and c.low + s <= target:
                return min(target, c.open + s), k, "target"
    c = cs[last]
    return (c.close if long_ else c.close + spr[last]), last, "time"


def run_symbol(sym, data, hyp):
    rows = data["bars"][(sym, "1h")]
    point = data["spec"][sym]["point"]
    cs = candles_of(rows)
    spr = [r[6] * point if len(r) > 6 else data["spec"][sym]["spread"] * point for r in rows]
    atrs = _atr_series(cs)
    trend_at = trend4h_fn(data["bars"].get((sym, "4h"), []))
    zs = zones(cs)
    touch = {id(z): first_touch(cs, z) for z in zs}
    cands = []
    for z in zs:
        if z.strength < MIN_STRENGTH:
            continue
        long_ = z.kind == "bullish"
        j = touch[id(z)]
        if j is None:
            continue
        if hyp == "H1":
            # the LIMIT fills on the first bar the ASK reaches the edge (on or after the bid touch)
            fill = None
            for k in range(j, min(len(cs), z.origin_index + _MAX_AGE_BARS + 1)):
                if (cs[k].close < z.bottom) if long_ else (cs[k].close > z.top):
                    if not ((cs[k].low <= z.top - spr[k]) if long_ else (cs[k].high >= z.bottom)):
                        break                      # closed through it before filling: cancelled
                if (cs[k].low <= z.top - spr[k]) if long_ else (cs[k].high >= z.bottom):
                    fill = k
                    break
            if fill is None or fill < WARMUP:
                continue
            e = fill
            entry = z.top if long_ else z.bottom
            a = atrs[e - 1]
            intrabar = True
        else:  # H2: first touch bar closes back outside, 4h trend agrees, enter next open
            if j + 1 >= len(cs) or j + 1 < WARMUP:
                continue
            ok_close = cs[j].close > z.top if long_ else cs[j].close < z.bottom
            close_time = rows[j][0] + 3600
            ok_trend = trend_at(close_time) == ("up" if long_ else "down")
            if not (ok_close and ok_trend):
                continue
            e = j + 1
            entry = cs[e].open + spr[e] if long_ else cs[e].open
            a = atrs[j]
            intrabar = False
        stop = z.bottom - STOP_ATR * a if long_ else z.top + STOP_ATR * a
        risk = (entry - stop) if long_ else (stop - entry)
        if risk <= 0:
            continue
        # nearest opposing zone that exists and is untouched at entry, strength >= 55, beyond entry
        opp = [y for y in zs if y.kind != z.kind and y.strength >= TARGET_MIN_STRENGTH
               and y.confirmed_index < e and (touch[id(y)] is None or touch[id(y)] >= e)
               and (y.bottom > entry if long_ else y.top < entry)]
        if opp:
            near = min(opp, key=lambda y: abs((y.bottom if long_ else y.top) - entry))
            edge = near.bottom if long_ else near.top
            rr = abs(edge - entry) / risk
            if rr < MIN_RR:
                continue
            rr = min(rr, MAX_RR)
        else:
            rr = DEFAULT_RR
        target = entry + rr * risk if long_ else entry - rr * risk
        cands.append((e, long_, entry, stop, target, risk, rr, z))
    cands.sort(key=lambda t: t[0])
    trades, busy_until = [], -1
    for e, long_, entry, stop, target, risk, rr, z in cands:
        if e <= busy_until:
            continue
        px, k, why = simulate(cs, spr, e, entry, stop, target, long_, hyp == "H1")
        r = ((px - entry) if long_ else (entry - px)) / risk
        busy_until = k
        trades.append({"sym": sym, "ac": data["asset_class"].get(sym, "forex"), "t": rows[e][0],
                       "dir": "long" if long_ else "short", "r": round(r, 4), "rr": round(rr, 2),
                       "exit": why, "strength": z.strength, "bars": k - e})
    return trades


def boot_p(rs, n=10000, seed=7):
    """One-sided bootstrap p that the mean is <= 0."""
    rnd = random.Random(seed)
    m = len(rs)
    cnt = 0
    for _ in range(n):
        if sum(rs[rnd.randrange(m)] for _ in range(m)) / m <= 0:
            cnt += 1
    return cnt / n


def boot_p_days(trades, n=10000, seed=11):
    """Same, resampling whole entry DAYS (trades on one day across symbols move together)."""
    by = {}
    for t in trades:
        by.setdefault(t["t"] // 86400, []).append(t["r"])
    days = list(by.values())
    rnd = random.Random(seed)
    cnt = 0
    for _ in range(n):
        pick = [days[rnd.randrange(len(days))] for _ in range(len(days))]
        tot = sum(sum(d) for d in pick)
        cnt_t = sum(len(d) for d in pick)
        if tot / cnt_t <= 0:
            cnt += 1
    return cnt / n


def summary(trades):
    rs = [t["r"] for t in trades]
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    loss = [r for r in rs if r <= 0]
    return {"n": len(rs), "mean": round(sum(rs) / len(rs), 4), "total": round(sum(rs), 2),
            "win": round(len(wins) / len(rs), 3),
            "pf": round(sum(wins) / -sum(loss), 3) if loss and sum(loss) < 0 else None}


def folds(trades, k=4):
    if not trades:
        return []
    t0, t1 = min(t["t"] for t in trades), max(t["t"] for t in trades) + 1
    w = (t1 - t0) / k
    return [summary([t for t in trades if t0 + i * w <= t["t"] < t0 + (i + 1) * w]) for i in range(k)]


def main():
    sp = sys.argv[1]
    out = {}
    for label, path in (("DEV", "audit/bars.pkl"), ("HOLDOUT", "holdout/bars.pkl")):
        data = pickle.load(open(f"{sp}/{path}", "rb"))
        syms = sorted({k[0] for k in data["bars"] if k[1] == "1h"})
        for hyp in ("H1", "H2"):
            trades = []
            for s in syms:
                trades += run_symbol(s, data, hyp)
            rs = [t["r"] for t in trades]
            res = {"all": summary(trades), "folds": folds(trades),
                   "by_class": {ac: summary([t for t in trades if t["ac"] == ac])
                                for ac in sorted({t["ac"] for t in trades})},
                   "by_dir": {d: summary([t for t in trades if t["dir"] == d]) for d in ("long", "short")},
                   "exits": {x: sum(1 for t in trades if t["exit"] == x) for x in ("stop", "target", "time")},
                   "p_trades": boot_p(rs) if rs else None, "p_days": boot_p_days(trades) if rs else None}
            if label == "DEV":
                res["ex_metals"] = summary([t for t in trades if t["ac"] != "metal"])
            out[f"{label}_{hyp}"] = res
            print(label, hyp, json.dumps(res["all"]), "p_trades", res["p_trades"], "p_days", res["p_days"], flush=True)
            json.dump(trades, open(f"{sp}/ob_entry_{label}_{hyp}.json", "w"))
    json.dump(out, open(f"{sp}/ob_entry_results.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
