"""Pass 2 of the MACD test: score the MACD gates, the MACD confidence points and a MACD-cross factor from
replay.py's output. No engine runs: a confidence change is applied to the recorded confidence, and a
removed gate is read from the replay variant that skips it.

Trades are replayed path-dependently (one position per symbol, 3-bar cooldown), net of spread. "Hybrid"
= confidence > 0.68 with the gate applied inside the path, as in analysis/ai_reads/evaluate.py.

Rule fixed before the results were read: a change is worth making only if, in the Hybrid view, it beats
live on total R over all 33 symbols, out-of-sample (last 30%) and in at least 3 of the 4 time folds, does
not lose on the watchlist, and its neighbouring settings (k, bonus) agree.

Usage:  python evaluate.py <replay_dir> <bars.pkl>
"""
import glob
import json
import os
import pickle
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

COOLDOWN = 3
HYBRID_MIN = 0.68
RISING = (0.05, -0.06)   # orchestrator: +0.05 when the histogram moves the trade's way, -0.06 otherwise
D1_PENALTY = 0.10        # orchestrator: daily MACD histogram against the trade


def fresh(t, k):
    """The histogram crossed to the trade's side within the last k bars (0 = on the decision bar)."""
    return t["h"] == 1 and t["since"] is not None and t["since"] <= k


def within(t, key, k):
    """A recorded 'bars since' event (zone_since / pw_break / pw_bounce) happened within k bars."""
    v = t.get(key)
    return v is not None and v <= k


def zone_near(t, d):
    v = t.get("zone_near")
    return v is not None and v <= d


def bonus(test, b):
    return lambda t: b if test(t) else 0.0


def no_rising(t):
    if t["strategy"] != "trend" or t["rising"] is None:
        return 0.0
    return -(RISING[0] if t["rising"] else RISING[1])


def no_d1(t):
    return D1_PENALTY if t["strategy"] == "trend" and t["d1"] == -1 else 0.0


def zero(t):
    return 0.0


def always(t):
    return True


def configs():
    """(name, replay variant, confidence change, keep-the-trade test)."""
    out = [
        ("live", "B", zero, always),
        ("no MACD-histogram gate", "NG", zero, always),
        ("no gate, no flat-momentum guard", "NGF", zero, always),
        ("live without the 'histogram rising' points", "B", no_rising, always),
        ("live without the daily-MACD penalty", "B", no_d1, always),
        ("live without any MACD confidence points", "B", lambda t: no_rising(t) + no_d1(t), always),
    ]
    for k in (1, 3, 6, 12):
        for b in (0.05, 0.10):
            out.append((f"live + {b:.2f} for a cross within {k} bars", "B",
                        lambda t, k=k, b=b: b if fresh(t, k) else 0.0, always))
    for k in (1, 3, 6, 12):
        out.append((f"live, cross within {k} bars required", "B", zero, lambda t, k=k: fresh(t, k)))
    for k in (3, 6):
        for b in (0.05, 0.10):
            out.append((f"no gate + {b:.2f} for a cross within {k} bars", "NG",
                        lambda t, k=k, b=b: b if fresh(t, k) else 0.0, always))
    for k in (3, 6):
        out.append((f"no gate, cross within {k} bars required", "NG", zero, lambda t, k=k: fresh(t, k)))
    # Supply/demand and last week's high/low (only when replay.py recorded them).
    for b in (0.05, 0.10):
        for k in (3, 6, 12):
            out.append((f"live + {b:.2f} for a zone bounce within {k} bars", "B",
                        bonus(lambda t, k=k: within(t, "zone_since", k), b), always))
        for d in (0.5, 1.0):
            out.append((f"live + {b:.2f} within {d} ATR of a zone", "B",
                        bonus(lambda t, d=d: zone_near(t, d), b), always))
        for k in (3, 12, 999):
            out.append((f"live + {b:.2f} for a last-week break within {'this week' if k == 999 else f'{k} bars'}",
                        "B", bonus(lambda t, k=k: within(t, "pw_break", k), b), always))
        for k in (3, 6, 12):
            out.append((f"live + {b:.2f} for a last-week bounce within {k} bars", "B",
                        bonus(lambda t, k=k: within(t, "pw_bounce", k), b), always))
    for k in (3, 6, 12):
        out.append((f"live, zone bounce within {k} bars required", "B", zero,
                    lambda t, k=k: within(t, "zone_since", k)))
    out.append(("live, last-week break this week required", "B", zero, lambda t: within(t, "pw_break", 999)))
    out.append(("live, last-week bounce within 12 bars required", "B", zero,
                lambda t: within(t, "pw_bounce", 12)))
    return out


def conf_of(t, adj):
    return round(max(0.05, min(0.95, t["conf"] + adj(t))), 2)


