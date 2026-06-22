"""Regime policy + ranging mean-reversion strategy."""
from app.agents.orchestrator import _mean_reversion_decision, regime_policy
from app.models.enums import AssetClass, Direction
from app.models.schemas import TimeframeRead, TradeProposal


def _base() -> TradeProposal:
    return TradeProposal(symbol="TEST", asset_class=AssetClass.INDEX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _tf(resistance=None, support=None) -> TimeframeRead:
    return TimeframeRead(timeframe="1h", trend="sideways", support_levels=support or [],
                         resistance_levels=resistance or [], indicators={}, patterns=[], comment="")


def test_entry_timeframe_matched_by_param_not_position():
    # The LLM technical path can return timeframes in any order, so the entry TF must be picked by
    # matching the requested timeframe — not by position. Here the list is REVERSED (1d first) and
    # only the 1h (entry) is ranging; the proposal's regime must come from the 1h read.
    from datetime import datetime, timezone

    from app.agents.orchestrator import _deterministic_decision
    from app.models.enums import TradingBias
    from app.models.schemas import FundamentalRead, TechnicalRead

    def tf(name, adx):
        return TimeframeRead(timeframe=name, trend="sideways", support_levels=[],
                             resistance_levels=[], indicators={"adx": adx, "last_close": 100.0},
                             patterns=[], comment="")

    tech = TechnicalRead(symbol="X", timeframes=[tf("1d", 30.0), tf("1h", 15.0)],
                         overall_trend="up", confidence=0.5, notes="")
    fund = FundamentalRead(symbol="X", bias=TradingBias.NEUTRAL)
    prop = _deterministic_decision("X", AssetClass.INDEX, "1h", tech, fund,
                                   datetime(2026, 6, 23, tzinfo=timezone.utc))
    assert prop.regime == "ranging"   # read the 1h (ADX 15), not the 1d (ADX 30)


def test_hybrid_lowers_bar_for_ranging_fades():
    # The Hybrid takes a ranging fade on a lower confidence bar than a trend trade.
    from app.agents.hybrid import _MR_MIN_CONFIDENCE, _effective_min_conf
    assert _effective_min_conf("trend", 0.70) == 0.70
    assert _effective_min_conf("mean_reversion", 0.70) == _MR_MIN_CONFIDENCE
    assert _effective_min_conf(None, 0.70) == 0.70


def test_regime_policy_mapping():
    assert regime_policy("trending")["strategy"] == "trend"
    assert regime_policy("ranging")["strategy"] == "mean_reversion"
    assert regime_policy("volatile")["strategy"] == "stand_aside"
    assert regime_policy("moderate")["strategy"] == "trend"


def test_mean_reversion_fades_resistance_when_rejected_and_overbought():
    # Bar's high tagged resistance (100.3) then CLOSED back at 100.0, overbought + RSI turning down,
    # mean below -> SHORT the rejection to the mean.
    ind = {"atr14": 1.0, "rsi14": 80.0, "rsi14_prev": 82.0, "last_close": 100.0,
           "last_high": 100.35, "ema20": 98.0}
    out = _mean_reversion_decision(_base(), ind, _tf(resistance=[100.3]))
    assert out.direction == Direction.SHORT
    assert out.regime == "ranging" and out.strategy == "mean_reversion"
    assert out.entry == 100.0 and out.stop_loss > 100.3 and out.take_profit == 98.0
    assert 0 < out.confidence <= 0.68


def test_mean_reversion_fades_support_when_rejected_and_oversold():
    # Bar's low tagged support (99.7) then closed back at 100.0, oversold + RSI turning up -> LONG.
    ind = {"atr14": 1.0, "rsi14": 20.0, "rsi14_prev": 18.0, "last_close": 100.0,
           "last_low": 99.65, "ema20": 102.0}
    out = _mean_reversion_decision(_base(), ind, _tf(support=[99.7]))
    assert out.direction == Direction.LONG
    assert out.stop_loss < 99.7 and out.take_profit == 102.0


def test_mean_reversion_needs_rejection_not_just_proximity():
    # Near resistance + overbought, but the wick never tagged the edge (last_high < res) -> no fade
    # (this is exactly the "don't fade a level that hasn't been tested/held" guard).
    ind = {"atr14": 1.0, "rsi14": 80.0, "rsi14_prev": 78.0, "last_close": 100.0,
           "last_high": 100.1, "ema20": 98.0}
    out = _mean_reversion_decision(_base(), ind, _tf(resistance=[100.3]))
    assert out.direction == Direction.NO_TRADE


def test_mean_reversion_stands_aside_mid_range():
    # Not at an edge and RSI neutral -> no fade.
    ind = {"atr14": 1.0, "rsi14": 50.0, "last_close": 100.0, "last_high": 100.1,
           "last_low": 99.9, "ema20": 100.0}
    out = _mean_reversion_decision(_base(), ind, _tf(resistance=[110.0], support=[90.0]))
    assert out.direction == Direction.NO_TRADE


def test_mean_reversion_skips_thin_reward():
    # Rejection at resistance + overbought, but the mean is barely below price -> reward too thin.
    ind = {"atr14": 1.0, "rsi14": 80.0, "rsi14_prev": 82.0, "last_close": 100.0,
           "last_high": 100.25, "ema20": 99.95}
    out = _mean_reversion_decision(_base(), ind, _tf(resistance=[100.2]))
    assert out.direction == Direction.NO_TRADE
