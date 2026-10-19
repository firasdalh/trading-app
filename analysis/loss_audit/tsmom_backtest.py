"""Time-series momentum, exactly as pre-registered in tsmom_preregistration.md. No parameter is tuned.

Usage (from backend/):
  python ../analysis/loss_audit/tsmom_backtest.py <daily.pkl> <equity> [holdout comma-list]

Signal: sign of the trailing 252-day return at each month end. Size: 40% annualised ex-ante vol per
instrument (EWMA variance, centre of mass 60 days). Equal weight across active instruments. Hold one
month. Costs: full spread on every traded change in position; overnight swap from the broker's CURRENT
terms on every day held (7 nights per 5 weekdays). Weekends are folded into the Fri->Mon return so
24/7 instruments share the same calendar as everything else.
"""
import datetime as dt
import math
import pickle
import statistics as st
import sys
from collections import defaultdict

LOOKBACK, VOL_TARGET, COM, ANN = 252, 0.40, 60, 261
MIN_ACTIVE = 10            # amendment A1: portfolio sample starts once >= 10 instruments are live
DELTA = COM / (COM + 1.0)
NIGHTS = 7.0 / 5.0          # swap nights charged per weekday held (incl. the triple-swap day)
SWAP_MODES = {0: "disabled", 1: "points", 4: "deposit-ccy/lot", 5: "annual-%-current"}


def weekday_series(rows):
    out = []
    for r in rows:
        d = dt.datetime.fromtimestamp(r[0], dt.timezone.utc).date()
        if d.weekday() < 5 and r[4] > 0:
            out.append((d, r[4], r[6] if len(r) > 6 else 0))
    return out


def swap_frac(spec, side, price):
    """Overnight financing as a SIGNED fraction of notional per night (positive = credit)."""
    sw = spec["swap_long"] if side > 0 else spec["swap_short"]
    mode = spec["swap_mode"]
    if mode == 0 or not sw:
        return 0.0
    if mode == 1:                                   # points per lot per night
        return sw * spec["point"] / price
    if mode == 4:                                   # account-currency money per lot per night
        per_lot = price * (spec["tick_value"] / spec["tick_size"]) if spec["tick_size"] else 0.0
        return sw / per_lot if per_lot else 0.0
    if mode == 5:                                   # annual % of current price
        return sw / 100.0 / 360.0
    return None                                     # unmodelled mode -> reported, never silently zero


def sgn(x):
    return (x > 0) - (x < 0)


def run_symbol(rows, spec, blend=False):
    """{date: (gross, spread_cost, swap)} for one instrument, plus its latest weight and vol.

    Positions are decided at a month-end CLOSE from data up to that close and held from the NEXT bar,
    so nothing here can see the future.
    """
    s = weekday_series(rows)
    n = len(s)
    if n < LOOKBACK + COM + 5:
        return {}, None, None, False
    d = [x[0] for x in s]
    c = [x[1] for x in s]
    sp_pts = [x[2] for x in s]
    mu = var = 0.0
    vol = [None] * n
    for i in range(1, n):
        r = c[i] / c[i - 1] - 1.0
        mu = DELTA * mu + (1 - DELTA) * r
        var = DELTA * var + (1 - DELTA) * (r - mu) ** 2
        if i >= COM:
            vol[i] = math.sqrt(var * ANN)
    month_end = [i for i in range(n - 1) if (d[i].year, d[i].month) != (d[i + 1].year, d[i + 1].month)]
    out = {}
    w_prev, w_last, vol_last, unmodelled = 0.0, None, None, False
    for k, m in enumerate(month_end):
        if m < LOOKBACK or not vol[m]:
            continue
        sig = sgn(c[m] / c[m - LOOKBACK] - 1)
        if blend:
            sig = (sgn(c[m] / c[m - 21] - 1) + sgn(c[m] / c[m - 63] - 1) + sig) / 3.0
        w = sig * VOL_TARGET / vol[m]
        end = month_end[k + 1] if k + 1 < len(month_end) else n - 1
        pts = sp_pts[m] if sp_pts[m] and sp_pts[m] > 0 else spec["spread"]
        spread_cost = abs(w - w_prev) * pts * spec["point"] / c[m]
        for j in range(m + 1, end + 1):
            r = c[j] / c[j - 1] - 1.0
            # Today's swap is a price amount (points) sized for TODAY's price. Applied to history it must
            # be a constant RATE (points / today's price), as the pre-registration says - dividing by the
            # historical price inflated gold's rate ~2.5x (verifier finding, 2026-10-20).
            sf = swap_frac(spec, sgn(w), c[-1]) if w else 0.0
            if sf is None:
                unmodelled, sf = True, 0.0
            out[d[j]] = (w * r, spread_cost if j == m + 1 else 0.0, abs(w) * sf * NIGHTS)
        w_prev, w_last, vol_last = w, w, vol[m]
    return out, w_last, vol_last, unmodelled


