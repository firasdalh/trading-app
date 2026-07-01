"""Multi-timeframe backtest simulator: trade scoring (R-multiples, conservative fills) + metrics.

(The single-symbol $-equity engine is covered by test_backtest.py; this covers the per-regime,
multi-timeframe R-based harness in app/backtest/simulator.py.)"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.simulator import (
    BTTrade,
    _armed_outcome,
    _crossed_intrabar,
    _simulate_trade,
    _tighter,
    compute_metrics,
    simulate_armed_symbol,
    simulate_symbol,
    simulate_symbol_advisor,
    simulate_symbol_stband,
    split_by_time,
    time_folds,
)
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


def _t_at(day: int) -> BTTrade:
    t = _t(1.0)
    t.entry_time = T0 + timedelta(days=day)
    return t


def test_split_by_time_holdout():
    trades = [_t_at(d) for d in range(10)]   # days 0..9
    in_s, oos = split_by_time(trades, 0.3)   # cutoff at day 0 + 0.7*9 = 6.3 -> IS days 0..6, OOS 7..9
    assert len(in_s) == 7 and len(oos) == 3
    assert split_by_time(trades, 0.0) == (trades, [])


def test_time_folds_align_and_cover():
    trades = [_t_at(d) for d in range(12)]    # days 0..11
    folds = time_folds(trades, 3)             # 3 equal-time folds over days 0..11
    assert len(folds) == 3
    assert sum(len(f) for f in folds) == 12   # every trade assigned exactly once
    # earliest trade in fold 0, latest in the last fold
    assert trades[0] in folds[0] and trades[-1] in folds[-1]


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


# ---------------------------------------------------------------- armed strategy

def test_crossed_intrabar():
    bar = _c(1, hi=102, lo=98, close=100)
    assert _crossed_intrabar("buy_stop", bar, 101) and not _crossed_intrabar("buy_stop", bar, 103)
    assert _crossed_intrabar("sell_stop", bar, 99) and not _crossed_intrabar("sell_stop", bar, 97)
    assert _crossed_intrabar("buy_limit", bar, 99) and not _crossed_intrabar("buy_limit", bar, 97)
    assert _crossed_intrabar("sell_limit", bar, 101) and not _crossed_intrabar("sell_limit", bar, 103)


def test_armed_outcome_long_target_and_stop():
    armed = {"order_type": "buy_stop", "trigger": 100.0, "stop": 99.0, "tp": 102.0}
    win = [_c(0, 100, 100, 100), _c(1, 102.5, 100.2, 101)]   # fill bar i=0, next bar tags target
    t = _armed_outcome("X", win, 0, armed, max_hold=10, cost_r=0)
    assert t.outcome == "target" and t.r == 2.0 and t.direction == "long" and t.strategy == "buy_stop"
    loss = [_c(0, 100, 100, 100), _c(1, 100.3, 98.5, 99)]
    t2 = _armed_outcome("X", loss, 0, armed, max_hold=10, cost_r=0)
    assert t2.outcome == "stop" and t2.r == -1.0


def test_armed_outcome_short_and_invalid():
    armed = {"order_type": "sell_stop", "trigger": 100.0, "stop": 101.0, "tp": 98.0}
    win = [_c(0, 100, 100, 100), _c(1, 100.3, 97.5, 98)]
    t = _armed_outcome("X", win, 0, armed, max_hold=10, cost_r=0)
    assert t.outcome == "target" and t.r == 2.0 and t.direction == "short"
    bad = {"order_type": "buy_stop", "trigger": 100.0, "stop": 100.0, "tp": 102.0}   # zero risk
    assert _armed_outcome("X", win, 0, bad, max_hold=10, cost_r=0) is None


def test_simulate_armed_symbol_runs():
    broker = _FakeBroker({"1h": _trend(320, 1, 100.0, 0.05), "1d": _trend(80, 24, 100.0, 0.6)})
    trades, stats = simulate_armed_symbol(broker, "X", AssetClass.FOREX, "1h", bars=320,
                                          context_bars=80, valid_bars=8, max_hold=24, cooldown=2)
    assert isinstance(trades, list) and set(stats) == {"armed", "filled", "expired"}
    assert stats["filled"] == len(trades) and stats["armed"] >= stats["filled"]


def test_regime_filter_stands_aside():
    # Restricting to an unreachable regime -> every proposal is skipped (no trades).
    broker = _FakeBroker({"1h": _trend(320, 1, 100.0, 0.05), "1d": _trend(80, 24, 100.0, 0.6)})
    assert simulate_symbol(broker, "X", AssetClass.FOREX, "1h", bars=320, context_bars=80,
                           max_hold=24, cooldown=2, regimes={"nope"}) == []


# ---------------------------------------------------------------- advisor-aware exit

def test_tighter_direction():
    assert _tighter(True, 99.0, 99.5) and not _tighter(True, 99.0, 98.5)        # long: higher is tighter
    assert _tighter(False, 101.0, 100.5) and not _tighter(False, 101.0, 101.5)  # short: lower is tighter
    assert _tighter(True, None, 50.0)                                            # no stop -> any is tighter


def test_simulate_symbol_advisor_runs_both_gates():
    # The advisor-aware exit must run for both gates, return a list, and never produce a trade whose
    # exit precedes its entry. The recorded stop is the ORIGINAL plan stop (dynamic stop is internal).
    broker = _FakeBroker({"1h": _trend(320, 1, 100.0, 0.05), "1d": _trend(80, 24, 100.0, 0.6)})
    for gate in ("old", "new"):
        for partial in (False, True):
            trades = simulate_symbol_advisor(broker, "X", AssetClass.FOREX, "1h", bars=320,
                                             context_bars=80, max_hold=24, cooldown=2,
                                             weaken_gate=gate, partial=partial)
            assert isinstance(trades, list)
            for t in trades:
                assert t.exit_time > t.entry_time and t.bars_held >= 1


def test_simulate_symbol_stband_runs():
    # The SuperTrend-band strategy must run on a trend, return valid trades, and never exit before
    # it enters. A steady uptrend should produce at least one long.
    broker = _FakeBroker({"1h": _trend(320, 1, 100.0, 0.08)})
    trades = simulate_symbol_stband(broker, "X", AssetClass.FOREX, "1h", bars=320, max_hold=40, cooldown=1)
    assert isinstance(trades, list)
    for t in trades:
        assert t.exit_time > t.entry_time and t.bars_held >= 1
        assert t.strategy == "supertrend_band" and t.stop != t.entry


def test_partial_scaleout_caps_a_clean_target_winner():
    # A clean winner that reaches +1.5R then the +2R target: WITHOUT partial it scores ~+2R; WITH
    # partial it banks half at +1.5R and the runner targets +2R -> 0.5*1.5 + 0.5*2.0 = +1.75R < +2R.
    broker = _FakeBroker({"1h": _trend(360, 1, 100.0, 0.06), "1d": _trend(90, 24, 100.0, 0.7)})
    base = dict(bars=360, context_bars=90, max_hold=40, cooldown=2, weaken_gate="new")
    plain = simulate_symbol_advisor(broker, "X", AssetClass.FOREX, "1h", partial=False, **base)
    scaled = simulate_symbol_advisor(broker, "X", AssetClass.FOREX, "1h", partial=True, **base)
    tgt_plain = [t for t in plain if t.outcome == "target"]
    if tgt_plain:  # only assert when the synthetic series actually produced a target-hit winner
        tp = tgt_plain[0]
        ts = next(t for t in scaled if t.entry_time == tp.entry_time)
        assert ts.r < tp.r  # the banked half at +1.5R caps the clean +2R winner
