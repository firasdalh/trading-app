"""AI-in-the-loop simulation over the LAST 30 DAYS.

Walks each chart bar-by-bar with NO look-ahead. At every bar the DETERMINISTIC engine runs; when it
produces a setup, the LIVE AI reviewer (your configured LLM) confirms or vetoes it exactly as it
would live. Confirmed trades are followed to their stop/target. Reports the win rate of what the
AI-inclusive engine would actually have traded — the "how much win out of 100" you asked for.

NOTE: calls the real LLM (OpenAI gpt-5-mini) once per setup — uses quota, takes a few minutes, and is
ONE non-deterministic realization (re-running can differ slightly). Real broker data via broker_map.
Run with uvicorn stopped: PYTHONPATH=backend python analysis/ai_lastmonth.py
"""
from __future__ import annotations

import bisect
import sys
from datetime import timedelta

from sqlalchemy import select

from app.agents.orchestrator import _deterministic_decision, run_orchestrator
from app.agents.pipeline import _timeframes_for
from app.agents.technical import run_technical
from app.backtest.simulator import _WINDOW, _neutral_fundamental, _simulate_trade
from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.enums import AssetClass, Direction
from app.models.schemas import OHLCVSeries

DAYS = 30
BARS = 1400          # enough history for the 200-bar window + 30 days of evaluation
CONTEXT_BARS = 700
MAX_HOLD = 96
COOLDOWN = 3
COST_R = 0.05


def _wr(rs):
    if not rs:
        return "n=0"
    w = sum(1 for r in rs if r > 0)
    return f"n={len(rs):3d}  win={100*w/len(rs):4.1f}%  ({w}W/{len(rs)-w}L)  exp={sum(rs)/len(rs):+.3f}R  net={sum(rs):+.1f}R"


def run_symbol(broker, symbol, ac, tf):
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
        return [], [], 0
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(symbol)
    n = len(entry)
    cutoff = entry[-1].ts - timedelta(days=DAYS)
    det, ai, vetoed = [], [], 0
    i = _WINDOW - 1
    while i < n:
        if entry[i].ts < cutoff:
            i += 1; continue
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
        technical = run_technical(symbol, window, use_llm=False)
        prop = _deterministic_decision(symbol, ac, tf, technical, fund, now=t_i, trend_only=True)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1; continue
        trade = _simulate_trade(symbol, entry, i, prop, max_hold=MAX_HOLD, cost_r=COST_R)
        if trade is None:
            i += 1; continue
        det.append(trade.r)
        # --- the AI reviewer's live verdict on this exact setup ---
        reviewed = run_orchestrator(symbol, ac, tf, technical, fund, now=t_i, use_llm=True,
                                    trend_only=True, ai_led=False, scalp=False, st_band=False)
        if reviewed.direction in (Direction.LONG, Direction.SHORT):   # confirmed
            ai.append(trade.r)
        else:                                                          # vetoed
            vetoed += 1
        i = i + max(1, trade.bars_held) + COOLDOWN
    return det, ai, vetoed


def main():
    from app.models.db import WatchItem
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    items = s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    syms = [(it.symbol, it.asset_class, it.timeframe) for it in items]
    s.close()

    DET, AI, VET = [], [], 0
    for sym, ac, tf in syms:
        try:
            broker = get_broker_for(AssetClass(ac), bmap)
            d, a, v = run_symbol(broker, sym, AssetClass(ac), tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}"); continue
        DET += d; AI += a; VET += v
        dw = 100*sum(1 for r in d if r > 0)/len(d) if d else 0
        aw = 100*sum(1 for r in a if r > 0)/len(a) if a else 0
        print(f"  {sym:9s} setups={len(d):2d} (det win {dw:3.0f}%)  AI-confirmed={len(a):2d} "
              f"(win {aw:3.0f}%)  vetoed={v}"); sys.stdout.flush()

    print("\n==== LAST %d DAYS ====" % DAYS)
    print("DETERMINISTIC only (no AI): " + _wr(DET))
    print("AI-INCLUSIVE (confirmed)  : " + _wr(AI))
    print(f"AI vetoed {VET} of {len(DET)} setups")
    if AI:
        aw = 100*sum(1 for r in AI if r > 0)/len(AI)
        print(f"\n>>> AI-inclusive engine: ~{aw:.0f} wins out of 100 trades "
              f"(vs ~{100*sum(1 for r in DET if r>0)/max(1,len(DET)):.0f}/100 deterministic-only)")


if __name__ == "__main__":
    main()