def portfolio(results, syms):
    """Equal-weight across the instruments HOLDING a position on each date -> {date: (gross, net_spread,
    net_all, n)}.

    An instrument is live from its first to its last position day. On a day it has no bar (a holiday on
    its exchange) it contributes 0 - its move is folded into its next bar's return - but it still counts
    in the denominator. Dividing by "instruments with a bar today" instead would turn a Christmas Day
    where only BTC trades into a 100%-BTC portfolio.
    """
    span = {s: (min(results[s][0]), max(results[s][0])) for s in syms if results[s][0]}
    days = sorted({d for s in span for d in results[s][0]})
    out = {}
    for day in days:
        live = [s for s, (a, b) in span.items() if a <= day <= b]
        v = [results[s][0].get(day, (0.0, 0.0, 0.0)) for s in live]
        n = len(v)
        out[day] = (sum(x[0] for x in v) / n, sum(x[0] - x[1] for x in v) / n,
                    sum(x[0] - x[1] + x[2] for x in v) / n, n)
    return out


def stats(rets):
    if len(rets) < 60:
        return None
    m, sd = st.mean(rets), st.pstdev(rets)
    if sd == 0:
        return None
    return {"sharpe": m / sd * math.sqrt(ANN), "t": m / sd * math.sqrt(len(rets)),
            "years": len(rets) / ANN, "ann_ret": m * ANN, "ann_vol": sd * math.sqrt(ANN)}


def scaled_path(days, rets, target=0.10):
    """Compounded equity at `target` annual vol (readability only - scaling does not change Sharpe)."""
    k = target / (st.pstdev(rets) * math.sqrt(ANN))
    eq, peak, dd, years = 1.0, 1.0, 0.0, defaultdict(lambda: 1.0)
    for day, r in zip(days, rets):
        eq *= 1 + k * r
        years[day.year] *= 1 + k * r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    return eq, dd, {y: v - 1 for y, v in sorted(years.items())}, k


def fmt(s):
    return "n/a" if not s else f"Sharpe {s['sharpe']:+.2f}  t {s['t']:+.2f}  over {s['years']:.1f}y"


