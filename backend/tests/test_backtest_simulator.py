"""Multi-timeframe backtest simulator: trade scoring (R-multiples, conservative fills) + metrics.

(The single-symbol $-equity engine is covered by test_backtest.py; this covers the per-regime,
multi-timeframe R-based harness in app/backtest/simulator.py.)"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.simulator import BTTrade, _simulate_trade, compute_metrics, simulate_symbol
from app.models.enums import AssetClass, Direction
from app.models.schemas import Candle, OHLCVSeries, TradeProposal

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _c(k: int, hi: float, lo: float, close: float, step_h: int = 1) -> Candle:
    return Candle(ts=T0 + timedelta(hours=k * step_h), open=close, high=hi, low=lo, close=close,
                  volume=1000.0)


def _prop(direction: Direction, entry: float, stop: float, target: float) -> TradeProposal:
    return TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h", direction=direction,
                         entry=entry, stop_loss=stop, take_profit=target, confidence=0.6,
                         regime="trending", strategy="trend")


# ---------------------------------------------------------------- trade scoring

def test_long_hits_target_scores_plus_planned_r():
    candles = [_c(0, 100, 100, 100), _c(1, 102.5, 100.2, 101.0)]  # bar1 tags target 102
    t = _simulate_trade("X", candles, 0, _prop(Direction.LONG, 100, 99, 102), max_hold=10, cost_r=0)
    assert t.outcome == "target" and t.r == 2.0 and t.bars_held == 1 and t.planned_rr == 2.0


def test_long_hits_stop_scores_minus_one_r():
    candles = [_c(0, 100, 100, 100), _c(1, 100.3, 98.5, 99.0)]    # bar1 tags stop 99
    t = _simulate_trade("X", candles, 0, _prop(Direction.LONG, 100, 99, 102), max_hold=10, cost_r=0)
    assert t.outcome == "stop" and t.r == -1.0


def test_ambiguous_bar_assumes_stop_first():
    # One bar tags BOTH stop and target -> conservative: the stop is assumed hit first.
    candles = [_c(0, 100, 100, 100), _c(1, 102.5, 98.5, 100.0)]
    t = _simulate_trade("X", candles, 0, _prop(Direction.LONG, 100, 99, 102), max_hold=10, cost_r=0)
    assert t.outcome == "stop" and t.r == -1.0


def test_short_target_and_stop():
    target = [_c(0, 100, 100, 100), _c(1, 100.3, 97.5, 98.0)]     # tags target 98
    t = _simulate_trade("X", target, 0, _prop(Direction.SHORT, 100, 101, 98), max_hold=10, cost_r=0)
    assert t.outcome == "target" and t.r == 2.0
    stop = [_c(0, 100, 100, 100), _c(1, 101.5, 99.7, 101.0)]      # tags stop 101
    t2 = _simulate_trade("X", stop, 0, _prop(Direction.SHORT, 100, 101, 98), max_hold=10, cost_r=0)
    assert t2.outcome == "stop" and t2.r == -1.0


def test_timeout_exits_at_close_of_last_held_bar():
    # Never tags either level within max_hold -> exit at the time-stop bar's close.
    candles = [_c(0, 100, 100, 100)] + [_c(k, 100.5, 99.5, 100.4) for k in range(1, 6)]
    t = _simulate_trade("X", candles, 0, _prop(Direction.LONG, 100, 99, 102), max_hold=3, cost_r=0)
    assert t.outcome == "timeout" and t.bars_held == 3 and t.r == pytest.approx(0.4)


def test_cost_r_is_subtracted():
    candles = [_c(0, 100, 100, 100), _c(1, 102.5, 100.2, 101.0)]
    t = _simulate_trade("X", candles, 0, _prop(Direction.LONG, 100, 99, 102), max_hold=10, cost_r=0.1)
    assert t.r == pytest.approx(1.9)


def test_zero_risk_proposal_is_skipped():
    candles = [_c(0, 100, 100, 100), _c(1, 101, 99, 100)]
    assert _simulate_trade("X", candles, 0, _prop(Direction.LONG, 100, 100, 102),
                           max_hold=5, cost_r=0) is None


# ---------------------------------------------------------------- metrics

def _t(r: float, regime: str = "trending") -> BTTrade:
    return BTTrade(symbol="X", direction="long", regime=regime, strategy="trend", confidence=0.6,
                   entry_time=T0, entry=100, stop=99, target=102, planned_rr=2.0, exit_time=T0,
                   exit=102, outcome="target", r=r, bars_held=1)


def test_metrics_empty():
    m = compute_metrics([])
    assert m.n == 0 and m.expectancy_r == 0.0 and m.profit_factor == 0.0


def test_metrics_core_values():
    m = compute_metrics([_t(2), _t(-1), _t(2), _t(-1), _t(-1)])
    assert m.n == 5 and m.wins == 2 and m.losses == 3
    assert m.win_rate == pytest.approx(0.4)
    assert m.total_r == pytest.approx(1.0) and m.expectancy_r == pytest.approx(0.2)
    assert m.profit_factor == pytest.approx(4.0 / 3.0)
    assert m.avg_win_r == pytest.approx(2.0) and m.avg_loss_r == pytest.approx(-1.0)
    assert m.breakeven_wr == pytest.approx(1.0 / 3.0)
    assert m.max_dd_r == pytest.approx(2.0)


def test_metrics_profit_factor_infinite_without_losses():
    assert compute_metrics([_t(2), _t(1.5)]).profit_factor == float("inf")


# ---------------------------------------------------------------- end-to-end smoke

class _FakeBroker:
    def __init__(self, by_tf: dict[str, list[Candle]]):
        self._by_tf = by_tf

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        c = self._by_tf.get(timeframe, [])
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=c[-limit:])


def _trend(n: int, step_h: int, base: float, drift: float) -> list[Candle]:
    px = base
    out = []
    for k in range(n):
        nxt = px + drift
        out.append(Candle(ts=T0 + timedelta(hours=k * step_h), open=px, high=max(px, nxt) + 0.2,
                          low=min(px, nxt) - 0.2, close=nxt, volume=1000.0))
        px = nxt
    return out


def test_simulate_symbol_runs_without_lookahead():
    # Full pipeline on synthetic data: it must run, return a list, and never produce a trade whose
    # exit precedes its entry (a look-ahead / ordering bug would surface here).
    broker = _FakeBroker({"1h": _trend(320, 1, 100.0, 0.05), "1d": _trend(80, 24, 100.0, 0.6)})
    trades = simulate_symbol(broker, "X", AssetClass.FOREX, "1h", bars=320, context_bars=80,
                             max_hold=24, cooldown=2)
    assert isinstance(trades, list)
    for t in trades:
        assert t.exit_time > t.entry_time and t.bars_held >= 1


def test_simulate_symbol_returns_empty_on_thin_history():
    broker = _FakeBroker({"1h": _trend(40, 1, 100.0, 0.05), "1d": _trend(5, 24, 100.0, 0.6)})
    assert simulate_symbol(broker, "X", AssetClass.FOREX, "1h", bars=40) == []


def test_regime_filter_stands_aside():
    # Restricting to an unreachable regime -> every proposal is skipped (no trades).
    broker = _FakeBroker({"1h": _trend(320, 1, 100.0, 0.05), "1d": _trend(80, 24, 100.0, 0.6)})
    assert simulate_symbol(broker, "X", AssetClass.FOREX, "1h", bars=320, context_bars=80,
                           max_hold=24, cooldown=2, regimes={"nope"}) == []
