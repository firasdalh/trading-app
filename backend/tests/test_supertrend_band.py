"""SuperTrend + EMA20-band breakout strategy: the decision (all 4 branches) + the mode toggle."""
from __future__ import annotations

from app.agents.orchestrator import _supertrend_band_decision
from app.models.enums import AssetClass, Direction
from app.models.schemas import TradeProposal


def _base() -> TradeProposal:
    return TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _ind(dir_, st_line, ema_h, ema_l, last):
    return {"supertrend_dir": dir_, "supertrend": st_line,
            "ema20_high": ema_h, "ema20_low": ema_l, "last_close": last}


def test_long_above_band_in_uptrend():
    # SuperTrend up + close above EMA20-high -> LONG, stop on the ST line, ~3R backstop target.
    p = _supertrend_band_decision(_base(), _ind(1.0, 105.0, 108.0, 104.0, 110.0), "X")
    assert p.direction == Direction.LONG and p.strategy == "supertrend_band"
    assert p.entry == 110.0 and p.stop_loss == 105.0
    assert p.take_profit == 125.0  # 110 + 3*(110-105)


def test_short_below_band_in_downtrend():
    p = _supertrend_band_decision(_base(), _ind(-1.0, 95.0, 96.0, 92.0, 90.0), "X")
    assert p.direction == Direction.SHORT and p.stop_loss == 95.0
    assert p.take_profit == 75.0  # 90 - 3*(95-90)


def test_no_trade_inside_band():
    p = _supertrend_band_decision(_base(), _ind(1.0, 103.0, 108.0, 104.0, 106.0), "X")
    assert p.direction == Direction.NO_TRADE and "inside" in p.rationale.lower()


def test_no_trade_when_supertrend_disagrees():
    # SuperTrend UP but price broke DOWN below the low band -> no trade (sides don't match).
    p = _supertrend_band_decision(_base(), _ind(1.0, 104.0, 105.0, 95.0, 90.0), "X")
    assert p.direction == Direction.NO_TRADE and "match" in p.rationale.lower()


def test_st_band_mode_endpoint_persists(db_session):
    from app.api.settings_routes import StBandModeRequest, set_st_band_mode
    from app.core.state import get_or_create_settings

    on = set_st_band_mode(StBandModeRequest(enabled=True), session=db_session)
    assert on.app.st_band_mode is True
    assert get_or_create_settings(db_session).st_band_mode is True
    off = set_st_band_mode(StBandModeRequest(enabled=False), session=db_session)
    assert off.app.st_band_mode is False
