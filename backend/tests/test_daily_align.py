"""Big-picture (daily) alignment filter — a market entry must agree with the timeframe TWO rungs
above the entry (1h→1d, 15m→4h).

Why it exists: over 1605 replayed signals the engine's direction call was right 52 times per 100 —
a coin flip is 50. Requiring the big-picture trend to agree lifted that to 55 (and 56 excluding
forex), the only filter tested that made the edge statistically real. It only ever BLOCKS a market
entry; armed/conditional setups are untouched because they re-validate at their trigger.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.agents.orchestrator import _confirm_trend, _deterministic_decision
from app.backtest.simulator import _neutral_fundamental
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead

NOW = datetime.now(timezone.utc)

# A clean, unambiguous LONG on the entry TF: strong trend, momentum with it, at value, room to run.
_UP = {"last_close": 100.0, "ema20": 99.0, "ema50": 97.0, "ema200": 94.0, "atr14": 2.0,
       "adx": 30.0, "plus_di": 28.0, "minus_di": 14.0, "rsi14": 58.0,
       "macd_hist": 0.6, "macd_hist_prev": 0.3, "vol_ratio": 1.2}


def _ind(trend: str, close: float = 100.0) -> dict:
    """EMA stack for a given trend direction (the engine derives trend from the numbers)."""
    if trend == "up":
        e20, e50, e200 = close - 1, close - 3, close - 6
    elif trend == "down":
        e20, e50, e200 = close + 1, close + 3, close + 6
    else:                                    # sideways: EMA20 == EMA50
        e20 = e50 = close
        e200 = close
    return {"last_close": close, "ema20": e20, "ema50": e50, "ema200": e200,
            "atr14": 2.0, "adx": 26.0, "macd_hist": 0.2}


def _tech(*, htf: str, big: str, entry=None) -> TechnicalRead:
    """1h entry + 4h (immediate higher) + 1d (the big-picture confirmation TF)."""
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators=entry or _UP,
                      support_levels=[90.0], resistance_levels=[130.0]),
        TimeframeRead(timeframe="4h", trend=htf, indicators=_ind(htf),
                      support_levels=[], resistance_levels=[]),
        TimeframeRead(timeframe="1d", trend=big, indicators=_ind(big),
                      support_levels=[], resistance_levels=[]),
    ])


def _decide(tech, disable=frozenset(), timeframe="1h"):
    return _deterministic_decision("X", AssetClass.STOCK, timeframe, tech,
                                   _neutral_fundamental("X"), NOW, disable=disable)


# --- which timeframe gets used --------------------------------------------------------------

def test_picks_two_rungs_above_the_entry():
    """1h entry -> the DAILY confirms (not the 4h, which the laddered rule already handles)."""
    trend, name = _confirm_trend(_tech(htf="up", big="down"), "1h")
    assert name == "1d" and trend == "down"


def test_falls_back_to_highest_when_ladder_is_short():
    """A 4h entry only has 1d above it — use that rather than skipping the check."""
    trend, name = _confirm_trend(_tech(htf="up", big="up"), "4h")
    assert name == "1d"


def test_no_higher_timeframe_skips_the_gate():
    """Nothing above the entry -> no opinion, don't block blind."""
    solo = TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators=_UP,
                      support_levels=[90.0], resistance_levels=[130.0])])
    assert _confirm_trend(solo, "1h") == ("sideways", "")
    assert _decide(solo).direction == Direction.LONG


# --- the gate --------------------------------------------------------------------------------

def test_long_allowed_when_big_picture_agrees():
    d = _decide(_tech(htf="up", big="up"))
    assert d.direction == Direction.LONG


def test_long_blocked_when_daily_is_down():
    d = _decide(_tech(htf="up", big="down"))
    assert d.direction == Direction.NO_TRADE
    assert d.watch is True
    assert "1d" in d.rationale and "DOWN" in d.rationale


def test_long_blocked_when_daily_is_sideways():
    """A flat big picture is NOT agreement — the strict reading is what tested best."""
    d = _decide(_tech(htf="up", big="sideways"))
    assert d.direction == Direction.NO_TRADE
    assert "no clear direction" in d.rationale


def test_short_blocked_when_daily_is_up():
    down_entry = {**_UP, "ema20": 101.0, "ema50": 103.0, "ema200": 106.0,
                  "plus_di": 14.0, "minus_di": 28.0, "rsi14": 42.0,
                  "macd_hist": -0.6, "macd_hist_prev": -0.3}
    d = _decide(_tech(htf="down", big="up", entry=down_entry))
    assert d.direction == Direction.NO_TRADE
    assert "1d" in d.rationale


def test_short_allowed_when_daily_is_down():
    down_entry = {**_UP, "ema20": 101.0, "ema50": 103.0, "ema200": 106.0,
                  "plus_di": 14.0, "minus_di": 28.0, "rsi14": 42.0,
                  "macd_hist": -0.6, "macd_hist_prev": -0.3}
    d = _decide(_tech(htf="down", big="down", entry=down_entry))
    assert d.direction == Direction.SHORT


# --- toggle ----------------------------------------------------------------------------------

def test_filter_can_be_switched_off():
    """Disabling it restores the previous (laddered-only) behaviour."""
    tech = _tech(htf="up", big="down")
    assert _decide(tech).direction == Direction.NO_TRADE
    assert _decide(tech, disable=frozenset({"daily_align"})).direction == Direction.LONG


def test_filter_is_registered_and_on_by_default():
    from app.agents.orchestrator import DET_FILTER_KEYS

    assert "daily_align" in DET_FILTER_KEYS       # appears in the UI filter list
    assert _decide(_tech(htf="up", big="down")).direction == Direction.NO_TRADE  # applied unless disabled
