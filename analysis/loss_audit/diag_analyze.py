"""Anatomy of the deterministic engine's trades (from bt_diag.py output)."""
import json
import pickle
import statistics as st
import sys
from collections import defaultdict

S, F = sys.argv[1], sys.argv[2]
GATE = len(sys.argv) > 3 and sys.argv[3] == "gate"
spec = pickle.load(open(S + "/bars.pkl", "rb"))["spec"]
AC = pickle.load(open(S + "/bars.pkl", "rb"))["asset_class"]
tr = json.load(open(F))
tr.sort(key=lambda t: t["entry_time"])


def cost(t, k=1.0):
    sp = spec.get(t["symbol"], {})
    px = (sp.get("spread") or 0) * (sp.get("point") or 0)
    risk = abs(t["entry"] - t["stop"]) * k
    return max(px / risk, 0.01) if risk else 0.05


for t in tr:
    t["net"] = t["r"] - cost(t)
if GATE:
    tr = [t for t in tr if (t["conf"] > 0.60 if t["strategy"] == "mean_reversion" else t["conf"] > 0.67)]
cut_time = tr[int(len(tr) * 0.7)]["entry_time"]
for t in tr:
    t["oos"] = t["entry_time"] >= cut_time


def ex(v, key="net"):
    return st.mean(x[key] for x in v) if v else float("nan")


def row(label, v):
    if not v:
        return f"  {label:34} n=   0"
    i = [x for x in v if not x["oos"]]
    o = [x for x in v if x["oos"]]
    return (f"  {label:34} n={len(v):4} win={100 * sum(x['r'] > 0 for x in v) / len(v):4.0f}% "
            f"net={ex(v):+.3f}  IS {ex(i):+.3f} (n={len(i)})  OOS {ex(o):+.3f} (n={len(o)})")


print(f"file={F.split('/')[-1]} gate={GATE} trades={len(tr)}  OOS from {cut_time[:10]}")
print(row("ALL", tr))
for s in sorted({t["strategy"] for t in tr}):
    print(row(f"strategy={s}", [t for t in tr if t["strategy"] == s]))
for s in sorted({t["regime"] for t in tr}):
    print(row(f"regime={s}", [t for t in tr if t["regime"] == s]))

trend = [t for t in tr if t["strategy"] == "trend"]
print("\n=== ANATOMY (trend trades) ===")
L = [t for t in trend if t["r"] <= 0]
W = [t for t in trend if t["r"] > 0]
for thr in (0.5, 1.0, 1.5):
    print(f"  losers that were +{thr}R in profit first: {100 * sum(t['mfe'] >= thr for t in L) / len(L):.0f}%")
for thr in (0.5, 0.8):
    print(f"  winners that were -{thr}R underwater first: {100 * sum(t['mae'] >= thr for t in W) / len(W):.0f}%")
print(f"  losers stopped within 2 bars: {100 * sum(t['bars_held'] <= 2 for t in L) / len(L):.0f}%   median loser MFE {st.median(t['mfe'] for t in L):.2f}R")
print("  signal decay — mean mark-to-market R N bars after entry (no stop), gross:")
for N in ("1", "2", "4", "8", "16"):
    v = [t["mtm"][N] for t in trend if N in t["mtm"]]
    print(f"    +{N:>2} bars: {st.mean(v):+.3f}R  (median {st.median(v):+.3f}, {100 * sum(x > 0 for x in v) / len(v):.0f}% positive)")

print("\n=== STOP x TARGET grid (trend trades, net of spread, R per trade in units of the NEW stop) ===")
keys = list(trend[0]["grid"].keys())
for k in ("0.75", "1.0", "1.5", "2.0"):
    cells = []
    for tgt in ("1.0", "1.5", "2.0", "3.0", "plan"):
        key = f"{k}x{tgt}"
        if key not in keys:
            continue
        # grid values are already R in units of the NEW stop (bt_diag.first_hit divides by it)
        def unit(t):
            return t["grid"][key] - cost(t, float(k))
        i = [unit(t) for t in trend if not t["oos"]]
        o = [unit(t) for t in trend if t["oos"]]
        cells.append(f"{tgt:>4}R: IS {st.mean(i):+.3f} OOS {st.mean(o):+.3f}")
    print(f"  stop {k}x | " + " | ".join(cells))


def seg(title, fn, pool=trend):
    g = defaultdict(list)
    for t in pool:
        k = fn(t)
        if k is not None:
            g[k].append(t)
    print(f"\n-- {title}")
    for k in sorted(g, key=str):
        print(row(str(k), g[k]))


def di_agree(t):
    p, m = t["ind"].get("plus_di"), t["ind"].get("minus_di")
    if p is None or m is None:
        return None
    return "DI agrees" if ((p > m) == (t["direction"] == "long")) else "DI AGAINST"


