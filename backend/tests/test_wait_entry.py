""""Wait, don't chase" — turn a market entry into a LIMIT arm at a better price.

The stop is a STRUCTURAL level and does not move with the entry, so a better fill improves the trade
twice: R shrinks (more size for the same money) and the run to target lengthens. Over 967 replayed
signals, 0.25 ATR scored +0.213R per trade out-of-sample against -0.060R for entering at market —
the only idea tested that did not decay on unseen data.

OFF by default (``wait_entry=0``): it was slightly WORSE on the older half of the data, so the edge
is real but unproven. The cost is honest and unhidden — roughly one trade in four never fills, and
those are skipped entirely.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.agents.orchestrator import _deterministic_decision
from app.backtest.simulator import _neutral_fundamental
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead

NOW = datetime.now(timezone.utc)

# A clean LONG: strong trend, momentum with it, big picture agreeing, room to the next level.
_UP = {"last_close": 100.0, "ema20": 99.0, "ema50": 97.0, "ema200": 94.0, "atr14": 2.0,
       "adx": 30.0, "plus_di": 28.0, "minus_di": 14.0, "rsi14": 58.0,
       "macd_hist": 0.6, "macd_hist_prev": 0.3, "vol_ratio": 1.2, "adx_prev": 28.0}


def _ctx(trend: str, close: float = 100.0) -> dict:
    e20, e50, e200 = ((close - 1, close - 3, close - 6) if trend == "up"
                      else (close + 1, close + 3, close + 6))
    return {"last_close": close, "ema20": e20, "ema50": e50, "ema200": e200,
            "atr14": 2.0, "adx": 26.0, "macd_hist": 0.2}


def _tech(entry=None, big="up") -> TechnicalRead:
    return TechnicalRead(symbol="X", overall_trend=big, confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend=big, indicators=entry or _UP,
                      support_levels=[80.0], resistance_levels=[140.0]),
        TimeframeRead(timeframe="4h", trend=big, indicators=_ctx(big),
                      support_levels=[], resistance_levels=[]),
        TimeframeRead(timeframe="1d", trend=big, indicators=_ctx(big),
                      support_levels=[], resistance_levels=[]),
    ])


def _decide(wait: float, tech=None):
    return _deterministic_decision("X", AssetClass.STOCK, "1h", tech or _tech(),
                                   _neutral_fundamental("X"), NOW, wait_entry=wait)


# --- off by default ---------------------------------------------------------------------------

def test_zero_keeps_the_market_entry():
    d = _decide(0.0)
    assert d.direction == Direction.LONG
    assert d.entry is not None
    assert d.conditional is None


def test_default_parameter_is_off():
    """Callers that don't pass wait_entry must behave exactly as before."""
    d = _deterministic_decision("X", AssetClass.STOCK, "1h", _tech(),
                                _neutral_fundamental("X"), NOW)
    assert d.direction == Direction.LONG and d.conditional is None


# --- the conversion ---------------------------------------------------------------------------

def test_market_entry_becomes_a_limit_arm():
    market = _decide(0.0)
    waited = _decide(0.25)

    # No longer a market trade — it's a pending order, so nothing opens until price comes back.
    assert waited.direction == Direction.NO_TRADE
    assert waited.watch is True
    assert waited.entry is None

    c = waited.conditional
    assert c is not None and c.order_type == "buy_limit"
    # 0.25 ATR (=0.5) better than the market entry.
    assert c.trigger_price < market.entry
    assert abs((market.entry - c.trigger_price) - 0.5) < 1e-6


def test_short_arms_a_sell_limit_above_the_market():
    down = {**_UP, "ema20": 101.0, "ema50": 103.0, "ema200": 106.0,
            "plus_di": 14.0, "minus_di": 28.0, "rsi14": 42.0,
            "macd_hist": -0.6, "macd_hist_prev": -0.3}
    market = _decide(0.0, _tech(down, big="down"))
    waited = _decide(0.25, _tech(down, big="down"))
    assert market.direction == Direction.SHORT
    c = waited.conditional
    assert c is not None and c.order_type == "sell_limit"
    assert c.trigger_price > market.entry     # a short wants to sell HIGHER


def test_stop_does_not_move_so_risk_shrinks():
    """The whole point: same stop, better entry -> smaller R and a longer run to target."""
    market = _decide(0.0)
    waited = _decide(0.25)
    c = waited.conditional

    assert c.stop_loss == market.stop_loss          # structural level, unchanged
    assert c.take_profit == market.take_profit      # same objective

    risk_market = abs(market.entry - market.stop_loss)
    risk_waited = abs(c.trigger_price - c.stop_loss)
    assert risk_waited < risk_market                # less risked per unit

    rr_market = abs(market.take_profit - market.entry) / risk_market
    assert c.rr > rr_market                         # ...and a better payoff for it


def test_bigger_wait_asks_for_a_better_price():
    a, b = _decide(0.25).conditional, _decide(0.5).conditional
    assert b.trigger_price < a.trigger_price
    assert b.rr > a.rr


# --- guards ------------------------------------------------------------------------------------

def test_limit_beyond_the_stop_falls_back_to_market():
    """If the 'better' price would sit past the stop it isn't a trade — keep the market entry
    rather than arming something that can never be sized."""
    d = _decide(5.0)                                # absurdly far; would cross the stop
    assert d.direction == Direction.LONG
    assert d.conditional is None


def test_rationale_states_the_trade_off():
    d = _decide(0.25)
    r = d.rationale.lower()
    assert "arms a limit" in r
    assert "never comes back" in r                  # the cost is spelled out, not hidden


def test_confidence_is_carried_onto_the_arm():
    market = _decide(0.0)
    assert _decide(0.25).conditional.confidence == market.confidence


# --- the API cap -------------------------------------------------------------------------------

def test_route_rejects_a_setting_known_to_lose_money():
    """0.75 ATR lost money in BOTH halves of the backtest, so the endpoint refuses it."""
    import pytest
    from pydantic import ValidationError

    from app.api.settings_routes import WaitEntryRequest

    assert WaitEntryRequest(atr=0.25).atr == 0.25
    assert WaitEntryRequest().atr == 0.0
    with pytest.raises(ValidationError):
        WaitEntryRequest(atr=0.75)
    with pytest.raises(ValidationError):
        WaitEntryRequest(atr=-0.1)
