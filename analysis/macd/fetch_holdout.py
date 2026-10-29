"""Read-only candle export for the holdout: symbols no analysis in this repo has used (see PREREG.md).

Same shape as analysis/ai_reads/fetch.py, so replay.py and evaluate.py run on it unchanged. A symbol
the terminal had to add to Market Watch to serve its history is removed again afterwards.

Usage (from backend/, MT5 terminal running):  python ../analysis/macd/fetch_holdout.py <out.pkl>
"""
import datetime as dt
import pickle
import sys
import time

import MetaTrader5 as mt5

OUT = sys.argv[1]
SYMS = {
    # the unseen G10 crosses of analysis/loss_audit/ob_path_preregistration.md, minus AUDCADm and
    # EURCADm (now on the watchlist, so in the in-sample 33)
    "CADCHFm": "forex", "EURAUDm": "forex", "EURCHFm": "forex", "EURGBPm": "forex", "EURNZDm": "forex",
    "GBPAUDm": "forex", "GBPCADm": "forex", "GBPCHFm": "forex", "GBPNZDm": "forex", "NZDCADm": "forex",
    "NZDCHFm": "forex", "NZDJPYm": "forex", "NZDUSDm": "forex", "USDCADm": "forex",
    # metals with an underlying of their own (gold/silver in other currencies are left out: they are
    # the in-sample XAUUSD/XAGUSD moves restated)
    "XALUSDm": "metal", "XCUUSDm": "metal", "XNIUSDm": "metal", "XPBUSDm": "metal", "XPDUSDm": "metal",
    "DXYm": "index",
}
TFS = {"1h": (mt5.TIMEFRAME_H1, 7000), "4h": (mt5.TIMEFRAME_H4, 2500), "1d": (mt5.TIMEFRAME_D1, 900)}

assert mt5.initialize(), mt5.last_error()
data = {"asset_class": {}, "watchlist": [], "bars": {}, "spec": {},
        "fetched_at": dt.datetime.now(dt.timezone.utc)}
added = []
for sym, ac in SYMS.items():
    si = mt5.symbol_info(sym)
    if si is None:
        print("missing", sym, flush=True)
        continue
    if not si.visible:
        mt5.symbol_select(sym, True)
        added.append(sym)
    for tf, (code, n) in TFS.items():
        r = mt5.copy_rates_from_pos(sym, code, 0, n)
        if r is None or len(r) == 0:
            print("no data", sym, tf, mt5.last_error(), flush=True)
            continue
        data["bars"][(sym, tf)] = [(int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]),
                                    float(x["close"]), float(x["tick_volume"]), int(x["spread"])) for x in r]
        time.sleep(0.05)
    data["asset_class"][sym] = ac
    si = mt5.symbol_info(sym)
    data["spec"][sym] = dict(point=si.point, digits=si.digits, spread=si.spread)
    print(sym, {tf: len(data["bars"].get((sym, tf), [])) for tf in TFS}, flush=True)
for sym in added:
    mt5.symbol_select(sym, False)
mt5.shutdown()
pickle.dump(data, open(OUT, "wb"))
print("saved", OUT, f"(restored Market Watch: removed {len(added)} added symbols)", flush=True)
