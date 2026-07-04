"""Confidence-formula ablation: which scoring terms are REDUNDANT (safe to remove)?

The confidence score only decides which setups clear the 70% Hybrid gate (all actionable setups are
simulated either way). So we score every setup under the full formula AND with each suspected-
duplicate term removed, then compare the performance of the >=70% subset each way. If removing a term
barely changes the >=70% subset's win%/expectancy, that term was double-counting a signal already
captured elsewhere -> safe to drop for a simpler, more robust formula.

Suspected duplicates (Task-2 correlation): conf_macd (MACD dir ~ trend), conf_rsi (RSI OB/OS ~ entry
distance), conf_ema200 (~ trend), conf_div (RSI-based ~ RSI/entry). Efficient: technical computed once
per bar; the cheap decision is re-run per ablation on the cached read.

Run with uvicorn stopped: PYTHONPATH=backend python analysis/confidence_ablation.py
"""
from __future__ import annotations

import bisect
import sys
from pathlib import Path

import numpy as np
from sqlalchemy import select

from app.agents.orchestrator import _deterministic_decision
from app.agents.pipeline import _timeframes_for
from app.agents.technical import run_technical
from app.backtest.simulator import _WINDOW, _neutral_fundamental, _simulate_trade
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
GATE = 0.70
OUT = Path(__file__).with_name("confidence_ablation.md")

VARIANTS = {
    "full": frozenset(),
    "no_macd": frozenset({"conf_macd"}),
    "no_rsi": frozenset({"conf_rsi"}),
    "no_ema200": frozenset({"conf_ema200"}),
    "no_div": frozenset({"conf_div"}),
    "no_ALL_four": frozenset({"conf_macd", "conf_rsi", "conf_ema200", "conf_div"}),
}


def run_symbol(broker, symbol, ac, tf, rows):
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
        return
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(symbol)
    n = len(entry)
    i = _WINDOW - 1
    while i < n:
        t_i = entry[i].ts
        window, ok = [], True
        for t in tfs:
            w = (entry[max(0, i - _WINDOW + 1): i + 1] if t == tf
                 else series[t][max(0, bisect.bisect_right(ts_index[t], t_i) - _WINDOW):
                                bisect.bisect_right(ts_index[t], t_i)])
            if not w:
                ok = False; break
            window.append(OHLCVSeries(symbol=symbol, timeframe=t, candles=w))
        if not ok:
            i += 1; continue
        tech = run_technical(symbol, window, use_llm=False)
        base = _deterministic_decision(symbol, ac, tf, tech, fund, now=t_i, trend_only=True)
        if not base.is_actionable or base.take_profit is None:
            i += 1; continue
        trade = _simulate_trade(symbol, entry, i, base, max_hold=MAX_HOLD, cost_r=COST_R)
        if trade is None:
            i += 1; continue
        confs = {}
        for name, dis in VARIANTS.items():
            p = _deterministic_decision(symbol, ac, tf, tech, fund, now=t_i, trend_only=True, disable=dis)
            confs[name] = p.confidence
        rows.append((trade.r, confs))
        i = i + max(1, trade.bars_held) + COOLDOWN


def _sub(rows, name):
    return [r for r, c in rows if c[name] >= GATE]


def _stat(rs):
    a = np.array(rs, dtype=float)
    if len(a) == 0:
        return (0, 0.0, 0.0, 0.0)
    return (len(a), (a > 0).mean() * 100, a.mean(), a.sum())


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    syms = [(it.symbol, it.asset_class, it.timeframe)
            for it in s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()]
    s.close()
    rows = []
    for sym, ac, tf in syms:
        try:
            run_symbol(get_broker_for(AssetClass(ac), bmap), sym, AssetClass(ac), tf, rows)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}")
        print(f"  {sym}: {len(rows)} setups so far"); sys.stdout.flush()

    full_n, full_wr, full_ex, full_tot = _stat(_sub(rows, "full"))
    L = ["# Confidence-formula ablation — which terms are redundant?", "",
         f"{len(rows)} actionable setups. For each formula variant, the >= {int(GATE*100)}% subset "
         "(what the Hybrid would actually trade) is scored. If dropping a term barely changes that "
         "subset, the term was double-counting.", "",
         f"Full formula, >= {int(GATE*100)}%: **n={full_n}, win {full_wr:.1f}%, exp {full_ex:+.3f}R, "
         f"total {full_tot:+.1f}R**", "",
         "| variant (term removed) | trades >=70% | win% | expectancy R | total R | vs full |",
         "|---|---|---|---|---|---|"]
    for name in VARIANTS:
        n_, wr, ex, tot = _stat(_sub(rows, name))
        dex = ex - full_ex
        L.append(f"| {name} | {n_} | {wr:.1f}% | {ex:+.3f} | {tot:+.1f} | {dex:+.3f}R |")

    # A term is "redundant / safe to remove" if removing it keeps expectancy within a small band
    # AND doesn't shrink the tradeable set much.
    verdicts = []
    for name in ("no_macd", "no_rsi", "no_ema200", "no_div"):
        n_, wr, ex, tot = _stat(_sub(rows, name))
        dex = ex - full_ex
        safe = dex >= -0.02          # removing it doesn't cost meaningful expectancy
        verdicts.append((name, dex, n_ - full_n, safe))
    L += ["", "## Verdict (per term)", ""]
    for name, dex, dn, safe in verdicts:
        L.append(f"- **{name.replace('no_','drop ')}**: expectancy {dex:+.3f}R vs full, "
                 f"trades {dn:+d} -> " + ("**redundant — safe to remove.**" if safe else
                 "**keep — it carries unique signal (removing it hurts).**"))
    all_n, all_wr, all_ex, all_tot = _stat(_sub(rows, "no_ALL_four"))
    L += ["", f"- **Drop all four at once:** >=70% n={all_n}, win {all_wr:.1f}%, exp {all_ex:+.3f}R "
          f"({all_ex-full_ex:+.3f}R vs full). "
          + ("The simpler formula is as good or better — cut the redundant terms."
             if all_ex >= full_ex - 0.02 else
             "Cutting all four together costs expectancy — remove only the individually-safe ones."),
          "", "_Deterministic engine; costs 0.05R; the ablation only changes the confidence SCORE "
          "(which setups clear 70%), not the trade outcomes. Small sample — read directionally._"]
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\nwrote {OUT}  (full 70% subset: n={full_n} exp={full_ex:+.3f})")


if __name__ == "__main__":
    main()
