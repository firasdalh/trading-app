"""Task 8 — Execution realism: perfect-fill vs slippage/spread-adjusted backtest.

Re-runs the deterministic funnel (trend_only, ADX=25) with NO look-ahead, then re-scores every trade
with the context-dependent execution cost model (app.backtest.slippage): wider near round numbers /
prior swing highs-lows, wider in thin sessions, extra slippage on stop exits. Compares against the
'perfect fill' (gross) numbers and writes analysis/execution_realism.md. Also dumps the realistic R
distribution to analysis/entries_slippage.json so Task 6 (drawdown) / Task 14 (sizing) can consume it.

Real broker data via settings.broker_map. Run with uvicorn stopped:
    PYTHONPATH=backend python analysis/execution_realism.py
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from app.agents.orchestrator import _deterministic_decision
from app.agents.pipeline import _timeframes_for
from app.agents.technical import run_technical
from app.backtest.simulator import _WINDOW, _neutral_fundamental, _simulate_trade, compute_metrics
from app.backtest.slippage import _near_round, _session, spread_cost_r
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
OUT = Path(__file__).with_name("execution_realism.md")
RJSON = Path(__file__).with_name("entries_slippage.json")


def _entry_ind(res, tf) -> dict:
    if not res or not res.timeframes:
        return {}
    prim = next((x for x in res.timeframes if x.timeframe == tf), res.timeframes[0])
    return prim.indicators or {}


def _run_symbol(broker, symbol, ac, tf):
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
        return [], [], []
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(symbol)
    n = len(entry)
    gross, real, costs = [], [], []
    i = _WINDOW - 1
    while i < n:
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
        if not ok:
            i += 1; continue
        res = run_technical(symbol, window, use_llm=False)
        prop = _deterministic_decision(symbol, ac, tf, res, fund, now=t_i, trend_only=True)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1; continue
        tr = _simulate_trade(symbol, entry, i, prop, max_hold=MAX_HOLD, cost_r=0.0)  # gross
        if tr is None:
            i += 1; continue
        ind = _entry_ind(res, tf)
        atr = ind.get("atr14")
        cost = spread_cost_r(entry=tr.entry, stop=tr.stop, atr=atr, entry_time=tr.entry_time,
                             asset_class=ac, outcome=tr.outcome,
                             prior_levels=[ind.get("swing_high"), ind.get("swing_low")])
        gross.append(tr)
        real.append(dataclasses.replace(tr, r=round(tr.r - cost, 4)))
        costs.append((cost, _near_round(tr.entry, atr) if atr else False,
                      _session(ac, tr.entry_time), tr.outcome))
        i = i + max(1, tr.bars_held) + COOLDOWN
    return gross, real, costs


def _fmt(m):
    pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    return f"{m.n} | {m.win_rate*100:.0f}% | {m.expectancy_r:+.3f} | {m.total_r:+.1f} | {pf} | {m.max_dd_r:.1f}"


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    items = s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    syms = [(it.symbol, it.asset_class, it.timeframe) for it in items]
    s.close()

    G, R, C = [], [], []
    per_sym = {}
    for sym, ac, tf in syms:
        try:
            broker = get_broker_for(AssetClass(ac), bmap)
            g, r, c = _run_symbol(broker, sym, AssetClass(ac), tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}"); continue
        if g:
            per_sym[sym] = (compute_metrics(g).expectancy_r, compute_metrics(r).expectancy_r)
        G += g; R += r; C += c
        print(f"  {sym}: {len(g)} trades, avg cost {sum(x[0] for x in c)/max(1,len(c)):.3f}R")
        sys.stdout.flush()

    mg, mr = compute_metrics(G), compute_metrics(R)
    avg_cost = sum(x[0] for x in C) / max(1, len(C))
    near_round = sum(1 for x in C if x[1]) / max(1, len(C))
    thin = sum(1 for x in C if x[2] == "thin") / max(1, len(C))
    stop_share = sum(1 for x in C if x[3] == "stop") / max(1, len(C))

    lines = ["# Task 8 — Execution realism (slippage/spread vs perfect fill)", ""]
    lines.append(f"Re-scored **{mg.n} deterministic trades** with the context-dependent cost model "
                 "(round numbers / prior swing H-L / thin sessions / stop slippage). "
                 f"Average modeled cost **{avg_cost:.3f}R/trade**. "
                 f"{near_round*100:.0f}% of entries sat on a round number; {thin*100:.0f}% filled in a "
                 f"thin session; {stop_share*100:.0f}% exited on a stop (extra slippage).")
    lines += ["", "## Perfect fill vs realistic", "",
              "| fills | trades | win% | expectancy R | total R | profit factor | maxDD R |",
              "|---|---|---|---|---|---|---|",
              f"| perfect (gross) | {_fmt(mg)} |",
              f"| realistic (slippage) | {_fmt(mr)} |"]
    drop = mg.expectancy_r - mr.expectancy_r
    pct = (drop / mg.expectancy_r * 100) if mg.expectancy_r else 0.0
    lines += ["", "## Impact", "",
              f"- Expectancy **{mg.expectancy_r:+.3f}R -> {mr.expectancy_r:+.3f}R** "
              f"(a **{drop:.3f}R**/trade haircut, ~{pct:.0f}% of the gross edge).",
              f"- Profit factor {mg.profit_factor:.2f} -> "
              f"{'inf' if mr.profit_factor==float('inf') else f'{mr.profit_factor:.2f}'}.",
              "- The realistic R distribution is dumped to `entries_slippage.json` and is the input "
              "Task 6 (drawdown) and Task 14 (sizing) should use instead of the gross numbers."]

    lines += ["", "## Per-symbol expectancy (gross -> realistic)", "",
              "| symbol | gross R | realistic R |", "|---|---|---|"]
    for sym, (eg, er) in sorted(per_sym.items(), key=lambda kv: kv[1][1]):
        lines.append(f"| {sym} | {eg:+.3f} | {er:+.3f} |")

    lines += ["", "## Read", ""]
    if mr.expectancy_r <= 0 < mg.expectancy_r:
        lines.append("- **The edge does not survive realistic costs** — it is positive on perfect "
                     "fills but <= 0 after slippage. The backtest edge is a fill-quality illusion; do "
                     "NOT size up on the gross numbers.")
    elif drop > 0.5 * mg.expectancy_r and mg.expectancy_r > 0:
        lines.append("- Slippage eats **more than half** the gross edge. The strategy still profits "
                     "on paper but is highly fill-sensitive — realistic sizing must use the net R, and "
                     "entries on round numbers / in thin sessions are the biggest leaks.")
    else:
        lines.append("- The edge **survives realistic costs** with a modest haircut — but always size "
                     "from the net (realistic) R, not the gross backtest.")
    lines.append("- Isolated to the backtest: the live funnel/Risk Manager are untouched. This only "
                 "gives Tasks 6 & 14 an honest R input.")
    lines.append("")
    lines.append("_Model caveats: costs are ATR-fraction proxies (no per-symbol spread table); "
                 "'prior H/L' uses the entry-TF swing levels; sessions are a coarse UTC-hour heuristic. "
                 "Directionally right, not tick-accurate._")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    RJSON.write_text(json.dumps([t.r for t in R]), encoding="utf-8")
    print(f"\nwrote {OUT} (gross {mg.expectancy_r:+.3f}R -> realistic {mr.expectancy_r:+.3f}R)")


if __name__ == "__main__":
    main()
