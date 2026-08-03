"""Trend-quality filters: the long trend must be SLOPING, and trend strength must be BUILDING.

Both answer a question the older gates don't:
  - "price above EMA200" says nothing about whether that average is going anywhere (flat EMA200 +
    price above it = a range, not an uptrend);
  - "ADX >= 23" says nothing about whether the trend is forming or dying (30-and-falling vs
    24-and-rising look identical to a level check).

Added 2026-08-03 from a filter review. Both are UNVALIDATED hypotheses on this book — toggles, so a
week of live results can judge them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.agents.indicators import ema_slope
from app.agents.orchestrator import _SLOPE_MIN_ATR, _deterministic_decision
from app.backtest.simulator import _neutral_fundamental
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead

NOW = datetime.now(timezone.utc)


def _entry(*, slope: float, adx: float = 30.0, adx_prev: float | None = 28.0,
           down: bool = False) -> dict:
    """A clean entry-TF read; ``slope`` and ``adx_prev`` are what these two filters look at."""
    if down:
        stack = {"ema20": 101.0, "ema50": 103.0, "ema200": 106.0,
                 "plus_di": 14.0, "minus_di": 28.0, "rsi14": 42.0,
                 "macd_hist": -0.6, "macd_hist_prev": -0.3}
    else:
        stack = {"ema20": 99.0, "ema50": 97.0, "ema200": 94.0,
                 "plus_di": 28.0, "minus_di": 14.0, "rsi14": 58.0,
                 "macd_hist": 0.6, "macd_hist_prev": 0.3}
    ind = {"last_close": 100.0, "atr14": 2.0, "adx": adx, "vol_ratio": 1.2,
           "ema_slope": slope, **stack}
    if adx_prev is not None:
        ind["adx_prev"] = adx_prev
    return ind


def _tf(trend: str, close: float = 100.0) -> dict:
    if trend == "up":
        e20, e50, e200 = close - 1, close - 3, close - 6
    else:
        e20, e50, e200 = close + 1, close + 3, close + 6
    return {"last_close": close, "ema20": e20, "ema50": e50, "ema200": e200,
            "atr14": 2.0, "adx": 26.0, "macd_hist": 0.2}


def _tech(entry_ind: dict, big: str = "up") -> TechnicalRead:
    """Entry 1h + 4h + 1d, with the big picture agreeing by default so the daily filter passes."""
    return TechnicalRead(symbol="X", overall_trend=big, confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend=big, indicators=entry_ind,
                      support_levels=[80.0], resistance_levels=[140.0]),
        TimeframeRead(timeframe="4h", trend=big, indicators=_tf(big),
                      support_levels=[], resistance_levels=[]),
        TimeframeRead(timeframe="1d", trend=big, indicators=_tf(big),
                      support_levels=[], resistance_levels=[]),
    ])


def _decide(tech, disable=frozenset()):
    return _deterministic_decision("X", AssetClass.STOCK, "1h", tech,
                                   _neutral_fundamental("X"), NOW, disable=disable)


# --- the slope indicator itself ---------------------------------------------------------------

def test_ema_slope_measures_travel_in_atrs():
    rising = [100.0 + i * 0.5 for i in range(260)]
    s = ema_slope(rising, 200, 20, 2.0)
    assert s is not None and s > 0            # a steadily rising series -> positive slope


def test_ema_slope_is_flat_on_a_range():
    flat = [100.0 + (1.0 if i % 2 else -1.0) for i in range(260)]
    s = ema_slope(flat, 200, 20, 2.0)
    assert s is not None and abs(s) < _SLOPE_MIN_ATR   # oscillation -> no real travel


def test_ema_slope_needs_history_and_atr():
    assert ema_slope([100.0] * 50, 200, 20, 2.0) is None      # not enough bars
    assert ema_slope([100.0 + i for i in range(260)], 200, 20, 0) is None  # no ATR to normalise by


# --- trend_slope filter -------------------------------------------------------------------------

def test_long_allowed_when_long_trend_is_rising():
    assert _decide(_tech(_entry(slope=0.8))).direction == Direction.LONG


def test_long_blocked_when_long_trend_is_flat():
    d = _decide(_tech(_entry(slope=0.02)))
    assert d.direction == Direction.NO_TRADE and d.watch is True
    assert "FLAT" in d.rationale


def test_long_blocked_when_long_trend_slopes_down():
    d = _decide(_tech(_entry(slope=-0.9)))
    assert d.direction == Direction.NO_TRADE
    assert "other way" in d.rationale


def test_short_blocked_when_long_trend_is_rising():
    d = _decide(_tech(_entry(slope=0.9, down=True), big="down"))
    assert d.direction == Direction.NO_TRADE


def test_short_allowed_when_long_trend_is_falling():
    d = _decide(_tech(_entry(slope=-0.8, down=True), big="down"))
    assert d.direction == Direction.SHORT


def test_trend_slope_can_be_switched_off():
    tech = _tech(_entry(slope=0.02))
    assert _decide(tech).direction == Direction.NO_TRADE
    assert _decide(tech, disable=frozenset({"trend_slope"})).direction == Direction.LONG


def test_missing_slope_skips_the_gate():
    """No slope available (short history) -> don't block blind."""
    ind = _entry(slope=0.0)
    del ind["ema_slope"]
    assert _decide(_tech(ind)).direction == Direction.LONG


# --- adx_rising filter ---------------------------------------------------------------------------

def test_allowed_when_adx_is_rising():
    assert _decide(_tech(_entry(slope=0.8, adx=24.0, adx_prev=21.0))).direction == Direction.LONG


def test_blocked_when_adx_is_falling():
    d = _decide(_tech(_entry(slope=0.8, adx=30.0, adx_prev=34.0)))
    assert d.direction == Direction.NO_TRADE and d.watch is True
    assert "FADING" in d.rationale


def test_flat_adx_is_allowed():
    """Equal to the previous bar is not falling — don't reject on a flat tick."""
    assert _decide(_tech(_entry(slope=0.8, adx=27.0, adx_prev=27.0))).direction == Direction.LONG


def test_high_but_falling_adx_is_rejected_while_low_but_rising_passes():
    """The whole point of the filter: the LEVEL alone can't separate these two."""
    strong_fading = _decide(_tech(_entry(slope=0.8, adx=30.0, adx_prev=33.0)))
    weak_building = _decide(_tech(_entry(slope=0.8, adx=24.0, adx_prev=22.0)))
    assert strong_fading.direction == Direction.NO_TRADE
    assert weak_building.direction == Direction.LONG


def test_adx_rising_can_be_switched_off():
    tech = _tech(_entry(slope=0.8, adx=30.0, adx_prev=34.0))
    assert _decide(tech).direction == Direction.NO_TRADE
    assert _decide(tech, disable=frozenset({"adx_rising"})).direction == Direction.LONG


def test_missing_adx_prev_skips_the_gate():
    assert _decide(_tech(_entry(slope=0.8, adx_prev=None))).direction == Direction.LONG


# --- registration --------------------------------------------------------------------------------

def test_both_filters_are_registered():
    from app.agents.orchestrator import DET_FILTER_KEYS

    assert {"trend_slope", "adx_rising"} <= DET_FILTER_KEYS
