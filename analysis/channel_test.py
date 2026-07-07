"""Validate the market-map (regression-channel / level-proximity) confidence factor.

The factor only changes the confidence SCORE (which setups clear the 70% Hybrid gate); the trade
outcomes are fixed. So we score every actionable setup WITH the factor (disable={}) and WITHOUT it
(disable={'conf_channel'}), then compare the >=70% subset each way — win%, expectancy, and an
in-/out-of-sample split. Keep the factor ONLY if 'don't buy into a channel wall' actually helps.

Efficient: technical computed once per bar; the cheap decision is re-run per variant on the cached read.
Run with uvicorn stopped: PYTHONPATH=backend python analysis/channel_test.py
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
OUT = Path(__file__).with_name("channel_test.md")
VARIANTS = {"WITH channel": frozenset(), "WITHOUT channel": frozenset({"conf_channel"})}


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
        confs = {name: _deterministic_decision(symbol, ac, tf, tech, fund, now=t_i, trend_only=True,
                                               disable=dis).confidence
                 for name, dis in VARIANTS.items()}
        rows.append((t_i, trade.r, confs))
        i = i + max(1, trade.bars_held) + COOLDOWN


def _stat(rs):
    a = np.array(rs, dtype=float)
    if len(a) == 0:
        return (0, 0.0, 0.0, 0.0)
    return (len(a), (a > 0).mean() * 100, a.mean(), a.sum())


def _sub(rows, name):
    return [r for _t, r, c in rows if c[name] >= GATE]


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

    rows.sort(key=lambda x: x[0])
    cut = int(len(rows) * 0.7)
    L = ["# Market-map (regression-channel level-proximity) — does it help?", "",
         f"{len(rows)} actionable setups. Comparing the >= {int(GATE*100)}% subset (what the Hybrid "
         "trades) with vs without the channel factor.", "",
         "| variant | trades >=70% | win% | expectancy R | total R |", "|---|---|---|---|---|"]
    for name in VARIANTS:
        n_, wr, ex, tot = _stat(_sub(rows, name))
        L.append(f"| {name} | {n_} | {wr:.1f}% | {ex:+.3f} | {tot:+.1f} |")

    L += ["", "## In-sample vs out-of-sample (last 30% held out)", "",
          "| window | WITH win% / exp | WITHOUT win% / exp |", "|---|---|---|"]
    for label, sub in [("in-sample", rows[:cut]), ("OUT-OF-SAMPLE", rows[cut:])]:
        _, ww, wex, _2 = _stat([r for _t, r, c in sub if c["WITH channel"] >= GATE])
        _, ow, oex, _3 = _stat([r for _t, r, c in sub if c["WITHOUT channel"] >= GATE])
        L.append(f"| {label} | {ww:.0f}% / {wex:+.3f} | {ow:.0f}% / {oex:+.3f} |")

    nw, _w2, exw, _w3 = _stat(_sub(rows, "WITH channel"))
    no, _o2, exo, _o3 = _stat(_sub(rows, "WITHOUT channel"))
    _, ww_is, wis, _ = _stat([r for _t, r, c in rows[:cut] if c["WITH channel"] >= GATE])
    _, ow_is, ois, _ = _stat([r for _t, r, c in rows[:cut] if c["WITHOUT channel"] >= GATE])
    _, ww_o, wos, _ = _stat([r for _t, r, c in rows[cut:] if c["WITH channel"] >= GATE])
    _, ow_o, oos, _ = _stat([r for _t, r, c in rows[cut:] if c["WITHOUT channel"] >= GATE])
    holds = (wis >= ois) and (wos >= oos)   # WITH >= WITHOUT in BOTH windows
    L += ["", "## Verdict", "",
          f"- Full sample: WITH **{exw:+.3f}R** vs WITHOUT **{exo:+.3f}R** (delta {exw-exo:+.3f}R).",
          "- " + ("**Improves in BOTH in- and out-of-sample -> WIRE IT.** Knowing where price sits in "
                  "the channel adds real value (don't buy into the wall)."
                  if holds and exw > exo else
                  "**Does NOT reliably beat WITHOUT (or fails out-of-sample) -> keep it OFF.** The "
                  "channel factor doesn't earn its place on this sample; leave the formula as-is."),
          "", "_Deterministic engine; costs 0.05R; the factor only changes the confidence SCORE, not "
          "the trade outcomes. Small sample — read directionally._"]
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\nwrote {OUT}  (WITH {exw:+.3f} vs WITHOUT {exo:+.3f}, holds_oos={holds})")


if __name__ == "__main__":
    main()
