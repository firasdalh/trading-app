"""Instrumented faithful backtest of the deterministic engine: every trade carries the entry-TF indicator
snapshot, higher-TF context, its price path (MFE/MAE) and a stop x target counterfactual grid, so the
anatomy of the losses can be read instead of guessed.

Simulation is identical to bt.py --mode faithful (one position per symbol, stop checked first).
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

from bt import candles, data  # noqa: E402
from bt_armed import windows  # noqa: E402  (faithful context builder)

from app.agents.orchestrator import (  # noqa: E402
    _confirm_trend, _deterministic_decision, _higher_trend, _macro_trend, _structure_label, _macro_structure,
)
from app.agents.pipeline import _timeframes_for  # noqa: E402
from app.agents.technical import run_technical  # noqa: E402
from app.backtest.simulator import _WINDOW, _neutral_fundamental  # noqa: E402
from app.models.enums import AssetClass, Direction  # noqa: E402

STOP_K = (0.75, 1.0, 1.5, 2.0)
TGT_R = (1.0, 1.5, 2.0, 3.0, "plan")
MAX_HOLD = 96


def first_hit(entry_c, i, is_long, entry, stop, target):
    """(+reward/risk | -1 | mark-to-market R at timeout, bars) with the stop checked first."""
    risk = abs(entry - stop)
    end = min(i + MAX_HOLD, len(entry_c) - 1)
    for j in range(i + 1, end + 1):
        b = entry_c[j]
        if is_long:
            if b.low <= stop:
                return -1.0, j - i
            if b.high >= target:
                return (target - entry) / risk, j - i
        else:
            if b.high >= stop:
                return -1.0, j - i
            if b.low <= target:
                return (entry - target) / risk, j - i
    px = entry_c[end].close
    return ((px - entry) if is_long else (entry - px)) / risk, end - i


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
        tf0 = next((x for x in technical.timeframes if x.timeframe == tf), None)
        ind = {k: v for k, v in (tf0.indicators if tf0 else {}).items() if isinstance(v, (int, float))}
        is_long = prop.direction == Direction.LONG
        entry, stop, tp = float(prop.entry), float(prop.stop_loss), float(prop.take_profit)
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue
        # actual trade (as the engine planned it)
        r, held = first_hit(entry_c, i, is_long, entry, stop, tp)
        # path excursions over the actual holding period (in units of planned risk)
        mfe = mae = 0.0
        mfe_bar = 0
        for j in range(i + 1, min(i + held, n - 1) + 1):
            b = entry_c[j]
            fav = ((b.high - entry) if is_long else (entry - b.low)) / risk
            adv = ((entry - b.low) if is_long else (b.high - entry)) / risk
            if fav > mfe:
                mfe, mfe_bar = fav, j - i
            mae = max(mae, adv)
        # counterfactual grid: stop at k x planned risk, target at t x planned risk (or the plan's target)
        grid = {}
        for k in STOP_K:
            st = entry - k * risk if is_long else entry + k * risk
            for t in TGT_R:
                tg = tp if t == "plan" else (entry + t * risk if is_long else entry - t * risk)
                grid[f"{k}x{t}"] = round(first_hit(entry_c, i, is_long, entry, st, tg)[0], 3)
        # time exits (mark-to-market R at bar close N bars later, ignoring stops)
        mtm = {}
        for N in (1, 2, 4, 8, 16):
            if i + N < n:
                px = entry_c[i + N].close
                mtm[str(N)] = round(((px - entry) if is_long else (entry - px)) / risk, 3)
        htf, htf_name = _higher_trend(technical, tf)
        conf_tr, conf_name = _confirm_trend(technical, tf)
        out.append({
            "symbol": sym, "entry_time": entry_c[i].ts.isoformat(), "direction": prop.direction.value,
            "regime": prop.regime, "strategy": prop.strategy, "conf": prop.confidence, "alignment": prop.alignment,
            "entry": entry, "stop": stop, "target": tp, "planned_rr": round(abs(tp - entry) / risk, 3),
            "stop_atr": round(risk / ind["atr14"], 3) if ind.get("atr14") else None,
            "r": round(r, 4), "bars_held": held, "mfe": round(mfe, 3), "mae": round(mae, 3), "mfe_bar": mfe_bar,
            "grid": grid, "mtm": mtm, "ind": ind, "htf": htf, "daily": conf_tr, "macro": _macro_trend(technical),
            "struct": _structure_label(tf0.indicators) if tf0 else None, "macro_struct": _macro_structure(technical),
            "rationale": (prop.rationale or "")[:400],
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
    print(f"diag: {len(tr)} trades in {time.time() - t0:.0f}s -> {a.out}")


if __name__ == "__main__":
    main()
