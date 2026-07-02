"""SuperTrend + EMA20-band breakout with STRUCTURE-based (S/R) stop & target + the mode toggle."""
from __future__ import annotations

from app.agents.orchestrator import _supertrend_band_decision
from app.models.enums import AssetClass, Direction
from app.models.schemas import TimeframeRead, TradeProposal


def _base() -> TradeProposal:
    return TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _inputs(dir_, ema_h, ema_l, last, atr=2.0, support=None, resistance=None):
    ind = {"supertrend_dir": dir_, "ema20_high": ema_h, "ema20_low": ema_l,
           "last_close": last, "atr14": atr}
    tf0 = TimeframeRead(
        timeframe="1h", trend="up" if dir_ > 0 else "down", indicators=ind,
        support_levels=[support] if support is not None else [],
        resistance_levels=[resistance] if resistance is not None else [],
    )
    return ind, tf0


def test_long_uses_structure_stop_and_target():
    # SuperTrend up + close above EMA20-high -> LONG; stop just below support, target at resistance.
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=110, atr=2.0, support=105, resistance=120)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.LONG and p.strategy == "supertrend_band"
    assert p.stop_loss == 104.6      # support 105 - 0.2*ATR(2)
    assert p.take_profit == 120.0    # nearest resistance
    assert "support" in p.rationale and "resistance" in p.rationale


def test_short_uses_structure_stop_and_target():
    ind, tf0 = _inputs(-1.0, ema_h=96, ema_l=92, last=90, atr=2.0, support=80, resistance=95)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.SHORT
    assert p.stop_loss == 95.4       # resistance 95 + 0.2*ATR
    assert p.take_profit == 80.0     # nearest support


def test_no_trade_inside_band():
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=106, atr=2.0, support=100, resistance=120)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.NO_TRADE and "inside" in p.rationale.lower()


def test_no_trade_when_supertrend_disagrees():
    # SuperTrend UP but price broke DOWN below the low band -> sides don't match.
    ind, tf0 = _inputs(1.0, ema_h=105, ema_l=100, last=90, atr=2.0, support=80, resistance=110)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.NO_TRADE and "match" in p.rationale.lower()


def test_no_trade_when_target_too_close():
    # Resistance just above entry -> < 1.5R to target -> skip.
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=110, atr=2.0, support=105, resistance=111)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.NO_TRADE and "r" in p.rationale.lower()


def test_st_band_mode_endpoint_persists(db_session):
    from app.api.settings_routes import StBandModeRequest, set_st_band_mode
    from app.core.state import get_or_create_settings

    on = set_st_band_mode(StBandModeRequest(enabled=True), session=db_session)
    assert on.app.st_band_mode is True
    assert get_or_create_settings(db_session).st_band_mode is True
    off = set_st_band_mode(StBandModeRequest(enabled=False), session=db_session)
    assert off.app.st_band_mode is False
