"""Generate a per-entry dataset from the DETERMINISTIC funnel over historical data — the shared
input for Task 2 (indicator correlation), Task 3 (entry timing), and Task 6 (drawdown simulation).

Replays the live funnel config (trend_only=True) bar-by-bar with NO look-ahead (exactly as
simulate_symbol does), and at each actionable entry records:
  - the entry-timeframe indicators (EMA20/50/200, ADX, MACD hist, RSI, ATR) + EMA20 five bars back,
  - the trade's realized R outcome (simulated to stop/target), and
  - where the entry sits inside the ADX>=25 "trend run" it belongs to (elapsed bars + price move).
Caches to analysis/entries.json so the three analyses run offline (one MT5 session total).

Real broker data via settings.broker_map (NEVER an empty map — that would be synthetic garbage).
Run from backend/ with uvicorn stopped:  PYTHONPATH=backend python analysis/generate_backtest_entries.py
"""
from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path

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
ADX_TREND = 25.0                 # the funnel's "trending" threshold (_ADX_STRONG)
OUT = Path(__file__).with_name("entries.json")


def _entry_ind(res, tf) -> dict:
    if not res or not res.timeframes:
        return {}
    prim = next((x for x in res.timeframes if x.timeframe == tf), res.timeframes[0])
    return prim.indicators or {}


def _adx_runs(adx_series: list) -> list[tuple[int, int]]:
    """Contiguous [start, end] index runs where ADX >= 25 (a 'trend' per the funnel)."""
    runs, start = [], None
    for idx, a in enumerate(adx_series):
        on = a is not None and a >= ADX_TREND
        if on and start is None:
            start = idx
        elif not on and start is not None:
            runs.append((start, idx - 1)); start = None
    if start is not None:
        runs.append((start, len(adx_series) - 1))
    return runs


def _run_for(idx: int, runs: list[tuple[int, int]]):
    for a, b in runs:
        if a <= idx <= b:
            return (a, b)
    return None


def gen_symbol(broker, symbol: str, ac: AssetClass, tf: str) -> list[dict]:
    tfs = _timeframes_for(tf)
    series: dict[str, list] = {}
    for t in tfs:
        limit = BARS if t == tf else CONTEXT_BARS
        try:
            sd = broker.get_ohlcv(symbol, t, limit=limit)
            series[t] = list(sd.candles) if sd and sd.candles else []
        except Exception:  # noqa: BLE001
            series[t] = []
    entry = series.get(tf, [])
    if len(entry) < _WINDOW + 5:
        return []
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(symbol)
    n = len(entry)

    # --- pass 1: technical (cached) + ADX per bar to segment trend runs ---
    techs: list = [None] * n
    adx_series: list = [None] * n
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
        if not ok:
            continue
        res = run_technical(symbol, window, use_llm=False)
        techs[i] = res
        adx_series[i] = _entry_ind(res, tf).get("adx")
    runs = _adx_runs(adx_series)

    # --- pass 2: decisions + entries (block re-entry while a trade is open, like the live scanner) ---
    out: list[dict] = []
    i = _WINDOW - 1
    while i < n:
        res = techs[i]
        if res is None:
            i += 1; continue
        prop = _deterministic_decision(symbol, ac, tf, res, fund, now=entry[i].ts, trend_only=True)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1; continue
        trade = _simulate_trade(symbol, entry, i, prop, max_hold=MAX_HOLD, cost_r=COST_R)
        if trade is None:
            i += 1; continue
        ind = _entry_ind(res, tf)
        ema20_prev = _entry_ind(techs[i - 5], tf).get("ema20") if i - 5 >= _WINDOW - 1 and techs[i - 5] else None
        rec = {
            "symbol": symbol, "tf": tf, "time": entry[i].ts.isoformat(),
            "direction": prop.direction.value, "regime": prop.regime,
            "confidence": prop.confidence, "r": trade.r, "outcome": trade.outcome,
            "bars_held": trade.bars_held,
            "ema20": ind.get("ema20"), "ema50": ind.get("ema50"), "ema200": ind.get("ema200"),
            "ema20_prev5": ema20_prev, "adx": ind.get("adx"), "macd_hist": ind.get("macd_hist"),
            "rsi": ind.get("rsi14"), "atr": ind.get("atr14"), "close": entry[i].close,
        }
        run = _run_for(i, runs)
        if run is not None:
            a, b = run
            total_bars = b - a
            rec["trend_bars_elapsed"] = i - a
            rec["trend_bars_total"] = total_bars
            rec["trend_bars_pct"] = round((i - a) / total_bars, 4) if total_bars > 0 else 0.0
            start_px, end_px, here_px = entry[a].close, entry[b].close, entry[i].close
            total_move = end_px - start_px
            rec["trend_move_pct"] = round((here_px - start_px) / total_move, 4) if abs(total_move) > 1e-9 else None
        out.append(rec)
        i = i + max(1, trade.bars_held) + COOLDOWN   # no overlapping entries on the same symbol
    return out


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    items = s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    syms = [(it.symbol, it.asset_class, it.timeframe) for it in items]
    s.close()
    if not syms:
        print("no watchlist symbols"); return
    print(f"generating entries for {len(syms)} symbols (bars={BARS}, cost_r={COST_R})")
    all_entries: list[dict] = []
    for sym, ac, tf in syms:
        try:
            broker = get_broker_for(AssetClass(ac), bmap)
            e = gen_symbol(broker, sym, AssetClass(ac), tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}"); continue
        print(f"  {sym}: {len(e)} entries"); sys.stdout.flush()
        all_entries += e
    OUT.write_text(json.dumps(all_entries, indent=0))
    print(f"\nwrote {len(all_entries)} entries -> {OUT}")


if __name__ == "__main__":
    main()