def path(rows, trades, variant, adj, keep, gate):
    out, block = [], -1
    for row in rows:
        if row["i"] <= block:
            continue
        tid = row["out"].get(variant)
        if not tid:
            continue
        t = trades[tid]
        if gate and not (conf_of(t, adj) > HYBRID_MIN and t["strategy"] != "mean_reversion"):
            continue
        if not keep(t):
            continue
        out.append(t)
        block = row["i"] + t["bars_held"] + COOLDOWN
    return out


def stats(v, cut, edges):
    if not v:
        return dict(n=0, tot=0.0, oos_tot=0.0, fold_tot=[0.0] * 4)
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
                ins=st.mean(ins) if ins else 0.0, oos=st.mean(oos) if oos else 0.0, oos_tot=sum(oos),
                fold_tot=[sum(f) for f in folds])


def fmt(s):
    if not s["n"]:
        return "n=0"
    f = " ".join(f"{x:+6.1f}" for x in s["fold_tot"])
    return (f"n={s['n']:5} win={100 * s['win']:3.0f}% exp={s['exp']:+.3f}R PF={s['pf']:.2f} "
            f"tot={s['tot']:+7.1f}R DD={s['dd']:5.1f}R | OOS {s['oos']:+.3f} ({s['oos_tot']:+.1f}R) | "
            f"fold totals {f}")


def week(ts):
    y, w, _ = datetime.fromisoformat(ts).isocalendar()
    return f"{y}-{w}"


def boot_ci(groups, n=2000, seed=7):
    """95% CI of a mean over (key -> values), resampling whole groups (symbol-weeks)."""
    ks = list(groups)
    if len(ks) < 5:
        return None
    rnd, means = random.Random(seed), []
    for _ in range(n):
        vals = [x for k in (rnd.choice(ks) for _ in ks) for x in groups[k]]
        if vals:
            means.append(st.mean(vals))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def describe(name, events, cut):
    """events: (net, symbol, ts). Mean with a symbol-week CI, plus the in/out-of-sample split."""
    if not events:
        return f"  {name:44} n=0"
    g = defaultdict(list)
    for net, sym, ts in events:
        g[(sym, week(ts))].append(net)
    ci = boot_ci(g)
    ins = [e[0] for e in events if e[2] < cut]
    oos = [e[0] for e in events if e[2] >= cut]
    civ = f"[{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else ""
    return (f"  {name:44} {st.mean(e[0] for e in events):+.3f}R n={len(events):5} {civ:15} | "
            f"first 70% {st.mean(ins) if ins else 0:+.3f} (n={len(ins)}) last 30% "
            f"{st.mean(oos) if oos else 0:+.3f} (n={len(oos)})")


