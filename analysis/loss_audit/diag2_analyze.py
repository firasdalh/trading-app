"""Test candidate entry filters and exit rules on bt_diag2 output (fixed engine, Hybrid gate, net of spread).

A candidate is only interesting if it moves expectancy the same way in BOTH halves (IS/OOS) and in most of
the 4 time folds — anything else is noise we'd be fitting.
"""
import json
import pickle
import statistics as st
import sys
from collections import defaultdict

S, F = sys.argv[1], sys.argv[2]
spec = pickle.load(open(S + "/bars.pkl", "rb"))["spec"]
AC = pickle.load(open(S + "/bars.pkl", "rb"))["asset_class"]
tr = json.load(open(F))
tr.sort(key=lambda t: t["entry_time"])


def cost(t):
    sp = spec.get(t["symbol"], {})
    px = (sp.get("spread") or 0) * (sp.get("point") or 0)
    return max(px / abs(t["entry"] - t["stop"]), 0.01)


tr = [t for t in tr if t["strategy"] == "trend" and t["conf"] > 0.67]
for t in tr:
    t["cost"] = cost(t)
    t["net"] = t["r"] - t["cost"]
ts = [t["entry_time"] for t in tr]
cut = ts[int(len(ts) * 0.7)]
edges = [ts[int(len(ts) * k / 4)] for k in range(4)] + ["9999"]


def summ(vals_times):
    """vals_times: list of (net_r, entry_time)."""
    if not vals_times:
        return "n=0"
    v = [x for x, _ in vals_times]
    i = [x for x, t in vals_times if t < cut]
    o = [x for x, t in vals_times if t >= cut]
    folds = []
    for k in range(4):
        f = [x for x, t in vals_times if edges[k] <= t < edges[k + 1]]
        folds.append(f"{st.mean(f):+.2f}" if f else "  -  ")
    return (f"n={len(v):4} exp={st.mean(v):+.3f} tot={sum(v):+6.1f} | IS {st.mean(i) if i else 0:+.3f} "
            f"OOS {st.mean(o) if o else 0:+.3f} | folds {' '.join(folds)}")


def seg(title, fn):
    g = defaultdict(list)
    for t in tr:
        k = fn(t)
        if k is not None:
            g[k].append((t["net"], t["entry_time"]))
    print(f"\n-- {title}")
    for k in sorted(g, key=str):
        print(f"   {str(k):26} {summ(g[k])}")


print(f"BASE (fixed engine, trend, Hybrid gate): {summ([(t['net'], t['entry_time']) for t in tr])}")

L = lambda t: t["direction"] == "long"  # noqa: E731
# ---------------- entry ideas ----------------
seg("A1 today's range already used (x daily ATR)", lambda t: None if not t["day"] else
    ("a <0.5" if t["day"]["range_used"] < 0.5 else "b 0.5-0.8" if t["day"]["range_used"] < 0.8 else
     "c 0.8-1.1" if t["day"]["range_used"] < 1.1 else "d >=1.1"))
seg("A2 today's move from the open in trade dir (x daily ATR)", lambda t: None if not t["day"] else
    ("a <-0.2 (against)" if t["day"]["move_from_open"] < -0.2 else "b -0.2..0.3" if t["day"]["move_from_open"] < 0.3 else
     "c 0.3-0.7" if t["day"]["move_from_open"] < 0.7 else "d >=0.7 (chased)"))
seg("A3 position in today's range (1 = at the extreme in trade dir)", lambda t: None if not t["day"] else
    ("a <0.4 (dip)" if t["day"]["pos_in_range"] < 0.4 else "b 0.4-0.75" if t["day"]["pos_in_range"] < 0.75 else "c >=0.75 (at extreme)"))


def st_age(d, long_):
    if not d.get("supertrend_dir"):
        return None
    if (d["supertrend_dir"] > 0) != long_:
        return "against"
    a = d.get("supertrend_bars_since_flip")
    return "old (no flip)" if a is None else a


def age_bucket(a, cuts):
    if a is None or isinstance(a, str):
        return a
    for lim, name in cuts:
        if a <= lim:
            return name
    return cuts[-1][1] if False else f"> {cuts[-1][0]}"


seg("B1 4h SuperTrend age (with trade)", lambda t: age_bucket(st_age(t["h4"], L(t)), [(6, "a <=6"), (18, "b 7-18"), (42, "c 19-42")]))
seg("B2 daily SuperTrend age (with trade)", lambda t: age_bucket(st_age(t["d1"], L(t)), [(5, "a <=5"), (15, "b 6-15"), (40, "c 16-40")]))


def rsi_dir(d, long_):
    r = d.get("rsi14")
    return None if r is None else (r if long_ else 100 - r)


seg("B3 4h RSI in trade dir", lambda t: (lambda r: None if r is None else "a <50" if r < 50 else "b 50-60" if r < 60 else "c 60-70" if r < 70 else "d >=70")(rsi_dir(t["h4"], L(t))))
seg("B4 daily RSI in trade dir", lambda t: (lambda r: None if r is None else "a <50" if r < 50 else "b 50-60" if r < 60 else "c 60-70" if r < 70 else "d >=70")(rsi_dir(t["d1"], L(t))))


def ext(d, t):
    if not d.get("ema20") or not d.get("atr14"):
        return None
    return (t["entry"] - d["ema20"]) / d["atr14"] * (1 if L(t) else -1)


