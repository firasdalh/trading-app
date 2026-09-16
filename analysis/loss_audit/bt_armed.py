"""Faithful (no look-ahead) simulation of the ARMED path as the live conditional watcher runs it.

variants:
  stop          break_retest OFF: buy_stop/sell_stop arm; fires at the close of the first bar that closes
                beyond the trigger; declined if overshoot > 0.25R (non-terminal: keeps waiting).
                R measured on the PLANNED risk (live sizes it from the trigger).
  retest_live   break_retest ON as the code behaves today: stage 1 = 2 closes beyond the level; stage 2's
                "touch" window includes the breakout bars, so it fires at the next close that holds
                beyond the level (right after the break). R:R-at-fill >= 1.5 else keep waiting.
  retest_fixed  the design intent: after the break is confirmed, a LATER bar must trade back into the
                zone, and a close must hold beyond the level. R:R-at-fill >= 1.5 else keep waiting.
Arms live 12 bars, cancelled on a failed break (close back through the level), or once past half-life with
a close beyond the stop. One arm/position per symbol at a time.
"""
import bisect
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.WARNING)

from bt import DUR, candles, data  # noqa: E402

from app.agents.orchestrator import _deterministic_decision  # noqa: E402
from app.agents.pipeline import _timeframes_for  # noqa: E402
from app.agents.technical import run_technical  # noqa: E402
from app.backtest.engine import _exit_price  # noqa: E402
from app.backtest.simulator import _WINDOW, _neutral_fundamental  # noqa: E402
from app.models.enums import AssetClass, Direction  # noqa: E402
from app.models.schemas import Candle, OHLCVSeries  # noqa: E402

ZONE_FRAC, LOOKBACK, VALID, MIN_RR, DRIFT = 0.35, 8, 12, 1.5, 0.25


def windows(series, tf, tfs, i, entry, ts_open, e_ts):
    t_i = entry[i].ts.timestamp()
    out = []
    for t in tfs:
        if t == tf:
            w = entry[max(0, i - _WINDOW + 1): i + 1]
        else:
            decision = t_i + DUR[tf]
            hi = bisect.bisect_right(ts_open[t], t_i)
            done = [c for c in series[t][max(0, hi - _WINDOW - 1): hi] if c.ts.timestamp() + DUR[t] <= decision]
            if hi:
                last = series[t][hi - 1]
                if last.ts.timestamp() + DUR[t] > decision:
                    lo = bisect.bisect_left(e_ts, last.ts.timestamp())
                    j_end = bisect.bisect_right(e_ts, t_i)
                    parts = series[tf][lo:j_end]
                    if parts:
                        done.append(Candle(ts=last.ts, open=parts[0].open, high=max(p.high for p in parts),
                                           low=min(p.low for p in parts), close=parts[-1].close,
                                           volume=sum(p.volume for p in parts)))
            w = done[-_WINDOW:]
        if not w:
            return None
        out.append(OHLCVSeries(symbol="", timeframe=t, candles=w))
    return out


def outcome(entry, j, direction, fill, stop, tp, risk_basis, max_hold=96):
    n = len(entry)
    end = min(j + max_hold, n - 1)
    exit_px, k_exit, res = entry[end].close, end, "timeout"
    for k in range(j + 1, end + 1):
        hit = _exit_price(direction, entry[k], stop, tp)
        if hit is not None:
            exit_px, res, k_exit = hit[0], hit[1], k
            break
    is_long = direction == Direction.LONG
    r = ((exit_px - fill) if is_long else (fill - exit_px)) / risk_basis
    return r, res, k_exit


