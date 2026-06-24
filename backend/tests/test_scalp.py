"""15m SCALP strategy (SCMS): the parsimonious trend-pullback scalp + its hard gates."""
from __future__ import annotations

from datetime import datetime, timezone

from app.agents.orchestrator import _deterministic_decision, _scalp_decision
from app.models.enums import AssetClass, Direction
from app.models.schemas import FundamentalRead, TechnicalRead, TimeframeRead, TradeProposal
from app.models.enums import TradingBias

ACTIVE = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)   # 14:00 UTC -> London/NY = active (FX)
THIN = datetime(2026, 6, 24, 2, 0, tzinfo=timezone.utc)      # 02:00 UTC -> thin (FX, non-JPY)


def _base() -> TradeProposal:
    return TradeProposal(symbol="EURUSD", asset_class=AssetClass.FOREX, timeframe="15m",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _tf(resistance=None, support=None) -> TimeframeRead:
    return TimeframeRead(timeframe="15m", trend="up", support_levels=support or [],
                         resistance_levels=resistance or [], indicators={}, patterns=[], comment="")


def _long_ind(**over) -> dict:
    ind = {"atr14": 0.0010, "last_close": 1.1002, "ema20": 1.1000, "ema50": 1.0990,
           "ema200": 1.0980, "macd_hist": 0.0002, "last_low": 1.0999, "last_high": 1.1003,
           "swing_low": 1.0995, "recent_low": 1.0994, "vol_ratio": 1.2,
           "rsi14": 48.0, "rsi14_prev": 45.0}   # RSI turning UP from the pullback
    ind.update(over)
    return ind


def _scalp(ind, *, macro="up", regime="trending", now=ACTIVE, tf=None):
    return _scalp_decision(_base(), ind, tf or _tf(resistance=[1.1030]), macro, regime,
                           AssetClass.FOREX, "EURUSD", now)


def test_scalp_long_pullback_continuation():
    out = _scalp(_long_ind())
    assert out.direction == Direction.LONG and out.strategy == "scalp"
    assert out.stop_loss < 1.0995 and out.take_profit == 1.1030   # stop beyond pullback, TP at resistance
    assert 0.5 < out.confidence <= 0.9


def test_scalp_skips_ranging_regime():
    out = _scalp(_long_ind(), regime="ranging")
    assert out.direction == Direction.NO_TRADE and "standing aside" in out.rationale.lower()


def test_scalp_skips_thin_session():
    out = _scalp(_long_ind(), now=THIN)
    assert out.direction == Direction.NO_TRADE and "session" in out.rationale.lower()


def test_scalp_needs_rsi_turning_up():
    # Price at value but RSI still FALLING (momentum hasn't turned) -> no entry (waits).
    out = _scalp(_long_ind(rsi14=45.0, rsi14_prev=48.0))
    assert out.direction == Direction.NO_TRADE and "waiting" in out.rationale.lower()


def test_scalp_needs_pullback_to_value():
    # Price is extended ABOVE the value zone (not a pullback) -> waits.
    out = _scalp(_long_ind(last_close=1.1010))
    assert out.direction == Direction.NO_TRADE


def test_scalp_does_not_fight_higher_tf():
    # 15m up but the higher timeframe is DOWN -> stand aside (no counter-macro scalp).
    out = _scalp(_long_ind(), macro="down")
    assert out.direction == Direction.NO_TRADE


def test_scalp_skips_when_stop_too_wide():
    # The invalidation swing is > 2xATR away -> not a scalp.
    out = _scalp(_long_ind(swing_low=1.0970, recent_low=1.0970))
    assert out.direction == Direction.NO_TRADE and "wide" in out.rationale.lower()


def test_scalp_skips_when_target_too_close():
    # Nearest resistance gives < 1.3R -> no room, stand aside.
    out = _scalp(_long_ind(), tf=_tf(resistance=[1.1006]))
    assert out.direction == Direction.NO_TRADE and "room" in out.rationale.lower()


def test_scalp_short_mirror():
    ind = {"atr14": 0.0010, "last_close": 1.1002, "ema20": 1.1000, "ema50": 1.1010,
           "ema200": 1.1020, "macd_hist": -0.0002, "last_high": 1.1003, "last_low": 1.0999,
           "swing_high": 1.1005, "recent_high": 1.1006, "vol_ratio": 1.2,
           "rsi14": 52.0, "rsi14_prev": 55.0}   # downtrend, RSI turning DOWN at value
    out = _scalp_decision(_base(), ind, _tf(support=[1.0970]), "down", "trending",
                          AssetClass.FOREX, "EURUSD", ACTIVE)
    assert out.direction == Direction.SHORT and out.stop_loss > 1.1005 and out.take_profit < 1.0998


def test_deterministic_decision_routes_to_scalp():
    tech = TechnicalRead(symbol="X", overall_trend="sideways", confidence=0.5, timeframes=[
        TimeframeRead(timeframe="15m", trend="sideways",
                      indicators={"adx": 10.0, "atr14": 1.0, "last_close": 100.0, "ema20": 100.0},
                      support_levels=[], resistance_levels=[])])
    fund = FundamentalRead(symbol="X", bias=TradingBias.NEUTRAL)
    out = _deterministic_decision("X", AssetClass.FOREX, "15m", tech, fund, ACTIVE, scalp=True)
    assert out.strategy == "scalp"   # routed to the scalp path (ranging -> stands aside)
