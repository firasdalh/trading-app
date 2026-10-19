"""Deep DAILY history + financing terms for the time-series-momentum test (tsmom_preregistration.md).

Read-only against the running MT5 terminal. Stores, per symbol: D1 bars (as deep as the broker serves),
the contract spec, and the CURRENT swap terms (MT5 keeps no swap history - see the pre-registration).

Usage (from backend/):  python ../analysis/loss_audit/fetch_daily.py <out.pkl> <comma-separated symbols>
"""
import datetime as dt
import pickle
import sys
import time

import MetaTrader5 as mt5

OUT, SYMS = sys.argv[1], [s for s in sys.argv[2].split(",") if s]
assert mt5.initialize(), mt5.last_error()
data = {"bars": {}, "spec": {}, "fetched_at": dt.datetime.now(dt.timezone.utc)}
for sym in SYMS:
    si = mt5.symbol_info(sym)
    if si is None:
        print("missing", sym, flush=True)
        continue
    mt5.symbol_select(sym, True)
    r = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_D1, 0, 8000)
    if r is None or len(r) == 0:
        print("no data", sym, mt5.last_error(), flush=True)
        continue
    rows = [(int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
             float(x["tick_volume"]), int(x["spread"])) for x in r][:-1]   # drop the forming day
    data["bars"][sym] = rows
    data["spec"][sym] = dict(
        point=si.point, digits=si.digits, spread=si.spread, contract=si.trade_contract_size,
        tick_value=si.trade_tick_value, tick_size=si.trade_tick_size, volume_min=si.volume_min,
        volume_step=si.volume_step, swap_long=si.swap_long, swap_short=si.swap_short,
        swap_mode=si.swap_mode, swap_3day=si.swap_rollover3days, currency_profit=si.currency_profit,
        path=si.path,
    )
    first = dt.datetime.fromtimestamp(rows[0][0], dt.timezone.utc)
    print(f"{sym:13} {len(rows):5} D1 bars from {first:%Y-%m-%d}  swap L/S {si.swap_long}/{si.swap_short} mode {si.swap_mode}",
          flush=True)
    time.sleep(0.05)
mt5.shutdown()
pickle.dump(data, open(OUT, "wb"))
print("saved", OUT, "symbols:", len(data["bars"]), flush=True)
