"""Merge explore.py's per-symbol outputs into the decisions worth asking the model about.

A decision is kept only if its answer can change a trade in some configuration evaluate.py scores: a
momentum / price-action answer when its "go" variant ends in a trade, a regime answer when choppy (or
emerging, with trend-only on) leads somewhere different from the deterministic outcome.

Usage:  python collect.py <explore_dir> <needed.json> [--symbols A,B]
"""
import argparse
import glob
import json
import os
from collections import Counter


def keys(row, variant, fork):
    return {fk.split(":")[1] for fk in row["seen"].get(variant, []) if fk.startswith(fork + ":")}


def matters(row) -> set:
    o, need = row["out"], set()
    for v in ("Vm", "Vmp", "Tmp"):
        if o.get(v):
            need |= keys(row, v, "momentum")
    for v in ("Vp", "Vmp", "Tmp"):
        if o.get(v):
            need |= keys(row, v, "priceaction")
    rk = keys(row, "Vr", "regime") | keys(row, "Tr", "regime") | keys(row, "Tmp", "regime")
    if rk and (o.get("Vr") != o.get("V0") or o.get("Vr") != o.get("Vmp")
               or o.get("Tr") != o.get("T0") or o.get("Tmp") != o.get("T0")):
        need |= rk
    return need


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("explore_dir")
    ap.add_argument("out")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    only = {s for s in a.symbols.split(",") if s}

    needed, per_sym = {}, Counter()
    for f in sorted(glob.glob(os.path.join(a.explore_dir, "explore_*.json"))):
        e = json.load(open(f))
        if only and e["symbol"] not in only:
            continue
        keep = set().union(*(matters(r) for r in e["rows"])) if e["rows"] else set()
        needed.update({k: e["needed"][k] for k in keep})
        per_sym[e["symbol"]] = len(keep)
    json.dump(needed, open(a.out, "w"))
    print(f"{len(needed)} decisions from {len(per_sym)} symbols:", dict(Counter(v[0] for v in needed.values())))
    print("per symbol:", dict(per_sym.most_common()))
