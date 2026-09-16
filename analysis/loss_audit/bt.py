"""Offline backtest harness over cached MT5 candles.

mode='lookahead' : the repo's historical behaviour (higher-TF bar whose OPEN <= t_i is included whole)
mode='faithful'  : completed higher-TF bars + a PARTIAL forming bar built from entry-TF bars up to the
                   decision bar's close — exactly what the live app sees at that moment.
"""
import bisect, pickle, sys, os, json, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.getcwd())
os.environ.setdefault("LOG_LEVEL", "WARNING")
from app.models.schemas import OHLCVSeries, Candle
from app.models.enums import AssetClass, Direction
from app.agents.orchestrator import _deterministic_decision
from app.agents.technical import run_technical
from app.backtest.simulator import _simulate_trade, _neutral_fundamental, _WINDOW, BTTrade
from app.agents.pipeline import _timeframes_for
import logging; logging.disable(logging.WARNING)

DUR = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_DATA = None
def data(path):
    global _DATA
    if _DATA is None:
        _DATA = pickle.load(open(path, "rb"))
    return _DATA

def candles(path, sym, tf):
    rows = data(path)["bars"].get((sym, tf), [])[:-1]          # drop the bar still forming at fetch time
    return [Candle(ts=datetime.fromtimestamp(r[0], tz=timezone.utc), open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in rows]

def run_symbol(args):
    path, sym, ac, tf, bars, mode, trend_only, disable, max_hold, cooldown, cost_r = args
    tfs = _timeframes_for(tf)
    series = {t: candles(path, sym, t) for t in tfs}
    entry = series[tf][-bars:] if bars else series[tf]
    if len(entry) < _WINDOW + 5:
        return []
    ts_open = {t: [c.ts.timestamp() for c in series[t]] for t in tfs}
    e_ts = [c.ts.timestamp() for c in series[tf]]
    fund = _neutral_fundamental(sym)
    out = []; n = len(entry); i = _WINDOW - 1; block = -1
    while i < n:
        if i <= block:
            i += 1; continue
        t_i = entry[i].ts.timestamp()
        window = []
        for t in tfs:
            if t == tf:
                w = entry[max(0, i - _WINDOW + 1): i + 1]
            elif mode == "lookahead":
                hi = bisect.bisect_right(ts_open[t], t_i)
                w = series[t][max(0, hi - _WINDOW): hi]
            else:
                decision = t_i + DUR[tf]                         # the entry bar has just CLOSED
                hi = bisect.bisect_right(ts_open[t], t_i)        # bars opened at/before the entry bar
                done = [c for c in series[t][max(0, hi - _WINDOW - 1): hi] if c.ts.timestamp() + DUR[t] <= decision]
                cur_open = None
                if hi:
                    last = series[t][hi - 1]
                    if last.ts.timestamp() + DUR[t] > decision:
                        cur_open = last.ts.timestamp()
                if cur_open is not None:                         # synthesize the forming bar from entry-TF bars
                    lo = bisect.bisect_left(e_ts, cur_open)
                    j_end = bisect.bisect_right(e_ts, t_i)
                    parts = series[tf][lo:j_end]
                    if parts:
                        done.append(Candle(ts=datetime.fromtimestamp(cur_open, tz=timezone.utc), open=parts[0].open,
                                           high=max(p.high for p in parts), low=min(p.low for p in parts),
                                           close=parts[-1].close, volume=sum(p.volume for p in parts)))
                w = done[-_WINDOW:]
            if not w:
                break
            window.append(OHLCVSeries(symbol=sym, timeframe=t, candles=w))
        else:
            technical = run_technical(sym, window, use_llm=False)
            prop = _deterministic_decision(sym, AssetClass(ac), tf, technical, fund, now=entry[i].ts,
                                           trend_only=trend_only, disable=frozenset(disable))
            if prop.is_actionable and prop.take_profit is not None:
                tf0 = next((x for x in technical.timeframes if x.timeframe == tf), None)
                atr = tf0.indicators.get("atr14") if tf0 else None
                tr = _simulate_trade(sym, entry, i, prop, max_hold=max_hold, cost_r=cost_r, atr=atr)
                if tr is not None:
                    d = tr.__dict__.copy(); d["entry_time"] = d["entry_time"].isoformat(); d["exit_time"] = d["exit_time"].isoformat()
                    d["conf"] = prop.confidence; d["alignment"] = prop.alignment
                    d["adx"] = tf0.indicators.get("adx") if tf0 else None
                    out.append(d)
                    block = i + tr.bars_held + cooldown
                    i = i + tr.bars_held + 1
                    continue
            i += 1
            continue
        i += 1
    return out

def main():
    import multiprocessing as mp, argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data"); ap.add_argument("--out"); ap.add_argument("--symbols"); ap.add_argument("--tf", default="1h")
    ap.add_argument("--bars", type=int, default=3000); ap.add_argument("--mode", default="faithful")
    ap.add_argument("--trend-only", action="store_true"); ap.add_argument("--disable", default="")
    ap.add_argument("--max-hold", type=int, default=96); ap.add_argument("--cooldown", type=int, default=3)
    ap.add_argument("--cost-r", type=float, default=0.0); ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    ac = data(a.data)["asset_class"]
    syms = [s for s in a.symbols.split(",") if s]
    dis = [d for d in a.disable.split(",") if d]
    jobs = [(a.data, s, ac[s], a.tf, a.bars, a.mode, a.trend_only, dis, a.max_hold, a.cooldown, a.cost_r) for s in syms]
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        res = pool.map(run_symbol, jobs, chunksize=1)
    trades = [t for r in res for t in r]
    json.dump(trades, open(a.out, "w"))
    print(f"{a.mode} trend_only={a.trend_only} disable={dis}: {len(trades)} trades in {time.time()-t0:.0f}s -> {a.out}")

if __name__ == "__main__":
    main()
