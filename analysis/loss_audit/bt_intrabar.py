"""Does deciding on a HALF-FORMED entry bar (what the live app does when it analyses mid-candle) perform
differently from deciding at the bar CLOSE (what the backtest measures)?

For a 1h entry timeframe, every 15m close is a decision point. At offset 15/30/45 min the last 1h bar is
PARTIAL (rebuilt from the 15m bars so far); at offset 60 it is complete. 4h/1d context is rebuilt the same
way (completed bars + the forming one up to the decision moment). Entry at that 15m close; exits walk
forward on 15m bars against the proposal's stop/target (stop first on an ambiguous bar).
One position per symbol per offset-stream; each offset is simulated independently.
"""
import bisect
import json
import logging
import os
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.WARNING)

from bt import candles, data  # noqa: E402

from app.agents.orchestrator import _deterministic_decision  # noqa: E402
from app.agents.technical import run_technical  # noqa: E402
from app.backtest.engine import _exit_price  # noqa: E402
from app.backtest.simulator import _neutral_fundamental  # noqa: E402
from app.models.enums import AssetClass, Direction  # noqa: E402
from app.models.schemas import Candle, OHLCVSeries  # noqa: E402

SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
W = 200


def ctx(series, ts, base, base_ts, k, tf):
    """tf candles as known at the close of base (15m) bar k."""
    decision = base[k].ts + timedelta(seconds=SEC["15m"])
    hi = bisect.bisect_left(ts[tf], decision)
    if hi == 0:
        return []
    last = series[tf][hi - 1]
    if last.ts + timedelta(seconds=SEC[tf]) <= decision:
        return series[tf][max(0, hi - W): hi]
    out = list(series[tf][max(0, hi - W): hi - 1])
    lo = bisect.bisect_left(base_ts, last.ts)
    parts = base[lo: k + 1]
    if parts:
        out.append(Candle(ts=last.ts, open=parts[0].open, high=max(p.high for p in parts),
                          low=min(p.low for p in parts), close=parts[-1].close, volume=sum(p.volume for p in parts)))
    return out[-W:]


def run_symbol(args):
    path, sym, ac, hours, disable, trend_only = args
    series = {tf: candles(path, sym, tf) for tf in ("15m", "1h", "4h", "1d")}
    base = series["15m"]
    ts = {tf: [c.ts for c in series[tf]] for tf in series}
    base_ts = ts["15m"]
    start_ts = base[-1].ts - timedelta(hours=hours * 1.45)       # ~hours of trading 1h bars
    k0 = max(bisect.bisect_left(base_ts, start_ts), 900)
    fund = _neutral_fundamental(sym)
    out = []
    busy_until = {15: -1, 30: -1, 45: -1, 60: -1}
    for k in range(k0, len(base) - 1):
        close_min = (base[k].ts + timedelta(minutes=15)).minute
        off = 60 if close_min == 0 else close_min
        if off not in busy_until or k <= busy_until[off]:
            continue
        window = [OHLCVSeries(symbol=sym, timeframe=tf, candles=ctx(series, ts, base, base_ts, k, tf))
                  for tf in ("1h", "4h", "1d")]
        if any(not w.candles or len(w.candles) < 60 for w in window):
            continue
        technical = run_technical(sym, window, use_llm=False)
        prop = _deterministic_decision(sym, AssetClass(ac), "1h", technical, fund, now=base[k].ts,
                                       trend_only=trend_only, disable=frozenset(disable))
        if not prop.is_actionable or not prop.take_profit or not prop.stop_loss:
            continue
        entry, stop, tp = base[k].close, float(prop.stop_loss), float(prop.take_profit)
        is_long = prop.direction == Direction.LONG
        risk = (entry - stop) if is_long else (stop - entry)
        if risk <= 0 or ((tp <= entry) if is_long else (tp >= entry)):
            continue
        end = min(k + 96 * 4, len(base) - 1)
        exit_px, j_exit, res = base[end].close, end, "timeout"
        for j in range(k + 1, end + 1):
            hit = _exit_price(prop.direction, base[j], stop, tp)
            if hit is not None:
                exit_px, res, j_exit = hit[0], hit[1], j
                break
        r = ((exit_px - entry) if is_long else (entry - exit_px)) / risk
        out.append({"symbol": sym, "offset": off, "entry_time": base[k].ts.isoformat(), "entry": entry,
                    "stop": stop, "r": round(r, 4), "outcome": res, "regime": prop.regime,
                    "strategy": prop.strategy, "direction": prop.direction.value})
        busy_until[off] = j_exit + 4 * 3
    return out


def main():
    import argparse
    import multiprocessing as mp

    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--out")
    ap.add_argument("--symbols")
    ap.add_argument("--hours", type=int, default=2000)
    ap.add_argument("--disable", default="")
    ap.add_argument("--trend-only", action="store_true")
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    ac = data(a.data)["asset_class"]
    jobs = [(a.data, s, ac[s], a.hours, [d for d in a.disable.split(",") if d], a.trend_only)
            for s in a.symbols.split(",") if s]
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        res = pool.map(run_symbol, jobs, chunksize=1)
    tr = [t for r in res for t in r]
    json.dump(tr, open(a.out, "w"))
    print(f"intrabar: {len(tr)} trades in {time.time() - t0:.0f}s -> {a.out}")


if __name__ == "__main__":
    main()
