"""Task 7 — Walk-forward / out-of-sample validation of the deterministic funnel.

Two questions:
  1) Are the funnel's parameters CURVE-FIT to history? We sweep the main regime knob (the ADX
     'trending' threshold, currently 25) and check (a) whether performance is a smooth PLATEAU around
     25 rather than a fragile spike, and (b) a proper WALK-FORWARD: on each in-sample window pick the
     best threshold, then score THAT choice on the next unseen window — if the best-IS threshold jumps
     around or its OOS edge collapses, that's overfitting.
  2) Does the CURRENT config (ADX=25) hold out-of-sample? A chronological IS/OOS holdout split.

Efficiency: the expensive step (run_technical per bar) is done ONCE per symbol and cached; the ADX
sweep only re-runs the cheap decision + trade simulation, monkeypatching orchestrator._ADX_STRONG.

Real broker data via settings.broker_map. Run with uvicorn stopped:
    PYTHONPATH=backend python analysis/walk_forward.py
"""
from __future__ import annotations

import bisect
import sys
from pathlib import Path

from sqlalchemy import select

import app.agents.orchestrator as orch
from app.agents.pipeline import _timeframes_for
from app.agents.technical import run_technical
from app.backtest.simulator import (
    _WINDOW, _neutral_fundamental, _simulate_trade, compute_metrics, split_by_time, time_folds,
)
from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass
from app.models.schemas import OHLCVSeries

BARS = 1500
CONTEXT_BARS = 600
MAX_HOLD = 96
COOLDOWN = 3
COST_R = 0.05
THRESHOLDS = [20.0, 22.0, 25.0, 28.0, 30.0]
DEFAULT = 25.0
K_FOLDS = 4
MIN_IS = 12          # need at least this many IS trades to trust an IS "optimum"
HOLDOUT = 0.30
OUT = Path(__file__).with_name("walk_forward.md")


def _entry_ind(res, tf) -> dict:
    if not res or not res.timeframes:
        return {}
    prim = next((x for x in res.timeframes if x.timeframe == tf), res.timeframes[0])
    return prim.indicators or {}


def _techs_for(broker, symbol, tf):
    """run_technical per bar, cached once. Returns (entry_candles, techs list aligned to bars)."""
    tfs = _timeframes_for(tf)
    series = {}
    for t in tfs:
        limit = BARS if t == tf else CONTEXT_BARS
        try:
            sd = broker.get_ohlcv(symbol, t, limit=limit)
            series[t] = list(sd.candles) if sd and sd.candles else []
        except Exception:  # noqa: BLE001
            series[t] = []
    entry = series.get(tf, [])
    if len(entry) < _WINDOW + 5:
        return [], []
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    n = len(entry)
    techs = [None] * n
    for i in range(_WINDOW - 1, n):
        t_i = entry[i].ts
        window, ok = [], True
        for t in tfs:
            if t == tf:
                w = entry[max(0, i - _WINDOW + 1): i + 1]
            else:
                hi = bisect.bisect_right(ts_index[t], t_i)
                w = series[t][max(0, hi - _WINDOW): hi]
            if not w:
                ok = False; break
            window.append(OHLCVSeries(symbol=symbol, timeframe=t, candles=w))
        if ok:
            techs[i] = run_technical(symbol, window, use_llm=False)
    return entry, techs