def main():
    path, equity = sys.argv[1], float(sys.argv[2])
    holdout = set(sys.argv[3].split(",")) if len(sys.argv) > 3 and sys.argv[3] else set()
    D = pickle.load(open(path, "rb"))
    bars, spec = D["bars"], D["spec"]
    syms = sorted(bars)
    prim = {s: run_symbol(bars[s], spec[s]) for s in syms}
    blnd = {s: run_symbol(bars[s], spec[s], blend=True) for s in syms}
    active = [s for s in syms if prim[s][0]]
    unmod = [s for s in active if prim[s][3]]

    print(f"instruments with a usable signal: {len(active)} of {len(syms)}")
    span = {s: (min(prim[s][0]), max(prim[s][0])) for s in active}
    print(f"signal history: earliest {min(a for a, b in span.values())}, latest {max(b for a, b in span.values())}")
    if unmod:
        print(f"!! swap mode not modelled (charged 0, results for these are OPTIMISTIC): {unmod}")

    P_all = portfolio(prim, active)
    start = next(d for d in P_all if P_all[d][3] >= MIN_ACTIVE)          # amendment A1
    P = {d: v for d, v in P_all.items() if d >= start}
    days = list(P)
    gross, nspr, nall = ([P[d][i] for d in days] for i in range(3))
    cnt = [P[d][3] for d in days]
    print(f"portfolio sample (>= {MIN_ACTIVE} active, amendment A1): {days[0]} .. {days[-1]}")
    print(f"active instruments per day: min {min(cnt)}, median {int(st.median(cnt))}, max {max(cnt)}")
    print()
    print("PRIMARY RULE (12-month sign, 40% vol, monthly):")
    for lbl, r in (("gross (no costs)", gross), ("net of spread", nspr), ("net of spread + swap", nall)):
        print(f"  {lbl:22} {fmt(stats(r))}")
    spread_drag = [g - n for g, n in zip(gross, nspr)]
    swap_drag = [a - n for a, n in zip(nall, nspr)]
    sd = st.pstdev(gross) * math.sqrt(ANN)
    print(f"  cost drag (info): spread {st.mean(spread_drag) * ANN / sd:.3f} Sharpe/yr, "
          f"swap {st.mean(swap_drag) * ANN / sd:+.3f} Sharpe/yr")
    print(f"  net of 3x spread + swap {fmt(stats([a - 2 * s for a, s in zip(nall, spread_drag)]))}  (info)")
    h = len(days) // 2
    s1, s2 = stats(nall[:h]), stats(nall[h:])
    print(f"  halves (net of all)    1st {days[0]}..{days[h - 1]}: {fmt(s1)}")
    print(f"                         2nd {days[h]}..{days[-1]}: {fmt(s2)}")
    eq, dd, yrs, k = scaled_path(days, nall)
    print(f"  at 10% annual vol: total {eq - 1:+.1%}, max drawdown {dd:.1%}")
    print("  per year: " + "  ".join(f"{y} {v:+.1%}" for y, v in yrs.items()))

    pre = [v[2] for d, v in P_all.items() if d < start]
    if pre:
        lone = sorted({s for s in active if min(prim[s][0]) < start})
        print(f"  footnote, pre-sample ({min(P_all)}..{start}, {', '.join(lone)}): {fmt(stats(pre))}  (not a verdict input)")

    print()
    print("SECONDARY (robustness only - 1/3/12-month blend):")
    PB = {d: v for d, v in portfolio(blnd, [s for s in syms if blnd[s][0]]).items() if d >= start}
    print(f"  net of spread + swap   {fmt(stats([v[2] for v in PB.values()]))}")

    print()
    print("SUBSETS (net of spread + swap):")
    for lbl, group in (("original 33", [s for s in active if s not in holdout]),
                       ("holdout 16", [s for s in active if s in holdout])):
        if group:
            sub = [v[2] for d, v in portfolio(prim, group).items() if d >= start]
            print(f"  {lbl:12} n={len(group):2}  {fmt(stats(sub))}")

    full = stats(nall)
    ok = bool(full and full["t"] > 1.645 and s1 and s2 and s1["sharpe"] > 0 and s2["sharpe"] > 0)
    under = bool(full and full["years"] < 8)
    print()
    verdict = "SUPPORTED" if ok else ("UNDERPOWERED (<8y, not significant)" if under else "REJECTED")
    print(f"VERDICT (pre-registered rule): {verdict}")

    # Implementability: today's target position per instrument at 10% portfolio vol vs the broker's
    # minimum lot. Notional per lot in account currency = price * tick_value / tick_size.
    live = [s for s in active if prim[s][1]]
    print()
    print(f"IMPLEMENTABILITY at equity {equity:,.0f} and 10% portfolio vol (k={k:.3f}, {len(live)} live positions):")
    print(f"  {'symbol':14}{'weight':>8}{'target $':>11}{'min-lot $':>11}{'needs equity':>14}")
    short = []
    for s in live:
        sp, w = spec[s], prim[s][1]
        price = weekday_series(bars[s])[-1][1]
        per_lot = price * sp["tick_value"] / sp["tick_size"] if sp["tick_size"] else float("nan")
        target = equity * k * abs(w) / len(live)
        minlot = sp["volume_min"] * per_lot
        need = minlot * len(live) / (k * abs(w)) if w else float("inf")
        if minlot > target * 1.5:          # rounding up to the min lot would overshoot by >50%
            short.append((s, need))
        print(f"  {s:14}{w:+8.2f}{target:11,.0f}{minlot:11,.0f}{need:14,.0f}")
    print(f"  {len(short)} of {len(live)} instruments cannot be held near their target size at this equity")
    if short:
        print(f"  equity needed for every instrument to be within 1.5x of target: {max(n for _, n in short) / 1.5:,.0f}")
    return prim, active, nall, k, equity, spec


if __name__ == "__main__":
    main()
