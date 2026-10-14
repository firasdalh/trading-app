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


def test_hybrid_never_auto_opens_ranging_fades():
    # Ranging fades lost in every test through the Hybrid gate (-37.9R over 73 trades), so the
    # auto-pilot no longer opens them at ANY confidence — not even at the engine's 0.68 fade cap,
    # which sits above the default 0.67 bar. Trend setups keep the configured bar.
    from app.agents.hybrid import _effective_min_conf
    from app.agents.orchestrator import _MR_CONF_CAP
    assert _effective_min_conf("trend", 0.67) == 0.67
    assert _effective_min_conf(None, 0.70) == 0.70
    assert not _MR_CONF_CAP > _effective_min_conf("mean_reversion", 0.67)
    assert not 1.0 > _effective_min_conf("mean_reversion", 0.50)


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


def test_mean_reversion_fades_price_channel_band():
    # No structural resistance nearby, but the bar tagged the UPPER price-channel band and closed back
    # inside with RSI in the (looser) fade band -> short the rejection to the mean.
    ind = {"atr14": 1.0, "rsi14": 68.0, "rsi14_prev": 70.0, "last_close": 100.0,
           "last_high": 101.2, "ema20": 98.0, "chan_upper": 101.0}
    out = _mean_reversion_decision(_base(), ind, _tf())  # no structural levels at all
    assert out.direction == Direction.SHORT
    assert out.take_profit == 98.0 and out.stop_loss > 101.0


def test_mean_reversion_uses_looser_rsi_band():
    # RSI 67 would NOT clear the old trend-extreme 75 gate, but DOES clear the MR fade band (66).
    ind = {"atr14": 1.0, "rsi14": 67.0, "last_close": 100.0, "last_high": 100.35, "ema20": 98.0}
    out = _mean_reversion_decision(_base(), ind, _tf(resistance=[100.3]))
    assert out.direction == Direction.SHORT


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


def test_mean_reversion_refuses_fade_against_higher_tf_trend():
    # A SHORT fade of resistance is a valid RANGE play — but NOT when the higher timeframe is in an
    # uptrend (that's a pullback, not a range). This is the USDJPY leak the backtest caught: ranging
    # shorts into a 1d uptrend went 0/11. The same setup with a neutral higher TF still fades.
    ind = {"atr14": 1.0, "rsi14": 80.0, "rsi14_prev": 82.0, "last_close": 100.0,
           "last_high": 100.35, "ema20": 98.0}
    assert _mean_reversion_decision(_base(), ind, _tf(resistance=[100.3]), "up").direction == Direction.NO_TRADE
    assert _mean_reversion_decision(_base(), ind, _tf(resistance=[100.3]), "sideways").direction == Direction.SHORT


def test_mean_reversion_refuses_long_fade_against_higher_tf_downtrend():
    # Mirror: a LONG fade of support is refused under a higher-TF downtrend (a falling knife), but
    # allowed when the higher TF isn't against it.
    ind = {"atr14": 1.0, "rsi14": 20.0, "rsi14_prev": 18.0, "last_close": 100.0,
           "last_low": 99.65, "ema20": 102.0}
    assert _mean_reversion_decision(_base(), ind, _tf(support=[99.7]), "down").direction == Direction.NO_TRADE
    assert _mean_reversion_decision(_base(), ind, _tf(support=[99.7]), "up").direction == Direction.LONG


def test_mean_reversion_skips_thin_reward():
    # Rejection at resistance + overbought, but the mean is barely below price -> reward too thin.
    ind = {"atr14": 1.0, "rsi14": 80.0, "rsi14_prev": 82.0, "last_close": 100.0,
           "last_high": 100.25, "ema20": 99.95}
    out = _mean_reversion_decision(_base(), ind, _tf(resistance=[100.2]))
    assert out.direction == Direction.NO_TRADE
