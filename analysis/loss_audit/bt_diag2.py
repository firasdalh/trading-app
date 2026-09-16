"""Extended instrumented faithful backtest: entry-TF + 4h + 1d indicator snapshots, today's partial daily
bar (range used vs daily ATR, position in the day's range), prior day/week levels, and the full forward
path (per bar: best favourable excursion, worst adverse excursion, close — all in planned-R units, direction-
relative) so exit rules can be replayed exactly. Same simulation as bt_diag.py.
"""
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.disable(logging.WARNING)

from bt import candles, data  # noqa: E402
from bt_armed import windows  # noqa: E402
from bt_diag import first_hit  # noqa: E402

from app.agents.orchestrator import _deterministic_decision  # noqa: E402
from app.agents.pipeline import _timeframes_for  # noqa: E402
from app.agents.technical import run_technical  # noqa: E402
from app.backtest.simulator import _WINDOW, _neutral_fundamental  # noqa: E402
from app.models.enums import AssetClass, Direction  # noqa: E402

PATH_BARS = 96


def nums(d):
    return {k: v for k, v in (d or {}).items() if isinstance(v, (int, float))}


def run_symbol(args):
    path, sym, ac, tf, bars, trend_only, disable = args
    tfs = _timeframes_for(tf)
    series = {t: candles(path, sym, t) for t in tfs}
    entry_c = series[tf][-bars:]
    if len(entry_c) < _WINDOW + 5:
        return []
    ts_open = {t: [c.ts.timestamp() for c in series[t]] for t in tfs}
    e_ts = [c.ts.timestamp() for c in series[tf]]
    fund = _neutral_fundamental(sym)
    out = []
    n = len(entry_c)
    i = _WINDOW - 1
    block = -1
    while i < n:
        if i <= block:
            i += 1
            continue
        w = windows(series, tf, tfs, i, entry_c, ts_open, e_ts)
        if w is None:
            i += 1
            continue
        technical = run_technical(sym, w, use_llm=False)
        prop = _deterministic_decision(sym, AssetClass(ac), tf, technical, fund, now=entry_c[i].ts,
                                       trend_only=trend_only, disable=frozenset(disable))
        if not (prop.is_actionable and prop.take_profit and prop.stop_loss):
            i += 1
            continue
        reads = {t.timeframe: t for t in technical.timeframes}
        ind = nums(reads[tf].indicators if tf in reads else {})
        is_long = prop.direction == Direction.LONG
        sg = 1 if is_long else -1
        entry, stop, tp = float(prop.entry), float(prop.stop_loss), float(prop.take_profit)
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue
        r, held = first_hit(entry_c, i, is_long, entry, stop, tp)
        fwd = []
        for j in range(i + 1, min(i + PATH_BARS, n - 1) + 1):
            b = entry_c[j]
            fav = ((b.high - entry) if is_long else (entry - b.low)) / risk
            adv = ((entry - b.low) if is_long else (b.high - entry)) / risk
            fwd.append((round(fav, 3), round(adv, 3), round(sg * (b.close - entry) / risk, 3)))
        day = {}
        d1 = next((s for s in w if s.timeframe == "1d"), None)
        d_ind = nums(reads["1d"].indicators) if "1d" in reads else {}
        if d1 and d1.candles and d_ind.get("atr14"):
            t0 = d1.candles[-1]
            atr_d = d_ind["atr14"]
            rng = t0.high - t0.low
            day = {"range_used": round(rng / atr_d, 3),
                   "move_from_open": round(sg * (t0.close - t0.open) / atr_d, 3),
                   "pos_in_range": round(((t0.close - t0.low) / rng if is_long else (t0.high - t0.close) / rng), 3)
                   if rng > 0 else 0.5,
                   "prev_range": round((d1.candles[-2].high - d1.candles[-2].low) / atr_d, 3) if len(d1.candles) > 1 else None}
        out.append({
            "symbol": sym, "entry_time": entry_c[i].ts.isoformat(), "direction": prop.direction.value,
            "regime": prop.regime, "strategy": prop.strategy, "conf": prop.confidence, "alignment": prop.alignment,
            "entry": entry, "stop": stop, "target": tp, "planned_rr": round(abs(tp - entry) / risk, 3),
            "atr_r": round(ind["atr14"] / risk, 4) if ind.get("atr14") else None,
            "r": round(r, 4), "bars_held": held, "fwd": fwd,
            "ind": ind, "h4": nums(reads["4h"].indicators) if "4h" in reads else {}, "d1": d_ind, "day": day,
        })
        block = i + held + 3
        i = i + held + 1
    return out


def main():
    import argparse
    import multiprocessing as mp

    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--out")
    ap.add_argument("--symbols")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--bars", type=int, default=6500)
    ap.add_argument("--trend-only", action="store_true")
    ap.add_argument("--disable", default="")
    ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    ac = data(a.data)["asset_class"]
    jobs = [(a.data, s, ac[s], a.tf, a.bars, a.trend_only, [d for d in a.disable.split(",") if d])
            for s in a.symbols.split(",") if s]
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        res = pool.map(run_symbol, jobs, chunksize=1)
    tr = [t for r in res for t in r]
    json.dump(tr, open(a.out, "w"))
    print(f"diag2: {len(tr)} trades in {time.time() - t0:.0f}s -> {a.out}")


if __name__ == "__main__":
    main()