def _trades_at_threshold(symbol, ac, tf, entry, techs, thr):
    """Cheap pass: re-run the decision at ADX threshold `thr` over cached techs, simulate trades."""
    orch._ADX_STRONG = thr
    fund = _neutral_fundamental(symbol)
    n = len(entry)
    trades = []
    i = _WINDOW - 1
    while i < n:
        res = techs[i]
        if res is None:
            i += 1; continue
        prop = orch._deterministic_decision(symbol, ac, tf, res, fund, now=entry[i].ts, trend_only=True)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1; continue
        t = _simulate_trade(symbol, entry, i, prop, max_hold=MAX_HOLD, cost_r=COST_R)
        if t is None:
            i += 1; continue
        trades.append(t)
        i = i + max(1, t.bars_held) + COOLDOWN
    return trades


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    items = s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    syms = [(it.symbol, it.asset_class, it.timeframe) for it in items]
    s.close()
    if not syms:
        print("no watchlist symbols"); return

    by_thr = {t: [] for t in THRESHOLDS}
    for sym, ac, tf in syms:
        try:
            broker = get_broker_for(AssetClass(ac), bmap)
            entry, techs = _techs_for(broker, sym, tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}"); continue
        if not entry:
            print(f"  {sym}: no data"); continue
        counts = []
        for thr in THRESHOLDS:
            tr = _trades_at_threshold(sym, AssetClass(ac), tf, entry, techs, thr)
            by_thr[thr] += tr
            counts.append(f"adx{int(thr)}={len(tr)}")
        print(f"  {sym}: " + " ".join(counts)); sys.stdout.flush()
    orch._ADX_STRONG = DEFAULT   # restore

    # Common calendar range so folds align across thresholds.
    all_times = [tr.entry_time for t in THRESHOLDS for tr in by_thr[t]]
    lo, hi = min(all_times), max(all_times)

    lines = ["# Task 7 — Walk-forward / out-of-sample validation", ""]

    # --- (0) parameter provenance ---
    lines += ["## 0. Parameter provenance (are these fitted or conventional?)", ""]
    lines += [
        "- **ADX 25 / 20**, **EMA 20/50/200**, **RSI 75/25**, **R:R 2.0 (cap 4.0)** are all textbook / "
        "round-number defaults (Wilder's ADX bands, standard EMA stack, classic RSI extremes) — not "
        "values that look grid-searched to a dataset.",
        "- The few non-standard numbers (mean-reversion RSI **66/34**, value-zone **1.0xATR**, chase "
        "penalty **2.5xATR**) carry an explicit written rationale in the code, not a fitted precision "
        "(e.g. 23.7). So the PRIOR is: convention-chosen, low curve-fit risk. The tests below check "
        "that empirically.", ""]

    # --- (1) ADX threshold sensitivity (full sample) ---
    lines += ["## 1. ADX-threshold sensitivity (full sample)", "",
              "A robust knob shows a smooth plateau; a fitted one shows a lonely spike at the chosen value.",
              "", "| ADX thr | trades | win% | expectancy R | profit factor | maxDD R |",
              "|---|---|---|---|---|---|"]
    for thr in THRESHOLDS:
        m = compute_metrics(by_thr[thr])
        star = " **<- current**" if thr == DEFAULT else ""
        pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        lines.append(f"| {int(thr)}{star} | {m.n} | {m.win_rate*100:.0f}% | {m.expectancy_r:+.3f} | "
                     f"{pf} | {m.max_dd_r:.1f} |")

    # --- (2) walk-forward: pick best threshold on IS, score it on the next unseen fold ---
    folds_by_thr = {t: time_folds(by_thr[t], K_FOLDS, lo=lo, hi=hi) for t in THRESHOLDS}
    lines += ["", f"## 2. Walk-forward ({K_FOLDS} time folds): best-IS threshold -> next OOS fold", "",
              "| test fold | best-IS thr | IS exp R | OOS exp R (best-IS) | OOS exp R (default 25) |",
              "|---|---|---|---|---|"]
    picks = []
    for k in range(1, K_FOLDS):
        # IS = folds [0..k-1] pooled; OOS = fold k.
        best_thr, best_is = None, None
        for thr in THRESHOLDS:
            is_trades = [tr for f in folds_by_thr[thr][:k] for tr in f]
            if len(is_trades) < MIN_IS:
                continue
            e = compute_metrics(is_trades).expectancy_r
            if best_is is None or e > best_is:
                best_is, best_thr = e, thr
        if best_thr is None:
            lines.append(f"| {k+1} | (insufficient IS) | - | - | - |"); continue
        oos_best = compute_metrics(folds_by_thr[best_thr][k])
        oos_def = compute_metrics(folds_by_thr[DEFAULT][k])
        picks.append(best_thr)
        lines.append(f"| {k+1} | {int(best_thr)} | {best_is:+.3f} | "
                     f"{oos_best.expectancy_r:+.3f} (n={oos_best.n}) | "
                     f"{oos_def.expectancy_r:+.3f} (n={oos_def.n}) |")

    # --- (3) current-config holdout ---
    is_trades, oos_trades = split_by_time(by_thr[DEFAULT], HOLDOUT)
    mi, mo = compute_metrics(is_trades), compute_metrics(oos_trades)
    lines += ["", f"## 3. Current config (ADX=25) holdout: last {int(HOLDOUT*100)}% of time held out", "",
              "| window | trades | win% | expectancy R | profit factor |",
              "|---|---|---|---|---|",
              f"| in-sample | {mi.n} | {mi.win_rate*100:.0f}% | {mi.expectancy_r:+.3f} | "
              f"{'inf' if mi.profit_factor==float('inf') else f'{mi.profit_factor:.2f}'} |",
              f"| OUT-OF-SAMPLE | {mo.n} | {mo.win_rate*100:.0f}% | {mo.expectancy_r:+.3f} | "
              f"{'inf' if mo.profit_factor==float('inf') else f'{mo.profit_factor:.2f}'} |"]

    # --- verdict ---
    stable = len(set(picks)) <= 2                                   # best-IS threshold doesn't thrash
    oos_holds = mo.expectancy_r > 0 and mo.expectancy_r >= 0.4 * mi.expectancy_r if mi.expectancy_r > 0 else mo.expectancy_r > 0
    default_near_best = True
    full = {t: compute_metrics(by_thr[t]).expectancy_r for t in THRESHOLDS}
    best_full = max(full, key=full.get)
    default_near_best = abs(full[DEFAULT] - full[best_full]) <= 0.05
    lines += ["", "## Verdict", ""]
    lines.append(f"- **Threshold stability across walk-forward folds:** best-IS threshold = "
                 f"{[int(p) for p in picks] or 'n/a'} -> "
                 + ("**stable** (little curve-fit risk on this knob)." if stable else
                    "**unstable** — the 'optimal' threshold jumps between windows, a curve-fitting warning."))
    lines.append(f"- **Is the default (25) near the full-sample optimum?** best full-sample threshold "
                 f"= {int(best_full)} (exp {full[best_full]:+.3f}); default 25 = {full[DEFAULT]:+.3f} -> "
                 + ("yes, 25 sits on the plateau." if default_near_best else
                    "no — 25 is off the empirical optimum (consider, but do NOT auto-tune)."))
    lines.append(f"- **Does the current config hold out-of-sample?** OOS expectancy {mo.expectancy_r:+.3f} "
                 f"vs IS {mi.expectancy_r:+.3f} -> "
                 + ("**holds** (edge persists into unseen data)." if oos_holds else
                    "**decays** out-of-sample — treat the backtest edge with caution."))
    lines += ["",
              "_Caveats: single knob swept (ADX threshold) — the strongest fitting risk, but not the "
              "only parameter; small per-fold samples make fold-level expectancy noisy; deterministic "
              "engine only (no AI review); costs = flat 0.05R. Walk-forward here VALIDATES robustness — "
              "it does not, and should not, auto-tune the live value._"]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