seg("+DI/-DI direction vs trade", di_agree)
seg("target aimed THROUGH a nearer level", lambda t: "through a level" if "strong trend through" in t["rationale"] else ("capped at level" if "capped at key level" in t["rationale"] else "clear path"))
seg("stop distance (ATR)", lambda t: None if t["stop_atr"] is None else ("a <1.05" if t["stop_atr"] < 1.05 else "b 1.05-1.5" if t["stop_atr"] < 1.5 else "c 1.5-2.2" if t["stop_atr"] < 2.2 else "d >=2.2"))
seg("entry distance from EMA20 (ATR)", lambda t: (lambda d: "a <0.5" if d < 0.5 else "b 0.5-1" if d < 1 else "c 1-1.5" if d < 1.5 else "d 1.5-2.5" if d < 2.5 else "e >=2.5")(abs(t["entry"] - t["ind"]["ema20"]) / t["ind"]["atr14"]) if t["ind"].get("ema20") and t["ind"].get("atr14") else None)
seg("RSI in trade direction", lambda t: (lambda r: "a <45" if r < 45 else "b 45-55" if r < 55 else "c 55-65" if r < 65 else "d 65-70" if r < 70 else "e >=70")(t["ind"]["rsi14"] if t["direction"] == "long" else 100 - t["ind"]["rsi14"]) if t["ind"].get("rsi14") is not None else None)
seg("ADX", lambda t: (lambda a: "a 20-23" if a < 23 else "b 23-28" if a < 28 else "c 28-35" if a < 35 else "d 35-45" if a < 45 else "e >=45")(t["ind"]["adx"]) if t["ind"].get("adx") else None)
seg("ADX rising vs falling", lambda t: None if t["ind"].get("adx_prev") is None else ("rising" if t["ind"]["adx"] >= t["ind"]["adx_prev"] else "falling"))
seg("MACD hist expanding in trade direction", lambda t: None if t["ind"].get("macd_hist_prev") is None else ("expanding" if ((t["ind"]["macd_hist"] > t["ind"]["macd_hist_prev"]) == (t["direction"] == "long")) else "fading"))
seg("MACD hist sign vs trade", lambda t: None if t["ind"].get("macd_hist") is None else ("with" if ((t["ind"]["macd_hist"] > 0) == (t["direction"] == "long")) else "against"))
seg("EMA200 side", lambda t: None if not t["ind"].get("ema200") else ("right side" if ((t["entry"] > t["ind"]["ema200"]) == (t["direction"] == "long")) else "wrong side"))
seg("EMA stack full (20>50>200)", lambda t: None if not t["ind"].get("ema200") else ("full stack" if ((t["ind"]["ema20"] > t["ind"]["ema50"] > t["ind"]["ema200"]) if t["direction"] == "long" else (t["ind"]["ema20"] < t["ind"]["ema50"] < t["ind"]["ema200"])) else "partial"))
seg("daily trend", lambda t: "agrees" if t["daily"] == ("up" if t["direction"] == "long" else "down") else f"daily {t['daily']}")
seg("4h trend", lambda t: "agrees" if t["htf"] == ("up" if t["direction"] == "long" else "down") else f"4h {t['htf']}")
seg("trend age (bars since SuperTrend flip)", lambda t: None if t["ind"].get("supertrend_bars_since_flip") is None else (lambda b: "a <=5" if b <= 5 else "b 6-15" if b <= 15 else "c 16-40" if b <= 40 else "d >40")(t["ind"]["supertrend_bars_since_flip"]))
seg("SuperTrend direction vs trade", lambda t: None if t["ind"].get("supertrend_dir") is None else ("with" if ((t["ind"]["supertrend_dir"] > 0) == (t["direction"] == "long")) else "against"))
seg("channel position (0=bottom,1=top) in trade dir", lambda t: None if t["ind"].get("chan_pos") is None else (lambda p: "a <0.3" if p < 0.3 else "b 0.3-0.7" if p < 0.7 else "c >=0.7")(t["ind"]["chan_pos"] if t["direction"] == "long" else 1 - t["ind"]["chan_pos"]))
seg("vol_atr_ratio", lambda t: None if t["ind"].get("vol_atr_ratio") is None else ("a <0.8" if t["ind"]["vol_atr_ratio"] < 0.8 else "b 0.8-1.2" if t["ind"]["vol_atr_ratio"] < 1.2 else "c >=1.2"))
seg("planned R:R", lambda t: "a <1.8" if t["planned_rr"] < 1.8 else "b 1.8-2.2" if t["planned_rr"] < 2.2 else "c 2.2-3" if t["planned_rr"] < 3 else "d >=3")
seg("structure label", lambda t: f"entry {t['struct']} / macro {t['macro_struct']}")
seg("direction", lambda t: t["direction"])
seg("asset class", lambda t: AC.get(t["symbol"]))
seg("confidence", lambda t: f"{int(t['conf'] * 10) / 10:.1f}")
