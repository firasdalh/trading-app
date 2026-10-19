"""Compare a backtest variant against a baseline: gated expectancy, folds, OOS, and WHICH trades moved.

Usage (from backend/):
    python ../analysis/loss_audit/compare_variants.py <dir-with-bars.pkl> baseline.json [variant.json ...]

Costs each trade at the REAL spread of its own entry bar (bars.pkl must be fetched with the 7-tuple
per-bar spread - see fetch_data.py). Gate = the Hybrid confidence bar (trend > 0.67, MR > 0.60).
"""
import datetime as dt
import json
import random
import pickle
import statistics as st
import sys

D, files = sys.argv[1], sys.argv[2:]
P = pickle.load(open(D + "/bars.pkl", "rb"))
spec = P["spec"]
bs = {(s, int(r[0])): r[6] for (s, tf), rows in P["bars"].items() for r in rows if len(r) > 6}


def load(f):
    tr = json.load(open(f))
    for t in tr:
        ts = int(dt.datetime.fromisoformat(t["entry_time"]).timestamp())
        sp = bs.get((t["symbol"], ts)) or bs.get((t["symbol"], ts - ts % 3600)) \
            or (spec.get(t["symbol"], {}).get("spread") or 0)
        px = sp * (spec.get(t["symbol"], {}).get("point") or 0)
        t["net"] = t["r"] - max(px / abs(t["entry"] - t["stop"]), 0.01)
    return sorted(tr, key=lambda x: x["entry_time"])


def gate(t):
    return t["conf"] > (0.60 if t["strategy"] == "mean_reversion" else 0.67)


def stats(v):
    if not v:
        return "n=0"
    r = [x["net"] for x in v]
    w, lo = [x for x in r if x > 0], [x for x in r if x <= 0]
    pf = (sum(w) / -sum(lo)) if lo and sum(lo) < 0 else 99
    eq = pk = dd = 0.0
    for x in r:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
    return (f"n={len(r):5} exp={st.mean(r):+.3f}R PF={pf:4.2f} tot={sum(r):+7.1f}R "
            f"win={100 * len(w) / len(r):4.1f}% maxDD={dd:5.1f}R")


def report(name, tr):
    g = [t for t in tr if gate(t)]
    trend = [t for t in g if t["strategy"] == "trend"]
    n = len(trend)
    folds = [trend[i * n // 4:(i + 1) * n // 4] for i in range(4)]
    c = int(n * 0.7)
    print(f"== {name}")
    print(f"   gated {stats(g)}")
    print(f"   trend {stats(trend)}")
    print("   folds " + "  ".join(f"{st.mean([x['net'] for x in q]):+.3f}" if q else "  n/a " for q in folds))
    if n:
        print(f"   IS {st.mean([x['net'] for x in trend[:c]]):+.3f}   OOS {st.mean([x['net'] for x in trend[c:]]):+.3f}")
    return trend


key = lambda t: (t["symbol"], t["entry_time"])
base = report("BASELINE  " + files[0].split("/")[-1].split("\\")[-1], load(files[0]))
bk = {key(t): t for t in base}
for f in files[1:]:
    var = report("VARIANT   " + f.split("/")[-1].split("\\")[-1], load(f))
    vk = {key(t): t for t in var}
    removed = [bk[k] for k in bk.keys() - vk.keys()]
    added = [vk[k] for k in vk.keys() - bk.keys()]
    retgt = [(bk[k], vk[k]) for k in bk.keys() & vk.keys() if abs(bk[k]["target"] - vk[k]["target"]) > 1e-9]
    print(f"   trades the variant REMOVED (priced as the baseline took them): {stats(removed)}")
    print(f"   trades the variant ADDED  (path-dependence)                  : {stats(added)}")

    # THE TEST FOR A FILTER: were the trades it removed worse than the ones it kept? Path totals
    # are dominated by reshuffling (the 2026-10-14 research measured a 15-30R noise floor), so
    # this compares the two groups directly, with a one-sided permutation p-value.
    if removed:
        kept = [bk[k] for k in bk.keys() & vk.keys()]
        rm, kp = [t["net"] for t in removed], [t["net"] for t in kept]
        obs = st.mean(kp) - st.mean(rm)
        pool, nr, hits = rm + kp, len(rm), 0
        rng = random.Random(7)
        for _ in range(5000):
            rng.shuffle(pool)
            if st.mean(pool[nr:]) - st.mean(pool[:nr]) >= obs:
                hits += 1
        print(f"   FILTER TEST: kept {st.mean(kp):+.3f}R vs removed {st.mean(rm):+.3f}R "
              f"(diff {obs:+.3f}R)  permutation p={hits / 5000:.3f}  "
              f"-> {'removed trades WERE worse' if obs > 0 else 'removed trades were BETTER - the filter hurts'}")

    # THE TEST FOR A TARGET CHANGE: same entry, different exit. Paired, so path effects cancel.
    if retgt:
        d = [x[1]["net"] - x[0]["net"] for x in retgt]
        rng = random.Random(11)
        boots = sorted(st.mean(rng.choices(d, k=len(d))) for _ in range(5000))
        lo, hi = boots[int(0.025 * 5000)], boots[int(0.975 * 5000)]
        b = sum(x[0]["net"] for x in retgt); v = sum(x[1]["net"] for x in retgt)
        print(f"   TARGET TEST (same entry): n={len(retgt)}  baseline {b:+.1f}R -> variant {v:+.1f}R  "
              f"mean paired diff {st.mean(d):+.3f}R  95% CI [{lo:+.3f}, {hi:+.3f}]  "
              f"-> {'CI excludes 0' if lo > 0 or hi < 0 else 'CI includes 0: not distinguishable from noise'}")
