"""Pre-registered test (see ob_path_preregistration.md): do baseline trades with an opposing order block
1.5R-4R in their path do worse?

Annotates each gated trend trade of a BASELINE run directly - rebuilding the exact entry-timeframe
window the engine saw (same slicing as bt.py) - so no second backtest is needed and the condition is
evaluated with only information available at the decision bar.

Usage (from backend/):  python ../analysis/loss_audit/ob_path_test.py <dir-with-bars.pkl> <baseline.json>
"""
import bisect
import os
import random
import statistics as st
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("LOG_LEVEL", "WARNING")
import logging  # noqa: E402

logging.disable(logging.WARNING)

from app.agents.order_blocks import find_order_blocks, opposing_block  # noqa: E402
from app.backtest.simulator import _WINDOW  # noqa: E402

sys.argv = [sys.argv[0], *sys.argv[1:]]
D, BASE = sys.argv[1], sys.argv[2]
_src = open(os.path.join(os.path.dirname(__file__), "compare_variants.py"), encoding="utf-8").read()
_argv = sys.argv
sys.argv = ["x", D, BASE]
exec(_src[:_src.index("key = lambda t")])   # load(), gate(), stats() and the per-bar spread costing
sys.argv = _argv

import bt  # noqa: E402  (same candle loader the backtest used: drops the forming bar)

BARS = 6500
cache = {}


def window_for(sym, entry_time):
    if sym not in cache:
        full = bt.candles(D + "/bars.pkl", sym, "1h")
        entry = full[-BARS:]
        cache[sym] = (entry, [c.ts.isoformat() for c in entry])
    entry, keys = cache[sym]
    i = bisect.bisect_left(keys, entry_time)
    if i >= len(keys) or keys[i] != entry_time:
        return None
    return entry[max(0, i - _WINDOW + 1): i + 1]


def in_path(t):
    w = window_for(t["symbol"], t["entry_time"])
    if not w or len(w) < 60:
        return None
    risk = abs(t["entry"] - t["stop"])
    if risk <= 0:
        return None
    ob = opposing_block(find_order_blocks(w, max_blocks=1000), t["direction"], t["entry"], min_strength=55)
    if ob is None:
        return False
    edge = ob.bottom if t["direction"] == "long" else ob.top
    return 1.5 <= abs(edge - t["entry"]) / risk <= 4.0


trades = [t for t in load(BASE) if gate(t) and t["strategy"] == "trend"]
flag = [(t, in_path(t)) for t in trades]
flag = [(t, f) for t, f in flag if f is not None]
w = [t["net"] for t, f in flag if f]
wo = [t["net"] for t, f in flag if not f]
print(f"annotated {len(flag)} of {len(trades)} gated trend trades")
print(f"  WITH an opposing block 1.5R-4R ahead : n={len(w):4}  {st.mean(w) if w else float('nan'):+.3f}R")
print(f"  WITHOUT                              : n={len(wo):4}  {st.mean(wo) if wo else float('nan'):+.3f}R")
if w and wo:
    obs = st.mean(wo) - st.mean(w)
    pool, nw, hits, rng = w + wo, len(w), 0, random.Random(2026)
    for _ in range(10000):
        rng.shuffle(pool)
        if st.mean(pool[nw:]) - st.mean(pool[:nw]) >= obs:
            hits += 1
    p = hits / 10000
    print(f"  gap {obs:+.3f}R   one-sided permutation p = {p:.4f}")
    print("  VERDICT (pre-registered rule):",
          "SUPPORTED" if obs > 0 and p < 0.05 else "REJECTED")
