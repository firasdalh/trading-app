"""The pre-registered test of PREREG.md: do the two candidate entry conditions pick better trades?

For each hypothesis, over every Hybrid-eligible decision of the unchanged live engine (path-independent,
net R of the trade it would open): mean with the condition minus mean without it, and a one-sided p from
a bootstrap that resamples whole symbol-weeks (neighbouring bars are near-duplicates, not independent
trades). Then, for the record only, the path-dependent backtest of live vs live-with-the-condition.

Usage:  python holdout_test.py <replay_dir> <bars.pkl>
"""
import glob
import json
import os
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluate import HYBRID_MIN, always, fresh, path, stats, week, within, zero  # noqa: E402

HYPOTHESES = {
    "H1 MACD cross with the trade within 6 bars": lambda t: fresh(t, 6),
    "H2 supply/demand bounce within 3 bars": lambda t: within(t, "zone_since", 3),
}


def gap_test(events, n=5000, seed=11):
    """events: (net, has_condition, cluster). Returns (n_with, mean_with, n_without, mean_without, gap, p)."""
    groups = defaultdict(list)
    for net, yes, key in events:
        groups[key].append((net, yes))

    def gap(items):
        y = [r for r, c in items if c]
        o = [r for r, c in items if not c]
        return (st.mean(y) - st.mean(o)) if y and o else None

    flat = [x for g in groups.values() for x in g]
    est = gap(flat)
    keys, rnd, below = list(groups), random.Random(seed), 0
    done = 0
    for _ in range(n):
        g = gap([x for k in (rnd.choice(keys) for _ in keys) for x in groups[k]])
        if g is None:
            continue
        done += 1
        below += g <= 0
    y = [r for r, c in flat if c]
    o = [r for r, c in flat if not c]
    return len(y), st.mean(y) if y else 0.0, len(o), st.mean(o) if o else 0.0, est, (below + 1) / (done + 1)


def main():
    xdir = sys.argv[1]
    ex = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(xdir, "replay_*.json")))]
    trades = {tid: t for e in ex for tid, t in e["trades"].items()}
    rows = {e["symbol"]: sorted(e["rows"], key=lambda r: r["i"]) for e in ex}
    ts = sorted(r["ts"] for e in ex for r in e["rows"])
    lo, hi = datetime.fromisoformat(ts[0]), datetime.fromisoformat(ts[-1])
    cut = (lo + (hi - lo) * 0.7).isoformat()
    edges = [(lo + (hi - lo) * k / 4).isoformat() for k in range(4)] + ["9999"]
    print(f"{len(ex)} symbols, {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}")

    live = [trades[r["out"]["B"]] for rs in rows.values() for r in rs
            if r["out"].get("B") and trades[r["out"]["B"]]["conf"] > HYBRID_MIN]
    print(f"Hybrid-eligible live decisions: {len(live)}, mean {st.mean(t['net'] for t in live):+.3f}R")
    for name, cond in HYPOTHESES.items():
        ev = [(t["net"], cond(t), (t["symbol"], week(t["entry_time"]))) for t in live]
        ny, my, no, mo, g, p = gap_test(ev)
        print(f"\n{name}\n  with: {my:+.3f}R (n={ny})   without: {mo:+.3f}R (n={no})   "
              f"gap {g:+.3f}R, one-sided p = {p:.4f}")
        for label, part in (("first 70%", [e for e, t in zip(ev, live) if t["entry_time"] < cut]),
                            ("last 30%", [e for e, t in zip(ev, live) if t["entry_time"] >= cut])):
            y = [e[0] for e in part if e[1]]
            o = [e[0] for e in part if not e[1]]
            if y and o:
                print(f"  {label}: with {st.mean(y):+.3f}R (n={len(y)}), without {st.mean(o):+.3f}R (n={len(o)})")
        for cfg, keep in (("live", always), ("live + condition required", cond)):
            v = [t for sym in rows for t in path(rows[sym], trades, "B", zero, keep, True)]
            s = stats(v, cut, edges)
            if s["n"]:
                print(f"  path, {cfg:26} n={s['n']:4} {s['exp']:+.3f}R/trade total {s['tot']:+6.1f}R "
                      f"DD {s['dd']:4.1f}R  folds {' '.join(f'{x:+.1f}' for x in s['fold_tot'])}")


if __name__ == "__main__":
    main()