def run_symbol(args):
    path, sym, ac, tf, bars, variant, trend_only, disable = args
    tfs = _timeframes_for(tf)
    series = {t: candles(path, sym, t) for t in tfs}
    entry = series[tf][-bars:]
    if len(entry) < _WINDOW + 5:
        return []
    ts_open = {t: [c.ts.timestamp() for c in series[t]] for t in tfs}
    e_ts = [c.ts.timestamp() for c in series[tf]]
    fund = _neutral_fundamental(sym)
    dis = set(disable)
    if variant == "stop":
        dis.add("break_retest")
    else:
        dis.discard("break_retest")
    dis = frozenset(dis)
    trades = []
    n = len(entry)
    i = _WINDOW - 1
    arm = None
    while i < n:
        bar = entry[i]
        if arm is not None:
            is_long = arm["dir"] == "long"
            direction = Direction.LONG if is_long else Direction.SHORT
            c = bar.close
            fire = False
            if i > arm["expire"]:
                arm = None
                i += 1
                continue
            half = (i - arm["born"]) >= VALID / 2
            if half and ((is_long and c <= arm["stop"]) or (not is_long and c >= arm["stop"])):
                arm = None
                i += 1
                continue
            if arm["level"] is None:  # plain stop arm
                beyond = c >= arm["trigger"] if is_long else c <= arm["trigger"]
                if beyond:
                    over = ((c - arm["trigger"]) if is_long else (arm["trigger"] - c)) / abs(arm["trigger"] - arm["stop"])
                    fire = over <= DRIFT
            else:
                lvl = arm["level"]
                if arm["broke_at"] is None:
                    if i - arm["born"] >= 1:
                        prev = entry[i - 1].close
                        if (is_long and prev > lvl and c > lvl) or (not is_long and prev < lvl and c < lvl):
                            arm["broke_at"] = i
                else:
                    if (is_long and c <= lvl) or (not is_long and c >= lvl):
                        arm = None  # failed break
                        i += 1
                        continue
                    zone = abs(lvl - arm["stop"]) * ZONE_FRAC
                    start = max(i - LOOKBACK + 1, 0) if variant == "retest_live" else arm["broke_at"] + 1
                    recent = entry[start: i + 1]
                    touched = any((b.low <= lvl + zone) if is_long else (b.high >= lvl - zone) for b in recent)
                    held = c > lvl if is_long else c < lvl
                    fire = touched and held
            if fire:
                fill = c
                risk_fill = abs(fill - arm["stop"])
                reward = abs(arm["tp"] - fill)
                right_side = (arm["tp"] > fill > arm["stop"]) if is_long else (arm["tp"] < fill < arm["stop"])
                if risk_fill > 0 and right_side:
                    rr = reward / risk_fill
                    basis = abs(arm["trigger"] - arm["stop"]) if arm["level"] is None else risk_fill
                    if arm["level"] is None or rr >= MIN_RR:
                        r, res, k = outcome(entry, i, direction, fill, arm["stop"], arm["tp"], basis)
                        trades.append({"symbol": sym, "variant": variant, "dir": arm["dir"],
                                       "entry_time": bar.ts.isoformat(), "fill": fill, "stop": arm["stop"],
                                       "tp": arm["tp"], "rr_fill": round(rr, 2), "r": round(r, 4),
                                       "outcome": res, "bars_held": k - i, "conf": arm["conf"],
                                       "wait": i - arm["born"], "risk": risk_fill})
                        arm = None
                        i = k + 1
                        continue
            i += 1
            continue
        w = windows(series, tf, tfs, i, entry, ts_open, e_ts)
        if w is None:
            i += 1
            continue
        technical = run_technical(sym, w, use_llm=False)
        prop = _deterministic_decision(sym, AssetClass(ac), tf, technical, fund, now=bar.ts,
                                       trend_only=trend_only, disable=dis)
        cnd = prop.conditional
        if (cnd is not None and cnd.trigger_price and cnd.stop_loss and cnd.take_profit
                and not prop.is_actionable):
            arm = {"dir": "long" if cnd.order_type.startswith("buy") else "short",
                   "trigger": cnd.trigger_price, "stop": cnd.stop_loss, "tp": cnd.take_profit,
                   "level": getattr(cnd, "break_level", None), "born": i, "expire": i + VALID,
                   "broke_at": None, "conf": cnd.confidence}
        i += 1
    return trades


def main():
    import argparse
    import multiprocessing as mp

    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--out")
    ap.add_argument("--symbols")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--variant", default="retest_fixed")
    ap.add_argument("--trend-only", action="store_true")
    ap.add_argument("--disable", default="")
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    ac = data(a.data)["asset_class"]
    jobs = [(a.data, s, ac[s], a.tf, a.bars, a.variant, a.trend_only,
             [d for d in a.disable.split(",") if d]) for s in a.symbols.split(",") if s]
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        res = pool.map(run_symbol, jobs, chunksize=1)
    tr = [t for r in res for t in r]
    json.dump(tr, open(a.out, "w"))
    print(f"{a.variant}: {len(tr)} fills in {time.time() - t0:.0f}s -> {a.out}")


if __name__ == "__main__":
    main()
