"""Pass 3: score each AI read against the deterministic engine, from explore.py's variants + label.py's answers.

A non-"go" answer always ends in no market entry and a "go" answer continues exactly like the forced
variant, so each configuration's decision at a bar is rebuilt from the variants without re-running the
engine. Trades are then replayed path-dependently (one position per symbol, 3-bar cooldown), net of the
symbol's spread. "Hybrid" = only setups Hybrid could open (confidence > 0.68, never a range fade), with
the gate inside the path so a skipped setup doesn't block the next one.

Usage (from backend/):  python ../analysis/ai_reads/evaluate.py <explore_dir> <labels.jsonl> <bars.pkl>
"""
import glob
import json
import os
import pickle
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime

COOLDOWN = 3
HYBRID_MIN = 0.68
TH = 0.55   # _MOM_AI_MIN_CONF / _REGIME_AI_MIN_CONF / _PA_AI_MIN_CONF
GO = {"momentum": "healthy_pullback", "priceaction": "likely_break"}

CONFIGS = {   # name: (label, base config it's compared with)
    "base": ("Deterministic only, trend-only OFF", None),
    "momentum": ("+ AI momentum-read", "base"),
    "priceaction": ("+ AI price-action-read", "base"),
    "regime": ("+ AI regime-read", "base"),
    "all": ("+ all three", "base"),
    "T_base": ("Deterministic only, trend-only ON", None),
    "T_regime": ("trend-only ON + AI regime-read", "T_base"),
    "T_all": ("trend-only ON + all three (as set now)", "T_base"),
}


def keys(row, variant, fork):
    return {fk.split(":")[1] for fk in row["seen"].get(variant, []) if fk.startswith(fork + ":")}


def says(L, ks, cat):
    """Every key answered ``cat`` with at least the engine's minimum confidence."""
    return all((lab := L.get(k)) is not None and lab[0] == cat and (lab[1] or 0) >= TH for k in ks)


def decide(cfg, row, L):
    o = row["out"]
    if cfg == "base":
        return o.get("V0")
    if cfg == "T_base":
        return o.get("T0")
    if cfg == "momentum":
        mk = keys(row, "Vm", "momentum")
        return o.get("V0") if not mk else (o.get("Vm") if says(L, mk, GO["momentum"]) else None)
    if cfg == "priceaction":
        pk = keys(row, "Vp", "priceaction")
        return o.get("V0") if not pk else (o.get("Vp") if says(L, pk, GO["priceaction"]) else None)
    if cfg == "regime":
        rk = keys(row, "Vr", "regime")
        return o.get("Vr") if rk and says(L, rk, "choppy_range") else o.get("V0")
    if cfg == "T_regime":
        rk = keys(row, "Tr", "regime")
        return o.get("Tr") if rk and says(L, rk, "emerging_trend") else o.get("T0")
    if cfg in ("all", "T_all"):
        if cfg == "all":
            rk, v = keys(row, "Vr", "regime"), "Vmp"
            if rk and says(L, rk, "choppy_range"):
                return o.get("Vr")
        else:
            rk, v = keys(row, "Tmp", "regime"), "Tmp"
            if rk and not says(L, rk, "emerging_trend"):
                return o.get("T0")
        mk, pk = keys(row, v, "momentum"), keys(row, v, "priceaction")
        if (mk and not says(L, mk, GO["momentum"])) or (pk and not says(L, pk, GO["priceaction"])):
            return None
        return o.get(v)
    raise ValueError(cfg)


def hybrid_ok(t):
    return t["conf"] > HYBRID_MIN and t["strategy"] != "mean_reversion"


def path(sym_rows, trades, cfg, L, gate):
    out, block = [], -1
    for row in sym_rows:
        if row["i"] <= block:
            continue
        tid = decide(cfg, row, L)
        if not tid:
            continue
        t = trades[tid]
        if gate and not hybrid_ok(t):
            continue
        out.append(dict(t, tid=tid, ai=tid != (row["out"].get("T0" if cfg.startswith("T_") else "V0"))))
        block = row["i"] + t["bars_held"] + COOLDOWN
    return out


