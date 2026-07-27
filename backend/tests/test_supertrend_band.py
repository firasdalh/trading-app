"""SuperTrend + EMA20-band breakout with STRUCTURE-based (S/R) stop & target + the mode toggle."""
from __future__ import annotations

from app.agents.orchestrator import _supertrend_band_decision
from app.models.enums import AssetClass, Direction
from app.models.schemas import TimeframeRead, TradeProposal


def _base() -> TradeProposal:
    return TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _inputs(dir_, ema_h, ema_l, last, atr=2.0, support=None, resistance=None, bars_since_flip=1.0,
            ema20=None, st_line=None):
    # Default the SuperTrend LINE close to price (~0.5xATR) so the early-entry (distance-from-line) check
    # passes; a test that wants a "late" entry passes a far ``st_line``.
    if st_line is None:
        st_line = (last - 0.5 * atr) if dir_ > 0 else (last + 0.5 * atr)
    ind = {"supertrend_dir": dir_, "ema20_high": ema_h, "ema20_low": ema_l,
           "last_close": last, "atr14": atr, "supertrend": st_line,
           "supertrend_bars_since_flip": bars_since_flip}
    if ema20 is not None:
        ind["ema20"] = ema20
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


def test_no_trade_when_far_from_line():
    # Valid long setup, but the close is far from the SuperTrend line (line 104, close 110 = 3xATR) ->
    # late / run-away entry with a wide stop -> skip.
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=110, atr=2.0, support=105, resistance=120,
                       st_line=104.0)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.NO_TRADE and "line" in p.rationale.lower()


def test_trades_old_flip_if_close_to_line():
    # An OLD flip (20 bars) no longer blocks the trade: because the close is still near the SuperTrend
    # line (0.5xATR), it's an early-by-PRICE entry and trades — the whole point of the line-distance rule
    # replacing the rigid bar count.
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=110, atr=2.0, support=105, resistance=120,
                       bars_since_flip=20.0, st_line=109.0)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.LONG


def test_no_trade_when_breakout_stretched_from_value():
    # Fresh flip + valid long, but the close is far above value (EMA20) -> chasing the breakout -> skip.
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=120, atr=2.0, support=105, resistance=140, ema20=106)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.NO_TRADE and "stretched" in p.rationale.lower()


def test_long_trades_when_not_stretched():
    # Same setup but the close sits just above value (~1.5xATR) -> not a chase -> takes the trade.
    ind, tf0 = _inputs(1.0, ema_h=108, ema_l=104, last=109, atr=2.0, support=105, resistance=120, ema20=106)
    p = _supertrend_band_decision(_base(), ind, tf0, "X")
    assert p.direction == Direction.LONG


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
