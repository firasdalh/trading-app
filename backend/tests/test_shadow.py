"""Shadow scorecard: the grader (win/loss/timeout/no_fill), the recorder, and end-to-end evaluate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.brokers.registry as registry
import app.data.ohlcv_cache as ohlcv_cache
from app.agents.shadow import (
    _grade_directional,
    _missed_move,
    evaluate_shadows,
    record_shadow,
    scorecard,
    shadow_note,
)
from app.models.enums import AssetClass, Direction
from app.models.schemas import Candle, OHLCVSeries, TradeProposal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _c(i, hi, lo, close=None):
    return Candle(ts=NOW + timedelta(hours=i + 1), open=(close or (hi + lo) / 2),
                  high=hi, low=lo, close=close if close is not None else (hi + lo) / 2, volume=1000)


# ---- the grader ----

def test_long_win():
    bars = [_c(0, 101, 99), _c(1, 105, 100, 104)]   # reaches target 104
    out, r = _grade_directional("long", 100, 98, 104, bars, is_arm=False, order_type=None)
    assert out == "win" and r == 2.0                 # (104-100)/(100-98)


def test_long_loss():
    bars = [_c(0, 101, 97)]                           # low 97 <= stop 98
    out, r = _grade_directional("long", 100, 98, 104, bars, is_arm=False, order_type=None)
    assert out == "loss" and r == -1.0


def test_long_timeout_marks_to_market():
    bars = [_c(0, 101, 99), _c(1, 102, 100, 101)]     # neither stop nor target -> timeout at 101
    out, r = _grade_directional("long", 100, 98, 104, bars, is_arm=False, order_type=None)
    assert out == "timeout" and r == 0.5              # (101-100)/2


def test_short_win():
    bars = [_c(0, 101, 95, 96)]                        # low 95 <= target 96
    out, r = _grade_directional("short", 100, 102, 96, bars, is_arm=False, order_type=None)
    assert out == "win" and r == 2.0                  # (100-96)/(102-100)


def test_arm_buy_stop_no_fill():
    bars = [_c(0, 101, 99), _c(1, 101.5, 99.5)]       # never reaches trigger 102
    out, r = _grade_directional("long", 102, 100, 108, bars, is_arm=True, order_type="buy_stop")
    assert out == "no_fill" and r == 0.0


def test_arm_buy_stop_fill_then_win():
    bars = [_c(0, 101, 99), _c(1, 102.5, 101, 102), _c(2, 109, 102, 108)]  # fills at 102, hits 108
    out, r = _grade_directional("long", 102, 100, 108, bars, is_arm=True, order_type="buy_stop")
    assert out == "win" and r == 3.0                  # (108-102)/(102-100)


def test_missed_move():
    assert _missed_move(100, 2.0, [_c(0, 104.1, 99)]) == "up"     # +2 ATR up
    assert _missed_move(100, 2.0, [_c(0, 100.5, 95.9)]) == "down"
    assert _missed_move(100, 2.0, [_c(0, 101, 99)]) == "none"


# ---- recorder + end-to-end evaluate ----

def _prop(direction, entry, stop, target, conf=0.7):
    return TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=direction, entry=entry, stop_loss=stop, take_profit=target,
                         confidence=conf, regime="trending")


def test_record_and_evaluate_win(db_session, monkeypatch):
    ai = _prop(Direction.LONG, 100.0, 98.0, 104.0)
    det = _prop(Direction.NO_TRADE, None, None, None, conf=0.0)  # engine stood aside; AI opened
    record_shadow(db_session, "EURUSDm", "forex", "1h", NOW, 100.0, 2.0, ai, det)

    # future candles: hits target 104 on bar 2, plus filler so len(after) >= 3
    future = [_c(0, 101, 99), _c(1, 105, 100, 104), _c(2, 106, 103, 105), _c(3, 106, 103, 105)]
    series = OHLCVSeries(symbol="EURUSDm", timeframe="1h", candles=future)
    monkeypatch.setattr(registry, "get_broker_for", lambda *a, **k: SimpleNamespace(name="stub"))
    monkeypatch.setattr(ohlcv_cache, "get_ohlcv_cached", lambda *a, **k: series)

    graded = evaluate_shadows(db_session)
    assert graded == 1
    card = scorecard(db_session)
    assert card["ai"]["wins"] == 1 and card["ai"]["win_rate"] == 1.0
    assert card["deterministic"]["stand_aside"] == 1   # engine stood aside on this one
    note = shadow_note(db_session)
    assert note is not None and "1 AI trades graded" in note


def test_record_stand_aside_missed(db_session, monkeypatch):
    ai = _prop(Direction.NO_TRADE, None, None, None, conf=0.0)   # AI stood aside
    det = _prop(Direction.NO_TRADE, None, None, None, conf=0.0)
    record_shadow(db_session, "EURUSDm", "forex", "1h", NOW, 100.0, 2.0, ai, det)

    # price ran +2 ATR up -> a missed long
    future = [_c(0, 101, 99), _c(1, 104.5, 100, 104), _c(2, 105, 103, 104)]
    series = OHLCVSeries(symbol="EURUSDm", timeframe="1h", candles=future)
    monkeypatch.setattr(registry, "get_broker_for", lambda *a, **k: SimpleNamespace(name="stub"))
    monkeypatch.setattr(ohlcv_cache, "get_ohlcv_cached", lambda *a, **k: series)

    evaluate_shadows(db_session)
    card = scorecard(db_session)
    assert card["ai"]["stand_aside"] == 1 and card["ai"]["stand_aside_missed"] == 1


def test_scorecard_respects_journal_reset(db_session, monkeypatch):
    """'Start fresh' (journal reset) also resets the shadow scorecard — rows before the marker drop out."""
    from datetime import timedelta

    from app.core.state import get_or_create_settings

    ai = _prop(Direction.LONG, 100.0, 98.0, 104.0)
    det = _prop(Direction.NO_TRADE, None, None, None, conf=0.0)
    record_shadow(db_session, "EURUSDm", "forex", "1h", NOW, 100.0, 2.0, ai, det)
    future = [_c(0, 101, 99), _c(1, 105, 100, 104), _c(2, 106, 103, 105), _c(3, 106, 103, 105)]
    series = OHLCVSeries(symbol="EURUSDm", timeframe="1h", candles=future)
    monkeypatch.setattr(registry, "get_broker_for", lambda *a, **k: SimpleNamespace(name="stub"))
    monkeypatch.setattr(ohlcv_cache, "get_ohlcv_cached", lambda *a, **k: series)
    evaluate_shadows(db_session)
    assert scorecard(db_session)["evaluated"] == 1        # counted before any reset

    # Reset marker set AFTER the row's timestamp -> the old row is excluded.
    get_or_create_settings(db_session).journal_reset_at = NOW + timedelta(hours=1)
    db_session.commit()
    card = scorecard(db_session)
    assert card["evaluated"] == 0 and card["pending"] is False
