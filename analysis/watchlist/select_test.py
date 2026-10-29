"""Which symbols should the Hybrid trade? Two questions, answered on the live engine's replayed trades.

1. Does picking symbols by their recent results work? Walk forward month by month: at the start of
   each month keep only the symbols whose trades over the previous L months made money (no trades =
   kept), trade them for that month, and compare with trading everything. Each month's choice uses
   only the past, so the whole run is out-of-sample.
2. Are some groups of instruments losers in every period? Group results in each half of the in-sample
   period and on the holdout.

Trades come from analysis/macd/replay.py (variant "B" = the live engine), path-dependent per symbol,
Hybrid bar (confidence > 0.68). The live spread gate (spread <= 15% of the stop distance) is applied
using the replay's per-trade cost, since the replay itself only charges the spread.

Rule fixed in this docstring before the first run: selection is worth doing only if it beats trading
everything in total R for every lookback L in (3, 6, 9) AND in both halves of the evaluation window.

Usage (from backend/):  python ../analysis/watchlist/select_test.py <in_replay> <in.pkl> <hold_replay> <hold.pkl>
"""
import glob
import json
import os
import pickle
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "macd"))
from evaluate import path, zero  # noqa: E402

SPREAD_MAX = 0.15
LOOKBACKS = (3, 6, 9)

GROUPS = {
    "gold, silver, platinum, palladium": {"XAUUSDm", "XAGUSDm", "XPTUSDm", "XPDUSDm"},
    "base metals": {"XZNUSDm", "XALUSDm", "XCUUSDm", "XNIUSDm", "XPBUSDm"},
    "energy": {"USOILm", "UKOILm", "XNGUSDm"},
    "indices": {"US500m", "USTECm", "US30m", "DE30m", "FR40m", "STOXX50m", "UK100m", "JP225m", "HK50m",
                "AUS200m"},
    "crypto": {"BTCUSDm", "ETHUSDm"},
    "FX majors + DXY": {"EURUSDm", "GBPUSDm", "AUDUSDm", "NZDUSDm", "USDJPYm", "USDCHFm", "USDCADm", "DXYm"},
}


def group_of(sym):
    for g, members in GROUPS.items():
        if sym in members:
            return g
    return "FX crosses"


def load(xdir):
    ex = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(xdir, "replay_*.json")))]
    trades = {tid: t for e in ex for tid, t in e["trades"].items()}
    rows = {e["symbol"]: sorted(e["rows"], key=lambda r: r["i"]) for e in ex}
    return trades, rows


def spread_ok(t):
    return t["cost"] <= SPREAD_MAX


def month(t):
    return t["entry_time"][:7]


def summary(v):
    if not v:
        return "n=0"
    rs = [t["net"] for t in sorted(v, key=lambda t: t["entry_time"])]
    eq = pk = dd = 0.0
    for r in rs:
        eq += r
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    return f"n={len(rs):4} {st.mean(rs):+.3f}R/trade total {sum(rs):+7.1f}R maxDD {dd:5.1f}R"


def walk_forward(rows, trades, syms, L, months):
    """Trades taken when each month keeps only the symbols with a non-negative trailing L-month total."""
    full = {s: path(rows[s], trades, "B", zero, spread_ok, True) for s in syms}
    by_sym_month = defaultdict(float)
    for s, v in full.items():
        for t in v:
            by_sym_month[(s, month(t))] += t["net"]
    keep = {}
    for k, m in enumerate(months):
        past = months[max(0, k - L):k]
        for s in syms:
            keep[(s, m)] = sum(by_sym_month[(s, p)] for p in past) >= 0
    return [t for s in syms
            for t in path(rows[s], trades, "B", zero, lambda t: spread_ok(t) and keep[(t["symbol"], month(t))], True)]


def main():
    in_trades, in_rows = load(sys.argv[1])
    data = pickle.load(open(sys.argv[2], "rb"))
    ho_trades, ho_rows = load(sys.argv[3])
    trades = {**in_trades, **ho_trades}
    rows = {**in_rows, **ho_rows}
    watch = [s for s in data["watchlist"] if s in rows]

    # ---- spread gate: what it removes ----
    print("=== live spread gate (spread <= 15% of the stop): Hybrid trades with vs without it ===")
    for name, syms in (("in-sample 33", list(in_rows)), ("holdout 20", list(ho_rows)), ("watchlist 19", watch)):
        raw = [t for s in syms for t in path(rows[s], trades, "B", zero, lambda t: True, True)]
        gated = [t for s in syms for t in path(rows[s], trades, "B", zero, spread_ok, True)]
        print(f"  {name:13} without gate {summary(raw)}\n  {'':13} with gate    {summary(gated)}")

    # ---- groups by period ----
    print("\n=== groups (spread-gated Hybrid trades) ===")
    in_all = {s: path(in_rows[s], trades, "B", zero, spread_ok, True) for s in in_rows}
    ho_all = {s: path(ho_rows[s], trades, "B", zero, spread_ok, True) for s in ho_rows}
    times = sorted(t["entry_time"] for v in in_all.values() for t in v)
    half = times[len(times) // 2][:10]
    print(f"  in-sample halves split at {half}")
    for g in list(GROUPS) + ["FX crosses"]:
        a = [t for s, v in in_all.items() if group_of(s) == g for t in v if t["entry_time"] < half]
        b = [t for s, v in in_all.items() if group_of(s) == g for t in v if t["entry_time"] >= half]
        h = [t for s, v in ho_all.items() if group_of(s) == g for t in v]
        print(f"  {g:34} 1st half {summary(a)}\n  {'':34} 2nd half {summary(b)}\n  {'':34} holdout  {summary(h)}")

    # ---- walk-forward selection ----
    print("\n=== walk-forward: each month trade only symbols whose last L months made money ===")
    for name, syms in (("all 53 symbols", list(rows)), ("in-sample 33", list(in_rows)), ("watchlist 19", watch)):
        base = [t for s in syms for t in path(rows[s], trades, "B", zero, spread_ok, True)]
        months = sorted({month(t) for t in base})
        ev = [m for m in months[max(LOOKBACKS):]]       # same evaluation months for every L
        mid = ev[len(ev) // 2]
        print(f"\n  {name}: evaluation {ev[0]} -> {ev[-1]} ({len(ev)} months), halves split at {mid}")
        b_in = [t for t in base if month(t) in ev]
        print(f"    trade everything      {summary(b_in)} | halves "
              f"{sum(t['net'] for t in b_in if month(t) < mid):+.1f}R / {sum(t['net'] for t in b_in if month(t) >= mid):+.1f}R")
        for L in LOOKBACKS:
            v = [t for t in walk_forward(rows, trades, syms, L, months) if month(t) in ev]
            print(f"    select by last {L} mo.  {summary(v)} | halves "
                  f"{sum(t['net'] for t in v if month(t) < mid):+.1f}R / {sum(t['net'] for t in v if month(t) >= mid):+.1f}R")

    # ---- the watchlist, symbol by symbol (information only: 15-40 trades each is mostly noise) ----
    print("\n=== watchlist symbols, spread-gated Hybrid trades (per symbol = noisy) ===")
    for s in sorted(watch, key=lambda s: -sum(t["net"] for t in in_all.get(s, []))):
        v = in_all.get(s, [])
        a = sum(t["net"] for t in v if t["entry_time"] < half)
        b = sum(t["net"] for t in v if t["entry_time"] >= half)
        print(f"  {s:10} {group_of(s):34} {summary(v)} | halves {a:+.1f}R / {b:+.1f}R")


if __name__ == "__main__":
    main()
