"""Arm the BREAK of a blocking higher-timeframe level instead of discarding the idea.

When a major 4h/1d level sits in the path, the engine stands aside — and its own rationale said it
was "waiting for a break". But nothing was ever armed to catch that break: the setup was valid in
DIRECTION, blocked only by structure, and simply dropped. This is the exact case a break-stop order
exists for (see ConditionalSuggestion's docstring), so the order that sentence promises is now
actually placed.

The arm reuses ``_conditional_break``, so it inherits a minimum R:R measured FROM the trigger and the
failed-break/reclaim guard. It only ever ARMS — nothing opens until price clears the level and the
conditional system re-validates it at the trigger.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.agents.orchestrator import _arm_level_break, _deterministic_decision
from app.backtest.simulator import _neutral_fundamental
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead

NOW = datetime.now(timezone.utc)

# Clean LONG on the entry TF: strong trend, momentum with it, big picture agreeing.
_UP = {"last_close": 100.0, "ema20": 99.0, "ema50": 97.0, "ema200": 94.0, "atr14": 2.0,
       "adx": 30.0, "plus_di": 28.0, "minus_di": 14.0, "rsi14": 58.0,
       "macd_hist": 0.6, "macd_hist_prev": 0.3, "vol_ratio": 1.2, "adx_prev": 28.0}


def _ctx(trend: str, close: float = 100.0, res=None, sup=None) -> TimeframeRead:
    e20, e50, e200 = ((close - 1, close - 3, close - 6) if trend == "up"
                      else (close + 1, close + 3, close + 6))
    return TimeframeRead(
        timeframe="4h", trend=trend,
        indicators={"last_close": close, "ema20": e20, "ema50": e50, "ema200": e200,
                    "atr14": 2.0, "adx": 26.0, "macd_hist": 0.2},
        support_levels=sup or [], resistance_levels=res or [])


def _tech(entry=None, big="up", d1_res=None, d1_sup=None) -> TechnicalRead:
    daily = _ctx(big, res=d1_res, sup=d1_sup)
    daily.timeframe = "1d"
    return TechnicalRead(symbol="X", overall_trend=big, confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend=big, indicators=entry or _UP,
                      support_levels=[80.0], resistance_levels=[140.0]),
        _ctx(big),
        daily,
    ])


def _decide(tech=None, disable=frozenset()):
    return _deterministic_decision("X", AssetClass.STOCK, "1h", tech or _tech(),
                                   _neutral_fundamental("X"), NOW, disable=disable)


# --- the builder ------------------------------------------------------------------------------

def test_builds_a_retest_buy_limit_at_the_blocking_resistance():
    """Not a stop ABOVE the level — a limit AT it, armed behind a required close-break."""
    brk = _arm_level_break(Direction.LONG, 100.0, 101.0, 2.0, _tech(), None, _UP, 0.6)
    assert brk is not None
    assert brk.order_type == "buy_limit"        # buy the RETEST, not the thrust
    assert brk.break_level == 101.0             # stage 1: this must close-break first
    assert abs(brk.trigger_price - 101.0) < 0.5  # entry sits AT the level, not chasing above it
    assert brk.stop_loss < 101.0                # stop stays beyond it — the level must hold
    assert brk.take_profit > brk.trigger_price
    assert brk.rr > 0


def test_builds_a_retest_sell_limit_at_the_blocking_support():
    brk = _arm_level_break(Direction.SHORT, 100.0, 99.0, 2.0, _tech(big="down"), None, _UP, 0.6)
    assert brk is not None
    assert brk.order_type == "sell_limit"
    assert brk.break_level == 99.0
    assert abs(brk.trigger_price - 99.0) < 0.5
    assert brk.stop_loss > 99.0                 # above the broken support (now resistance)
    assert brk.take_profit < brk.trigger_price


def test_reward_is_measured_from_the_retest_price_not_from_here():
    """R:R has to be honest about the price the trade actually starts at."""
    brk = _arm_level_break(Direction.LONG, 100.0, 101.0, 2.0, _tech(), None, _UP, 0.6)
    risk = brk.trigger_price - brk.stop_loss
    assert abs(brk.rr - (brk.take_profit - brk.trigger_price) / risk) < 0.01


def test_no_atr_means_no_arm():
    assert _arm_level_break(Direction.LONG, 100.0, 101.0, None, _tech(), None, _UP, 0.6) is None


def test_reclaimed_level_is_not_armed():
    """Failed-break guard, inherited from _conditional_break: price already pierced this level and
    came back, so it's a trap, not a barrier (the XAGGBP case)."""
    ind = {**_UP, "recent_high": 105.0}          # already spiked above the 101 level
    assert _arm_level_break(Direction.LONG, 100.0, 101.0, 2.0, _tech(), None, ind, 0.6) is None


# --- wired into the decision ------------------------------------------------------------------

def _blocked_tech():
    """A valid LONG with a DAILY resistance ~0.5 ATR overhead — the blocking case."""
    return _tech(d1_res=[101.0])


def test_blocked_setup_now_carries_a_retest_arm():
    d = _decide(_blocked_tech())
    assert d.direction == Direction.NO_TRADE      # still does NOT buy into the level
    assert d.watch is True
    assert d.conditional is not None              # ...but the break+retest is armed
    assert d.conditional.order_type == "buy_limit"
    assert d.conditional.break_level == 101.0


def test_rationale_explains_the_arm():
    d = _decide(_blocked_tech())
    assert "higher-timeframe level" in d.rationale
    assert "RETEST" in d.rationale
    assert "fake break" in d.rationale


def test_it_never_opens_at_market():
    """The arm must not become a market entry — that's what the level gate exists to prevent."""
    d = _decide(_blocked_tech())
    assert d.entry is None and d.direction == Direction.NO_TRADE


def test_disabling_the_level_filter_still_skips_the_arm():
    """With htf_level off the engine trades THROUGH the level, so there's nothing to arm."""
    d = _decide(_blocked_tech(), disable=frozenset({"htf_level"}))
    assert d.direction == Direction.LONG


def test_unblocked_setup_is_unaffected():
    """No level in the way -> normal market entry, no level-break arm."""
    d = _decide(_tech())
    assert d.direction == Direction.LONG