def main():
    xdir, bars = sys.argv[1], sys.argv[2]
    watch = set(pickle.load(open(bars, "rb"))["watchlist"])
    ex = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(xdir, "replay_*.json")))]
    trades = {tid: t for e in ex for tid, t in e["trades"].items()}
    rows = {e["symbol"]: sorted(e["rows"], key=lambda r: r["i"]) for e in ex}

    ts = sorted(r["ts"] for e in ex for r in e["rows"])
    lo, hi = datetime.fromisoformat(ts[0]), datetime.fromisoformat(ts[-1])
    cut = (lo + (hi - lo) * 0.7).isoformat()
    edges = [(lo + (hi - lo) * k / 4).isoformat() for k in range(4)] + ["9999"]
    print(f"{len(ex)} symbols, {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}; out-of-sample from {cut[:10]}")

    # ---- 1. path-dependent backtests ----
    for gate, title in ((True, "Hybrid (conf > 0.68)"), (False, "All signals (Run analysis)")):
        for universe, syms in (("all 33 symbols", list(rows)), ("your watchlist (19)", [s for s in rows if s in watch])):
            print(f"\n=== Backtest — {title} — {universe} ===")
            base = None
            for name, var, adj, keep in configs():
                if not gate and adj is not zero and keep is always:
                    continue          # confidence changes only matter through the Hybrid gate
                s = stats([t for sym in syms for t in path(rows[sym], trades, var, adj, keep, gate)], cut, edges)
                if base is None:
                    base = s
                    print(f"  {name:46} {fmt(s)}")
                    continue
                better = sum(a > b for a, b in zip(s["fold_tot"], base["fold_tot"]))
                print(f"  {name:46} {fmt(s)} | vs live {s['tot'] - base['tot']:+6.1f}R, "
                      f"OOS {s['oos_tot'] - base['oos_tot']:+5.1f}R, folds better {better}/4")

    # ---- 2. what the gates block, and what MACD says about the trades live takes (path-independent) ----
    for gate, title in ((True, "Hybrid-eligible"), (False, "all signals")):
        print(f"\n=== Decisions ({title}): every bar, net R of the trade it would open ===")
        ok = (lambda t: t["conf"] > HYBRID_MIN) if gate else (lambda t: True)
        live, blocked, flat = [], [], []
        for sym, rs in rows.items():
            for row in rs:
                o = row["out"]
                if o.get("B") and ok(trades[o["B"]]):
                    live.append(trades[o["B"]])
                if o.get("NG") and not o.get("B") and ok(trades[o["NG"]]):
                    blocked.append(trades[o["NG"]])
                if o.get("NGF") and not o.get("NG") and ok(trades[o["NGF"]]):
                    flat.append(trades[o["NGF"]])
        ev = lambda v: [(t["net"], t["symbol"], t["entry_time"]) for t in v]  # noqa: E731
        print(describe("trades live takes", ev(live), cut))
        print(describe("trades the histogram gate blocks", ev(blocked), cut))
        print(describe("trades the flat-momentum guard blocks", ev(flat), cut))
        print("  -- live trades, by the 1h MACD state --")
        for name, f in (
                ("cross with the trade, 0-1 bars ago", lambda t: t["h"] == 1 and t["since"] is not None and t["since"] <= 1),
                ("cross with the trade, 2-3 bars ago", lambda t: t["h"] == 1 and t["since"] is not None and 2 <= t["since"] <= 3),
                ("cross with the trade, 4-6 bars ago", lambda t: t["h"] == 1 and t["since"] is not None and 4 <= t["since"] <= 6),
                ("cross with the trade, 7-12 bars ago", lambda t: t["h"] == 1 and t["since"] is not None and 7 <= t["since"] <= 12),
                ("cross with the trade, 13+ bars ago", lambda t: t["h"] == 1 and (t["since"] is None or t["since"] > 12)),
                ("histogram slightly against (< 10% ATR)", lambda t: t["h"] == -1),
                ("histogram rising the trade's way", lambda t: t["strategy"] == "trend" and t["rising"] is True),
                ("histogram not rising", lambda t: t["strategy"] == "trend" and t["rising"] is False),
                ("MACD line on the trade's side of zero", lambda t: t["line"] == 1),
                ("MACD line on the other side of zero", lambda t: t["line"] == -1),
                ("4h histogram with the trade", lambda t: t["h4"] == 1),
                ("4h histogram against", lambda t: t["h4"] == -1),
                ("daily histogram with the trade", lambda t: t["d1"] == 1),
                ("daily histogram against", lambda t: t["d1"] == -1)):
            print(describe(name, ev([t for t in live if f(t)]), cut))
        if "zone_since" not in (live[0] if live else {}):
            continue
        print("  -- live trades, by supply/demand (demand under a long, supply over a short) --")
        z, p = (lambda t: t["zone_since"]), (lambda t: t["pw_break"])
        for name, f in (
                ("bounced off a zone 0-3 bars ago", lambda t: z(t) is not None and z(t) <= 3),
                ("bounced off a zone 4-6 bars ago", lambda t: z(t) is not None and 4 <= z(t) <= 6),
                ("bounced off a zone 7-12 bars ago", lambda t: z(t) is not None and 7 <= z(t) <= 12),
                ("no zone bounce in 12 bars", lambda t: z(t) is None),
                ("inside a zone", lambda t: t["zone_near"] == 0),
                ("0-0.5 ATR from a zone", lambda t: t["zone_near"] is not None and 0 < t["zone_near"] <= 0.5),
                ("0.5-1 ATR from a zone", lambda t: t["zone_near"] is not None and 0.5 < t["zone_near"] <= 1),
                ("1-2 ATR from a zone", lambda t: t["zone_near"] is not None and 1 < t["zone_near"] <= 2),
                ("over 2 ATR from a zone", lambda t: t["zone_near"] is not None and t["zone_near"] > 2),
                ("no zone on the trade's side", lambda t: t["zone_near"] is None),
                ("opposing zone < 1.5 ATR ahead", lambda t: t["room"] is not None and t["room"] < 1.5),
                ("opposing zone 1.5-3 ATR ahead", lambda t: t["room"] is not None and 1.5 <= t["room"] <= 3),
                ("opposing zone > 3 ATR ahead / none", lambda t: t["room"] is None or t["room"] > 3)):
            print(describe(name, ev([t for t in live if f(t)]), cut))
        print("  -- live trades, by last week's high/low --")
        for name, f in (
                ("broke last week's level 0-3 bars ago", lambda t: p(t) is not None and p(t) <= 3),
                ("broke it 4-12 bars ago", lambda t: p(t) is not None and 4 <= p(t) <= 12),
                ("broke it 13+ bars ago (this week)", lambda t: p(t) is not None and p(t) > 12),
                ("not beyond last week's level", lambda t: p(t) is None),
                ("bounced off last week's high/low 0-3 bars ago", lambda t: within(t, "pw_bounce", 3)),
                ("bounced off it 4-12 bars ago", lambda t: t["pw_bounce"] is not None and 4 <= t["pw_bounce"] <= 12),
                ("no bounce off it in 12 bars", lambda t: t["pw_bounce"] is None)):
            print(describe(name, ev([t for t in live if f(t)]), cut))


if __name__ == "__main__":
    main()