def stats(v, cut, edges):
    if not v:
        return dict(n=0)
    v = sorted(v, key=lambda t: t["entry_time"])
    rs = [t["net"] for t in v]
    w = [r for r in rs if r > 0]
    lo = [r for r in rs if r <= 0]
    eq = pk = dd = 0.0
    for r in rs:
        eq += r
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    ins = [t["net"] for t in v if t["entry_time"] < cut]
    oos = [t["net"] for t in v if t["entry_time"] >= cut]
    folds = [[t["net"] for t in v if edges[k] <= t["entry_time"] < edges[k + 1]] for k in range(4)]
    return dict(n=len(rs), win=len(w) / len(rs), exp=st.mean(rs), tot=sum(rs), dd=dd,
                pf=(sum(w) / -sum(lo)) if lo and sum(lo) < 0 else 99.0,
                ins=st.mean(ins) if ins else 0.0, oos=st.mean(oos) if oos else 0.0,
                folds=[st.mean(f) if f else 0.0 for f in folds])


def fmt(s):
    if not s.get("n"):
        return "n=0"
    f = " ".join(f"{x:+.2f}" for x in s["folds"])
    return (f"n={s['n']:5} win={100 * s['win']:3.0f}% exp={s['exp']:+.3f}R PF={s['pf']:.2f} "
            f"tot={s['tot']:+7.1f}R DD={s['dd']:5.1f}R | IS {s['ins']:+.3f} OOS {s['oos']:+.3f} | folds {f}")


def week(t):
    y, w, _ = datetime.fromisoformat(t).isocalendar()
    return f"{y}-{w}"


