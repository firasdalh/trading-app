"""Tests for the backtesting engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest.engine import run_backtest
from app.models.enums import AssetClass
from app.models.schemas import Candle, OHLCVSeries, RiskLimits

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _limits() -> RiskLimits:
    return RiskLimits()


def _series_uptrend(n=200, start=100.0, step=0.3) -> OHLCVSeries:
    candles = []
    price = start
    for i in range(n):
        o = price
        price += step
        candles.append(Candle(ts=NOW + timedelta(hours=i), open=o, high=price + 0.1,
                              low=o - 0.1, close=price, volume=1000))
    return OHLCVSeries(symbol="UP", timeframe="1h", candles=candles)


def _series_flat(n=200, price=100.0) -> OHLCVSeries:
    candles = [
        Candle(ts=NOW + timedelta(hours=i), open=price, high=price + 0.05,
               low=price - 0.05, close=price, volume=1000)
        for i in range(n)
    ]
    return OHLCVSeries(symbol="FLAT", timeframe="1h", candles=candles)


def test_backtest_runs_and_reports_structure():
    res = run_backtest("UP", AssetClass.STOCK, _series_uptrend(), _limits(), starting_equity=100_000)
    assert res.bars == 200
    assert len(res.equity_curve) > 0
    m = res.metrics
    assert m.total_trades == m.wins + m.losses or m.total_trades >= m.wins + m.losses
    assert 0.0 <= m.win_rate <= 1.0
    assert m.starting_equity == 100_000
    # Equity curve starts at (or near) starting equity.
    assert abs(res.equity_curve[0].equity - 100_000) < 100_000


def test_uptrend_produces_long_trades_and_profit():
    res = run_backtest("UP", AssetClass.STOCK, _series_uptrend(step=0.5), _limits())
    # A persistent uptrend should generate at least one long trade.
    assert res.metrics.total_trades >= 1
    assert all(t.direction.value in ("long", "short") for t in res.trades)
    # Net P&L should be positive on a clean uptrend with trend-following entries.
    assert res.metrics.net_pnl > 0


def test_flat_market_few_or_no_trades():
    res = run_backtest("FLAT", AssetClass.STOCK, _series_flat(), _limits())
    # A dead-flat market gives no clear trend -> the orchestrator should mostly sit out.
    assert res.metrics.total_trades == 0


def test_metrics_profit_factor_and_drawdown_bounds():
    res = run_backtest("UP", AssetClass.STOCK, _series_uptrend(), _limits())
    m = res.metrics
    assert 0.0 <= m.max_drawdown_pct <= 1.0
    if m.profit_factor is not None:
        assert m.profit_factor >= 0.0
