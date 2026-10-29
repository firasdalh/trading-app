"""Pass 1 of the AI-read test: replay every bar and find where an AI label could change a trade. No model calls.

Each bar is decided under eight variants that force each read's "go" answer (healthy pullback / likely
break / choppy range / emerging trend). Because a non-"go" label always ends in no market entry, these
variants are enough to rebuild the engine's decision for ANY labelling afterwards (see evaluate.py), so
the model is only asked about decisions where its answer can matter.

Usage (from backend/):  python ../analysis/ai_reads/explore.py <bars.pkl> <out_dir> [--procs 7]
"""
import json
import os
import pickle
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LOG_LEVEL", "WARNING")
import logging  # noqa: E402

logging.disable(logging.WARNING)

from app.agents.orchestrator import _deterministic_decision  # noqa: E402
from app.agents.pipeline import _UNPROVEN_FILTERS, _timeframes_for  # noqa: E402
from app.agents.technical import run_technical  # noqa: E402
from app.backtest.simulator import _WINDOW, _context_window, _neutral_fundamental, _simulate_trade  # noqa: E402
from app.models.enums import AssetClass  # noqa: E402
from app.models.schemas import Candle, OHLCVSeries  # noqa: E402

from oracle import install  # noqa: E402

TF = "1h"
DISABLE = frozenset(_UNPROVEN_FILTERS)      # the live set: settings.disabled_filters is empty
MAX_HOLD, COOLDOWN = 96, 3

GO = {"momentum": "healthy_pullback", "priceaction": "likely_break"}
# name: (trend_only, momentum_ai, regime_ai, priceaction_ai, forced labels)
VARIANTS = {
    "V0": (False, False, False, False, None),
    "Vm": (False, True, False, False, {"momentum": GO["momentum"]}),
    "Vp": (False, False, False, True, {"priceaction": GO["priceaction"]}),
    "Vmp": (False, True, False, True, dict(GO)),
    "Vr": (False, False, True, False, {"regime": "choppy_range"}),
    "T0": (True, False, False, False, None),
    "Tr": (True, False, True, False, {"regime": "emerging_trend"}),
    "Tmp": (True, True, True, True, {**GO, "regime": "emerging_trend"}),
}


def candles(rows):
    return [Candle(ts=datetime.fromtimestamp(r[0], tz=timezone.utc), open=r[1], high=r[2], low=r[3],
                   close=r[4], volume=r[5]) for r in rows]


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
    oracle = install()

    rows, trades, needed = [], {}, {}
    tid_of: dict = {}
    t0 = time.time()
    limit = int(os.environ.get("AIR_LIMIT", "0"))
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
            out, seen = {}, {}
            for name, (trend_only, mom, reg, pa, forced) in VARIANTS.items():
                oracle.forced, oracle.seen = forced or {}, []
                prop = _deterministic_decision(
                    sym, ac, TF, technical, fund, now=entry[i].ts, trend_only=trend_only, disable=DISABLE,
                    momentum_ai=mom, regime_ai=reg, priceaction_ai=pa,
                    entry_candles=entry[max(0, i - _WINDOW + 1): i + 1])
                if oracle.seen:
                    seen[name] = sorted({f"{f}:{k}" for f, k in oracle.seen})
                tid = None
                if prop.is_actionable and prop.take_profit is not None:
                    sig = (prop.direction.value, prop.entry, prop.stop_loss, prop.take_profit, prop.confidence,
                           prop.strategy)
                    tid = tid_of.get((i, sig))
                    if tid is None:
                        tr = _simulate_trade(sym, entry, i, prop, max_hold=MAX_HOLD, cost_r=0.0, atr=atr)
                        if tr is not None:
                            tid = f"{sym}:{i}:{len(tid_of)}"
                            tid_of[(i, sig)] = tid
                            sp = max(spec.get("spread") or 0, spreads.get(int(entry[i].ts.timestamp()), 0))
                            cost = max(sp * (spec.get("point") or 0) / abs(tr.entry - tr.stop), 0.01)
                            trades[tid] = dict(
                                symbol=sym, i=i, direction=tr.direction, regime=prop.regime,
                                strategy=prop.strategy, conf=prop.confidence, entry_time=tr.entry_time.isoformat(),
                                entry=tr.entry, stop=tr.stop, target=tr.target, planned_rr=tr.planned_rr,
                                exit_time=tr.exit_time.isoformat(), outcome=tr.outcome, r=tr.r,
                                cost=round(cost, 4), net=round(tr.r - cost, 4), bars_held=tr.bars_held)
                out[name] = tid
            if seen or any(out.values()):
                rows.append(dict(i=i, ts=entry[i].ts.isoformat(), out={k: v for k, v in out.items() if v},
                                 seen=seen))
                if any(out.values()):
                    for keys in seen.values():
                        for fk in keys:
                            f, k = fk.split(":")
                            needed[k] = oracle.texts[k]
    print(f"{sym}: {len(entry)} bars, {len(rows)} rows, {len(trades)} trades, {len(needed)} snapshots "
          f"to label, {time.time() - t0:.0f}s", flush=True)
    return dict(symbol=sym, asset_class=ac.value, n_bars=len(entry), rows=rows, trades=trades,
                needed={k: list(v) for k, v in needed.items()})


def main():
    import argparse
    import multiprocessing as mp

    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("out_dir")
    ap.add_argument("--procs", type=int, default=7)
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    syms = [s for s in a.symbols.split(",") if s] or list(pickle.load(open(a.data, "rb"))["asset_class"])
    os.makedirs(a.out_dir, exist_ok=True)
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        for res in pool.imap_unordered(run_symbol, [(a.data, s) for s in syms]):
            json.dump(res, open(os.path.join(a.out_dir, f"explore_{res['symbol']}.json"), "w"))
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
