"""Follow-ups to evaluate.py's event study, on the same decisions (path-independent, net R):

1. Calibration - does a more confident "go" pick better trades than a less confident one?
2. Free rules - the prompt is a handful of numbers; would a one-line rule on those same numbers
   separate the trades as well as the model does? If yes, the model call buys nothing.

Usage (from backend/):  python ../analysis/ai_reads/extras.py <explore_dir> <labels.jsonl>
"""
import glob
import json
import os
import random
import re
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluate import GO, hybrid_ok, keys, load_labels, says, week  # noqa: E402

NUM = r"(-?[\d.]+(?:e-?\d+)?|None)"


def facts(fork, text):
    """The numbers in a prompt, by name (None where the engine had no value)."""
    f = {}

    def grab(name, pat):
        m = re.search(pat, text)
        f[name] = (None if m is None or m.group(1) == "None" else float(m.group(1)))

    f["long"] = "direction: long" in text
    grab("adx", r"ADX: " + NUM)
    grab("pdi", r"\+DI: " + NUM)
    grab("mdi", r"-DI: " + NUM)
    if fork == "priceaction":
        grab("dist", r"distance from price: " + NUM)
        f["building"] = "toward the level building" in text
        grab("vol", r"Volume trend: " + NUM)
        grab("pos", r"price position " + NUM)
        f["rsi_up"] = "turning up" in text
    elif fork == "momentum":
        m = re.search(r"Higher-TF \(\w+\) trend: (\w+), MACD hist: " + NUM, text)
        f["htf_trend"] = m.group(1) if m else None
        f["htf_macd"] = None if (m is None or m.group(2) == "None") else float(m.group(2))
        f["weakening"] = "magnitude weakening" in text
        grab("ema_dist", r"EMA20 distance: " + NUM)
    elif fork == "regime":
        m1 = re.search(r"EMA stack \(entry TF\): (\S+)", text)
        m2 = re.search(r"EMA stack \(higher TF [^)]*\): (\S+)", text)
        f["stack_entry"], f["stack_htf"] = (m1.group(1) if m1 else "?"), (m2.group(1) if m2 else "?")
        grab("r2", r"fit r2 " + NUM)
        grab("vr", r"baseline\): " + NUM)
    return f


def di_with(f):
    if f["pdi"] is None or f["mdi"] is None:
        return False
    return (f["pdi"] > f["mdi"]) == f["long"]


RULES = {
    "priceaction": {
        "momentum building + volume expanding": lambda f: f["building"] and (f["vol"] or 0) > 0,
        "ADX >= 25 and DI with the trade": lambda f: (f["adx"] or 0) >= 25 and di_with(f),
        "level within 0.5 ATR": lambda f: f["dist"] is not None and f["dist"] <= 0.5,
    },
    "momentum": {
        "higher-TF trend AND MACD with the trade": lambda f: (
            f["htf_trend"] == ("up" if f["long"] else "down") and f["htf_macd"] is not None
            and (f["htf_macd"] > 0) == f["long"]),
        "counter-move weakening": lambda f: f["weakening"],
    },
    "regime": {
        "both EMA stacks lined up": lambda f: f["stack_entry"] == f["stack_htf"] and f["stack_entry"] in (
            "bullish", "bearish"),
        "ADX >= 22": lambda f: (f["adx"] or 0) >= 22,
    },
}

# fork -> (variant whose trade "go" produces, base variant, the label that means go)
STUDIES = {"momentum": ("Vm", "V0", GO["momentum"]), "priceaction": ("Vp", "V0", GO["priceaction"]),
           "regime": ("Tr", "T0", "emerging_trend")}


def line(name, v):
    return f"    {name:42} {st.mean(v) if v else 0:+.3f}R  n={len(v)}"