def boot_ci(groups, n=2000, seed=7):
    """95% CI of a mean over (key -> list of values), resampling whole groups (symbol-weeks)."""
    keys_ = list(groups)
    if len(keys_) < 5:
        return None
    rnd = random.Random(seed)
    means = []
    for _ in range(n):
        vals = [x for k in (rnd.choice(keys_) for _ in keys_) for x in groups[k]]
        if vals:
            means.append(st.mean(vals))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def load_labels(paths):
    """Answers by draw, from one or more comma-separated label files."""
    by_draw = defaultdict(dict)
    meta = defaultdict(dict)
    for path in paths.split(","):
        for line in open(path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            by_draw[r["draw"]][r["key"]] = (r["category"], r["confidence"], r["evidence"])
            meta[r["key"]]["fork"] = r["fork"]
            meta[r["key"]].setdefault("tok", []).append((r["tin"], r["tout"]))
    return by_draw, meta


def main():
    xdir, lab_path, bars = sys.argv[1], sys.argv[2], sys.argv[3]
    data = pickle.load(open(bars, "rb"))
    watch = set(data["watchlist"])
    ex = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(xdir, "explore_*.json")))]
    trades = {tid: t for e in ex for tid, t in e["trades"].items()}
    rows = {e["symbol"]: sorted(e["rows"], key=lambda r: r["i"]) for e in ex}
    by_draw, meta = load_labels(lab_path)
    draws = sorted(by_draw)

    ts = sorted(r["ts"] for e in ex for r in e["rows"])
    lo, hi = datetime.fromisoformat(ts[0]), datetime.fromisoformat(ts[-1])
    cut = (lo + (hi - lo) * 0.7).isoformat()
    edges = [(lo + (hi - lo) * k / 4).isoformat() for k in range(4)] + ["9999"]
    print(f"{len(ex)} symbols, {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}; out-of-sample from {cut[:10]}; "
          f"labels: draws {draws}, {len(by_draw[draws[0]])} decisions")

    # ---- 1. how the model answers, and how often a second identical ask disagrees ----
    print("\n=== 1. Answers and repeatability (same prompt asked twice) ===")
    for fork in ("momentum", "regime", "priceaction"):
        ks = [k for k, m in meta.items() if m["fork"] == fork]
        if not ks:
            continue
        L1 = by_draw[draws[0]]
        ks1 = [k for k in ks if k in L1]
        dist = Counter((L1[k][0] if L1[k][1] is not None and L1[k][1] >= TH else "below 0.55/none")
                       for k in ks1)
        line = f"{fork:12} n={len(ks1):5}  " + ", ".join(f"{c} {100 * v / len(ks1):.0f}%" for c, v in dist.most_common())
        if len(draws) > 1:
            both = [k for k in ks if k in by_draw[1] and k in by_draw[2]]
            cat_flip = sum(by_draw[1][k][0] != by_draw[2][k][0] for k in both)
            gocat = GO.get(fork)
            gos = {"regime": ("choppy_range", "emerging_trend")}.get(fork, (gocat,))
            dec_flip = sum(any(says(by_draw[1], {k}, g) != says(by_draw[2], {k}, g) for g in gos) for k in both)
            line += f"\n{'':12} asked twice: label differs {100 * cat_flip / max(1, len(both)):.0f}%, " \
                    f"trade decision differs {100 * dec_flip / max(1, len(both)):.0f}% (n={len(both)})"
        print(line)
    tin = sum(a for m in meta.values() for a, _ in m["tok"])
    tout = sum(b for m in meta.values() for _, b in m["tok"])
    print(f"tokens: {tin:,} in / {tout:,} out  ->  ${tin / 1e6 * 0.25 + tout / 1e6 * 2.0:.2f}")

    # ---- 2. path-dependent backtests ----
    for gate, title in ((True, "Hybrid (conf > 0.68, no range fades)"), (False, "All signals (Run analysis)")):
        for universe, syms in (("all 33 symbols", list(rows)), ("your watchlist (19)", [s for s in rows if s in watch])):
            print(f"\n=== 2. Backtest — {title} — {universe} ===")
            for cfg, (label, base) in CONFIGS.items():
                for d in (draws if base else draws[:1]):
                    v = [t for s in syms for t in path(rows[s], trades, cfg, by_draw[d], gate)]
                    s_ = stats(v, cut, edges)
                    tag = f" [draw {d}]" if base and len(draws) > 1 else ""
                    print(f"  {label + tag:46} {fmt(s_)}")
                    if base:
                        ai = [t for t in v if t["ai"]]
                        if ai:
                            print(f"  {'   of which the AI changed:':46} {fmt(stats(ai, cut, edges))}")

    # ---- 3. event study: does the label pick the better trades? (every decision, not one path) ----
    print("\n=== 3. Does the answer separate good trades from bad? (all decisions, path-independent) ===")
    studies = (("momentum", "Vm", "V0", "momentum", GO["momentum"]),
               ("priceaction", "Vp", "V0", "priceaction", GO["priceaction"]),
               ("regime choppy (trend-only OFF)", "Vr", "V0", "regime", "choppy_range"),
               ("regime emerging (trend-only ON)", "Tr", "T0", "regime", "emerging_trend"))
    civ = lambda c: f"[{c[0]:+.2f},{c[1]:+.2f}]" if c else ""  # noqa: E731
    for name, var, base, fork, gocat in studies:
        for gate in (False, True):
            for d in draws:
                grp = {True: defaultdict(list), False: defaultdict(list)}
                for s, rs in rows.items():
                    for row in rs:
                        ks = keys(row, var, fork)
                        o = row["out"]
                        if not ks or o.get(var) == o.get(base) or not all(k in by_draw[d] for k in ks):
                            continue
                        tg, tb = trades.get(o.get(var)), trades.get(o.get(base))
                        if gate:
                            tg = tg if tg and hybrid_ok(tg) else None
                            tb = tb if tb and hybrid_ok(tb) else None
                        if tg is None and tb is None:
                            continue
                        # What answering "go" changes at this decision, in R (no trade = 0).
                        delta = (tg["net"] if tg else 0.0) - (tb["net"] if tb else 0.0)
                        grp[says(by_draw[d], ks, gocat)][(s, week(row["ts"]))].append(delta)
                yes = [x for g in grp[True].values() for x in g]
                no = [x for g in grp[False].values() for x in g]
                if not yes and not no:
                    continue
                print(f"  {name:32} {'Hybrid' if gate else 'all':6} d{d} if always go: "
                      f"{st.mean(yes + no):+.3f}R n={len(yes + no):5} | AI said {gocat}: "
                      f"{st.mean(yes) if yes else 0:+.3f}R n={len(yes):5} {civ(boot_ci(grp[True]))} | "
                      f"otherwise: {st.mean(no) if no else 0:+.3f}R n={len(no):5} {civ(boot_ci(grp[False]))}")


if __name__ == "__main__":
    main()