seg("B5 distance above 4h EMA20 in trade dir (4h ATR)", lambda t: (lambda x: None if x is None else "a <0" if x < 0 else "b 0-1" if x < 1 else "c 1-2" if x < 2 else "d >=2")(ext(t["h4"], t)))
seg("B6 distance above daily EMA20 in trade dir (daily ATR)", lambda t: (lambda x: None if x is None else "a <0" if x < 0 else "b 0-1" if x < 1 else "c 1-2" if x < 2 else "d >=2")(ext(t["d1"], t)))
seg("B7 daily ADX", lambda t: None if t["d1"].get("adx") is None else ("a <20" if t["d1"]["adx"] < 20 else "b 20-30" if t["d1"]["adx"] < 30 else "c 30-40" if t["d1"]["adx"] < 40 else "d >=40"))
seg("B8 4h ADX", lambda t: None if t["h4"].get("adx") is None else ("a <20" if t["h4"]["adx"] < 20 else "b 20-30" if t["h4"]["adx"] < 30 else "c 30-40" if t["h4"]["adx"] < 40 else "d >=40"))
seg("C1 entry-TF RSI in trade dir", lambda t: (lambda r: None if r is None else "a <50" if r < 50 else "b 50-58" if r < 58 else "c 58-66" if r < 66 else "d >=66")(rsi_dir(t["ind"], L(t))))
seg("C2 entry-TF RSI turning with the trade", lambda t: None if t["ind"].get("rsi14_prev") is None else ("turning with" if ((t["ind"]["rsi14"] > t["ind"]["rsi14_prev"]) == L(t)) else "turning against"))
seg("C3 bb_width (entry TF, abs)", lambda t: None if t["ind"].get("bb_width") is None else ("a tight" if t["ind"]["bb_width"] < 0.004 else "b mid" if t["ind"]["bb_width"] < 0.01 else "c wide"))
seg("C4 vs prior day's extreme (breakout above PDH for a long)", lambda t: None if not t["d1"].get("prior_day_high") else (
    ("above PDH" if t["entry"] > t["d1"]["prior_day_high"] else "below PDH") if L(t) else ("below PDL" if t["entry"] < t["d1"]["prior_day_low"] else "above PDL")))
seg("C5 vs prior week's extreme", lambda t: None if not t["d1"].get("prior_week_high") else (
    ("above PWH" if t["entry"] > t["d1"]["prior_week_high"] else "inside") if L(t) else ("below PWL" if t["entry"] < t["d1"]["prior_week_low"] else "inside")))
seg("C6 entry TF trend age (<=24 by construction)", lambda t: None if not isinstance(st_age(t["ind"], L(t)), float) else ("a <=4" if st_age(t["ind"], L(t)) <= 4 else "b 5-12" if st_age(t["ind"], L(t)) <= 12 else "c 13-24"))
seg("C7 asset class", lambda t: AC[t["symbol"]])
seg("C8 direction", lambda t: t["direction"])


# ---------------- exit rules ----------------
def replay(t, *, target=None, time_n=None, time_min=0.0, partial=None, be_after_partial=False,
           trail_k=None, trail_start=1.0, max_bars=96):
    """Exit-rule replay on the recorded path. Returns net R (units of planned risk)."""
    tgt = t["planned_rr"] if target is None else target
    s = -1.0
    banked = 0.0
    frac = 1.0
    peak = 0.0
    atr_r = t["atr_r"] or 0.0
    path = t["fwd"][:max_bars]
    for j, (fav, adv, close) in enumerate(path, start=1):
        if -adv <= s:                                   # stop first on an ambiguous bar
            return banked + frac * s - t["cost"]
        if partial and frac == 1.0 and fav >= partial[0]:
            banked += partial[1] * partial[0]
            frac = 1.0 - partial[1]
            if be_after_partial:
                s = max(s, 0.0)
        if tgt is not None and fav >= tgt:
            return banked + frac * tgt - t["cost"]
        peak = max(peak, fav)
        if trail_k is not None and peak >= trail_start and atr_r:
            s = max(s, peak - trail_k * atr_r)
        if time_n is not None and j == time_n and close < time_min:
            return banked + frac * close - t["cost"]
    last = path[-1][2] if path else 0.0
    return banked + frac * last - t["cost"]


def ex(title, **kw):
    print(f"   {title:44} {summ([(replay(t, **kw), t['entry_time']) for t in tr])}")


print("\n== EXIT RULES (replayed on each trade's real path) ==")
ex("plan (engine target, fixed stop)")
for tg in (1.5, 2.0, 3.0):
    ex(f"fixed target {tg}R", target=tg)
for n in (3, 6, 12, 24):
    for m in (0.0, 0.3):
        ex(f"time stop: bar {n} close < {m}R -> exit", time_n=n, time_min=m)
ex("partial 50% at 1R, runner to plan", partial=(1.0, 0.5))
ex("partial 50% at 1R + breakeven", partial=(1.0, 0.5), be_after_partial=True)
ex("partial 33% at 1.5R, runner to plan", partial=(1.5, 1 / 3))
for k in (1.5, 2.5, 3.5):
    for start in (1.0, 2.0):
        ex(f"ATR trail {k}x after +{start}R, target=plan", trail_k=k, trail_start=start)
        ex(f"ATR trail {k}x after +{start}R, NO target", trail_k=k, trail_start=start, target=99)