def gap(ev, pred, n=2000, seed=7):
    """Mean R where ``pred`` holds minus where it doesn't, with a 95% CI that resamples whole
    symbol-weeks: neighbouring bars at the same level are near-duplicates, not independent trades."""
    groups = defaultdict(list)
    for e in ev:
        groups[e[3]].append((e[0], pred(e)))

    def diff(items):
        y = [r for r, p in items if p]
        o = [r for r, p in items if not p]
        return (st.mean(y) - st.mean(o)) if y and o else None

    est = diff([x for g in groups.values() for x in g])
    if est is None:
        return "gap n/a"
    rnd, ks, ds = random.Random(seed), list(groups), []
    for _ in range(n):
        d = diff([x for k in (rnd.choice(ks) for _ in ks) for x in groups[k]])
        if d is not None:
            ds.append(d)
    ds.sort()
    return f"gap {est:+.3f}R [{ds[int(0.025 * len(ds))]:+.2f}, {ds[int(0.975 * len(ds))]:+.2f}]"


def main():
    xdir, lab_path = sys.argv[1], sys.argv[2]
    by_draw, _ = load_labels(lab_path)
    L = by_draw[min(by_draw)]
    ex = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(xdir, "explore_*.json")))]
    trades = {tid: t for e in ex for tid, t in e["trades"].items()}
    texts = {k: v for e in ex for k, v in e["needed"].items()}

    for fork, (var, base, gocat) in STUDIES.items():
        for gate in (False, True):
            ev = []   # (net R of the trade "go" opens, label, prompt facts, (symbol, week), time)
            for e in ex:
                for row in e["rows"]:
                    ks = keys(row, var, fork)
                    o = row["out"]
                    if len(ks) != 1 or o.get(var) == o.get(base) or not o.get(var):
                        continue
                    t = trades[o[var]]
                    if gate and not hybrid_ok(t):
                        continue
                    (k,) = ks
                    if k not in L or k not in texts:
                        continue
                    ev.append((t["net"], L[k], facts(fork, texts[k][1]), (e["symbol"], week(row["ts"])),
                               row["ts"]))
            if not ev:
                continue
            print(f"\n{fork} ({'Hybrid-eligible' if gate else 'all'} trades the 'go' answer would open), n={len(ev)}")
            said = lambda e: says({0: e[1]}, {0}, gocat)  # noqa: E731
            print(line("if always go", [e[0] for e in ev]))
            go = [(e[0], e[1]) for e in ev if said(e)]
            print(line(f"model said {gocat}", [r for r, _ in go]) + "   " + gap(ev, said))
            print(line("model said otherwise", [e[0] for e in ev if not said(e)]))
            print(line("  ... said go at confidence >= 0.80", [r for r, lab in go if lab[1] >= 0.8]))
            print(line("  ... said go at confidence < 0.80", [r for r, lab in go if lab[1] < 0.8]))
            for name, rule in RULES[fork].items():
                yes = [e[0] for e in ev if rule(e[2])]
                no = [e[0] for e in ev if not rule(e[2])]
                print(line(f"rule '{name}'", yes) + f"   | otherwise {st.mean(no) if no else 0:+.3f}R n={len(no)}   "
                      + gap(ev, lambda e: rule(e[2])))
            # Does the separation hold over time, or is it one lucky stretch?
            # Calendar splits, as in evaluate.py: 70% of the time span, and four equal stretches.
            lo, hi = (datetime.fromisoformat(x) for x in (min(e[4] for e in ev), max(e[4] for e in ev)))
            edges = [(lo + (hi - lo) * k / 4).isoformat() for k in range(4)] + ["9999"]
            cut = (lo + (hi - lo) * 0.7).isoformat()
            for name, lo_, hi_ in (("first 70%", "", cut), ("last 30%", cut, "9999"),
                                   *((f"fold {k + 1}", edges[k], edges[k + 1]) for k in range(4))):
                part = [e for e in ev if lo_ <= e[4] < hi_]
                y = [e[0] for e in part if said(e)]
                o = [e[0] for e in part if not said(e)]
                print(f"    {name:10} said go {st.mean(y) if y else 0:+.3f}R n={len(y):4} | "
                      f"otherwise {st.mean(o) if o else 0:+.3f}R n={len(o):4}")


if __name__ == "__main__":
    main()
