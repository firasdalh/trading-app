"""Backtest measurement of the entry circuit breakers (naive replay over a trade stream)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest.breakers import simulate_breakers
from app.backtest.simulator import BTTrade

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _t(r, *, mins_from_start, hold_min=5, symbol="EURUSDm") -> BTTrade:
    entry = T0 + timedelta(minutes=mins_from_start)
    return BTTrade(symbol=symbol, direction="long", regime="trending", strategy="trend",
                   confidence=0.7, entry_time=entry, entry=1.10, stop=1.09, target=1.12,
                   planned_rr=2.0, exit_time=entry + timedelta(minutes=hold_min),
                   exit=1.11, outcome="target" if r > 0 else "stop", r=r, bars_held=1)


def _row(impacts, sub):
    return next(im for im in impacts if sub in im.name)


def test_trade_count_blocks_beyond_cap_same_day():
    trades = [_t(1.0, mins_from_start=i * 30) for i in range(5)]  # 5 trades, one day
    imp = _row(simulate_breakers(trades, max_trades_per_day=3, max_consecutive_losses=0), "trades/day")
    assert imp.blocked == 2


def test_consecutive_losses_block_within_cooldown():
    trades = [_t(-1.0, mins_from_start=0), _t(-1.0, mins_from_start=10), _t(-1.0, mins_from_start=20),
              _t(1.0, mins_from_start=30)]  # 4th enters 30m; last loss closed 25m -> within 120m
    imp = _row(simulate_breakers(trades, max_consecutive_losses=3, breaker_cooldown_minutes=120,
                                 max_trades_per_day=0), "losses in a row")
    assert imp.blocked == 1
    assert imp.net_effect_r == -1.0  # skipping a +1R probe costs 1R this time


def test_consecutive_losses_probe_after_cooldown():
    trades = [_t(-1.0, mins_from_start=0), _t(-1.0, mins_from_start=10), _t(-1.0, mins_from_start=20),
              _t(1.0, mins_from_start=200)]  # 4th enters 200m; last loss closed 25m -> past 120m cooldown
    imp = _row(simulate_breakers(trades, max_consecutive_losses=3, breaker_cooldown_minutes=120,
                                 max_trades_per_day=0), "losses in a row")
    assert imp.blocked == 0
    assert imp.note  # flags that the cooldown didn't bind on this spacing


def test_perf_breaker_blocks_below_floor():
    trades = [_t(-0.5, mins_from_start=i * 10) for i in range(4)]  # avg -0.5R over 4
    trades.append(_t(1.0, mins_from_start=45))                     # within cooldown of the last close
    imp = _row(simulate_breakers(trades, max_trades_per_day=0, max_consecutive_losses=0,
                                 expectancy_window=4, min_expectancy_r=-0.2,
                                 breaker_cooldown_minutes=120), "Perf floor")
    assert imp.blocked == 1


def test_all_off_blocks_nothing():
    trades = [_t(-1.0, mins_from_start=i * 10) for i in range(6)]
    imps = simulate_breakers(trades, max_trades_per_day=0, max_consecutive_losses=0, expectancy_window=0)
    assert all(im.blocked == 0 for im in imps)
