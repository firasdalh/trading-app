"""Summarise backtest trade files: gross + net-of-spread expectancy, IS/OOS, by symbol/regime/strategy."""
import json
import pickle
import statistics as st
import sys
from collections import defaultdict

S = sys.argv[1]
files = sys.argv[2:]
spec = pickle.load(open(S + "/bars.pkl", "rb")).get("spec", {})
WATCH = {"XAUUSDm", "JP225m", "USOILm", "USTECm", "HK50m", "BTCUSDm", "XNGUSDm", "DE30m", "AUS200m",
         "US500_x100m", "FR40m", "STOXX50m", "UK100m", "AUDUSDm"}


def cost_r(t):
    sp = spec.get(t["symbol"], {})
    spread_px = (sp.get("spread") or 0) * (sp.get("point") or 0)
    risk = abs(t.get("entry", t.get("fill", 0)) - t["stop"]) if "entry" in t else t.get("risk") or 0
    if not risk:
        return 0.05
    return max(spread_px / risk, 0.01)


def stats(tr, key="r_net"):
    if not tr:
        return "n=0"
    rs = [t[key] for t in tr]
    w = [r for r in rs if r > 0]
    lo = [r for r in rs if r <= 0]
    pf = (sum(w) / -sum(lo)) if lo and sum(lo) < 0 else float("inf")
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return (f"n={len(rs):4d} win={100 * len(w) / len(rs):4.1f}% exp={st.mean(rs):+.3f}R "
            f"PF={pf:4.2f} tot={sum(rs):+7.1f}R maxDD={dd:5.1f}R")


for f in files:
    tr = json.load(open(f))
    for t in tr:
        t["cost"] = cost_r(t)
        t["r_net"] = t["r"] - t["cost"]
    tr.sort(key=lambda t: t["entry_time"])
    print("=" * 100)
    print(f.split("/")[-1].split("\\")[-1], "| median cost/trade", round(st.median([t["cost"] for t in tr]), 3) if tr else "-")
    print("  ALL gross :", stats(tr, "r"))
    print("  ALL net   :", stats(tr))
    cut = int(len(tr) * 0.7)
    if tr:
        print(f"  IS  net   : {stats(tr[:cut])}   (to {tr[cut - 1]['entry_time'][:10]})")
        print(f"  OOS net   : {stats(tr[cut:])}")
    wl = [t for t in tr if t["symbol"] in WATCH]
    print("  WATCHLIST14 net:", stats(wl))
    recent = [t for t in tr if t["entry_time"] >= "2026-07-08"]
    print("  since Jul-8 net:", stats(recent), " | watchlist:", stats([t for t in recent if t["symbol"] in WATCH]))
    for key in ("regime", "strategy", "direction", "dir", "outcome", "variant"):
        if tr and key in tr[0]:
            g = defaultdict(list)
            for t in tr:
                g[t.get(key)].append(t)
            if len(g) > 1:
                print(f"  by {key}:")
                for k, v in sorted(g.items(), key=lambda kv: -len(kv[1])):
                    print(f"    {str(k):22} {stats(v)}")
    g = defaultdict(list)
    for t in tr:
        g[t["symbol"]].append(t)
    print("  by symbol (net), with IS/OOS expectancy:")
    for k, v in sorted(g.items(), key=lambda kv: -sum(t["r_net"] for t in kv[1])):
        c = int(len(v) * 0.7)
        is_e = st.mean([t["r_net"] for t in v[:c]]) if c else 0
        oos_e = st.mean([t["r_net"] for t in v[c:]]) if len(v) - c else 0
        star = "*" if k in WATCH else " "
        print(f"   {star}{k:14} {stats(v)}  IS {is_e:+.2f} OOS {oos_e:+.2f}")
