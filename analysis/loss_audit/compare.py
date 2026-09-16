"""Before/after comparison of two bt_diag runs on the same data (net of per-symbol spread)."""
import json
import pickle
import statistics as st
import sys
from collections import defaultdict

S, A, B = sys.argv[1], sys.argv[2], sys.argv[3]
spec = pickle.load(open(S + "/bars.pkl", "rb"))["spec"]
AC = pickle.load(open(S + "/bars.pkl", "rb"))["asset_class"]
WATCH = {"XAUUSDm", "JP225m", "USOILm", "USTECm", "HK50m", "BTCUSDm", "XNGUSDm", "DE30m", "AUS200m",
         "US500_x100m", "FR40m", "STOXX50m", "UK100m", "AUDUSDm"}


def load(f):
    tr = json.load(open(f))
    for t in tr:
        sp = spec.get(t["symbol"], {})
        px = (sp.get("spread") or 0) * (sp.get("point") or 0)
        t["net"] = t["r"] - max(px / abs(t["entry"] - t["stop"]), 0.01)
    return sorted(tr, key=lambda t: t["entry_time"])


def gate(tr):
    return [t for t in tr if (t["conf"] > 0.60 if t["strategy"] == "mean_reversion" else t["conf"] > 0.67)]


a, b = load(A), load(B)
ts = sorted(t["entry_time"] for t in a)
edges = [ts[int(len(ts) * i / 4)] for i in range(4)] + ["9999"]
cut = ts[int(len(ts) * 0.7)]


def stats(v):
    if not v:
        return "n=0"
    rs = [t["net"] for t in v]
    w = [r for r in rs if r > 0]
    lo = [r for r in rs if r <= 0]
    pf = sum(w) / -sum(lo) if lo and sum(lo) < 0 else 99
    eq = pk = dd = 0.0
    for r in rs:
        eq += r
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    i = [t["net"] for t in v if t["entry_time"] < cut]
    o = [t["net"] for t in v if t["entry_time"] >= cut]
    folds = " ".join(f"{st.mean([t['net'] for t in v if edges[k] <= t['entry_time'] < edges[k + 1]] or [0]):+.2f}"
                     for k in range(4))
    return (f"n={len(rs):4} win={100 * len(w) / len(rs):3.0f}% exp={st.mean(rs):+.3f} PF={pf:.2f} tot={sum(rs):+6.1f}R "
            f"DD={dd:5.1f}R | IS {st.mean(i) if i else 0:+.3f} OOS {st.mean(o) if o else 0:+.3f} | folds {folds}")


for label, fn in (("ALL signals", lambda x: x), ("HYBRID gate", gate),
                  ("HYBRID gate, 14-pair watchlist", lambda x: [t for t in gate(x) if t["symbol"] in WATCH]),
                  ("HYBRID gate, since 2026-07-08", lambda x: [t for t in gate(x) if t["entry_time"] >= "2026-07-08"])):
    print(label)
    print("  before:", stats(fn(a)))
    print("  after :", stats(fn(b)))
print("\nHYBRID gate by asset class (before -> after):")
for c in sorted(set(AC.values())):
    va = [t for t in gate(a) if AC[t["symbol"]] == c]
    vb = [t for t in gate(b) if AC[t["symbol"]] == c]
    ea = st.mean(t["net"] for t in va) if va else 0
    eb = st.mean(t["net"] for t in vb) if vb else 0
    print(f"  {c:7} {ea:+.3f} (n={len(va)}) -> {eb:+.3f} (n={len(vb)})")
print("\nHYBRID gate by symbol (before -> after), sorted by after:")
rows = []
for s in AC:
    va = [t for t in gate(a) if t["symbol"] == s]
    vb = [t for t in gate(b) if t["symbol"] == s]
    if vb:
        o = [t["net"] for t in vb if t["entry_time"] >= cut]
        i = [t["net"] for t in vb if t["entry_time"] < cut]
        rows.append((st.mean(t["net"] for t in vb), s, st.mean(t["net"] for t in va) if va else 0, len(va), len(vb),
                     st.mean(i) if i else 0, st.mean(o) if o else 0))
improved = 0
for eb, s, ea, na, nb, i, o in sorted(rows, reverse=True):
    improved += eb > ea
    print(f"  {'*' if s in WATCH else ' '}{s:13} {ea:+.3f} (n={na:3}) -> {eb:+.3f} (n={nb:3})  after IS {i:+.2f} OOS {o:+.2f}")
print(f"symbols improved: {improved}/{len(rows)}")
g = defaultdict(list)
for t in gate(b):
    g[t["strategy"]].append(t)
print("\nafter, HYBRID gate by strategy:", {k: f"{st.mean(x['net'] for x in v):+.3f}(n={len(v)})" for k, v in g.items()})
