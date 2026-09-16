"""One-shot, read-only candle export from the running MT5 terminal into a local pickle."""
import MetaTrader5 as mt5, pickle, sys, datetime as dt, time
OUT = sys.argv[1]
SYMS = {
 # current watchlist
 "XAUUSDm":"metal","JP225m":"index","USOILm":"energy","USTECm":"index","HK50m":"index","BTCUSDm":"crypto",
 "XNGUSDm":"energy","DE30m":"index","AUS200m":"index","US500_x100m":"index","FR40m":"index","STOXX50m":"index",
 "UK100m":"index","AUDUSDm":"forex",
 # previously validated core / traded
 "AUDNZDm":"forex","CADJPYm":"forex","EURUSDm":"forex","USDCHFm":"forex","XAGUSDm":"metal","XAGGBPm":"metal",
 "XAGAUDm":"metal","ETHUSDm":"crypto","UKOILm":"energy","AUDCHFm":"forex","USDJPYm":"forex","CHFJPYm":"forex",
 "GBPUSDm":"forex","AUDJPYm":"forex","XAGEURm":"metal","US30m":"index","XAUAUDm":"metal","EURJPYm":"forex","GBPJPYm":"forex",
}
TFS = {"15m":(mt5.TIMEFRAME_M15, 12000), "1h":(mt5.TIMEFRAME_H1, 7000), "4h":(mt5.TIMEFRAME_H4, 2500), "1d":(mt5.TIMEFRAME_D1, 900)}
assert mt5.initialize(), mt5.last_error()
data = {"asset_class": SYMS, "bars": {}, "fetched_at": dt.datetime.now(dt.timezone.utc)}
for sym in SYMS:
    if mt5.symbol_info(sym) is None:
        print("missing", sym); continue
    mt5.symbol_select(sym, True)
    for tf,(code,n) in TFS.items():
        r = mt5.copy_rates_from_pos(sym, code, 0, n)
        if r is None:
            print("no data", sym, tf, mt5.last_error()); continue
        data["bars"][(sym, tf)] = [(int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), float(x["tick_volume"])) for x in r]
        time.sleep(0.05)
    print(sym, {tf: len(data["bars"].get((sym, tf), [])) for tf in TFS}, flush=True)
    si = mt5.symbol_info(sym)
    data.setdefault("spec", {})[sym] = dict(point=si.point, digits=si.digits, spread=si.spread, contract=si.trade_contract_size, tick_value=si.trade_tick_value, tick_size=si.trade_tick_size)
mt5.shutdown()
pickle.dump(data, open(OUT, "wb"))
print("saved", OUT)
