"""Read-only candle export for the AI-read test: 1h/4h/1d for the watchlist plus the loss-audit universe.

Usage (from backend/, MT5 terminal running):  python ../analysis/ai_reads/fetch.py <out.pkl>
"""
import datetime as dt
import pickle
import sys
import time

import MetaTrader5 as mt5

OUT = sys.argv[1]
# Asset classes as the live watchlist files them (the engine sizes stops by class).
SYMS = {
    # watchlist 2026-10-28 (IN50m left out: no prices since 2023-06)
    "EURUSDm": "forex", "USDJPYm": "forex", "AUDCHFm": "forex", "XPTUSDm": "forex", "XZNUSDm": "forex",
    "XAUUSDm": "metal", "BTCUSDm": "crypto", "USOILm": "energy", "XNGUSDm": "energy", "STOXX50m": "index",
    "FR40m": "index", "JP225m": "index", "DE30m": "index", "HK50m": "index", "US500m": "index",
    "ETHUSDm": "crypto", "EURCADm": "forex", "CADJPYm": "forex", "AUDCADm": "forex",
    # loss-audit universe (analysis/loss_audit/fetch_data.py), for sample size
    "USTECm": "index", "AUS200m": "index", "UK100m": "index", "AUDUSDm": "forex", "AUDNZDm": "forex",
    "USDCHFm": "forex", "XAGUSDm": "metal", "UKOILm": "energy", "CHFJPYm": "forex", "GBPUSDm": "forex",
    "AUDJPYm": "forex", "US30m": "index", "EURJPYm": "forex", "GBPJPYm": "forex",
}
WATCHLIST = list(SYMS)[:19]
TFS = {"1h": (mt5.TIMEFRAME_H1, 7000), "4h": (mt5.TIMEFRAME_H4, 2500), "1d": (mt5.TIMEFRAME_D1, 900)}

assert mt5.initialize(), mt5.last_error()
data = {"asset_class": {}, "watchlist": WATCHLIST, "bars": {}, "spec": {},
        "fetched_at": dt.datetime.now(dt.timezone.utc)}
for sym, ac in SYMS.items():
    si = mt5.symbol_info(sym)
    if si is None:
        print("missing", sym, flush=True)
        continue
    mt5.symbol_select(sym, True)
    for tf, (code, n) in TFS.items():
        r = mt5.copy_rates_from_pos(sym, code, 0, n)
        if r is None or len(r) == 0:
            print("no data", sym, tf, mt5.last_error(), flush=True)
            continue
        data["bars"][(sym, tf)] = [(int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]),
                                    float(x["close"]), float(x["tick_volume"]), int(x["spread"])) for x in r]
        time.sleep(0.05)
    data["asset_class"][sym] = ac
    data["spec"][sym] = dict(point=si.point, digits=si.digits, spread=si.spread)
    print(sym, {tf: len(data["bars"].get((sym, tf), [])) for tf in TFS}, flush=True)
mt5.shutdown()
pickle.dump(data, open(OUT, "wb"))
print("saved", OUT, flush=True)
