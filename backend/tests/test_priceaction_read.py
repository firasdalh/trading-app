"""AI price-action classifier + the deterministic engine's label->action mapping (wait/enter-through)."""
from __future__ import annotations

import app.agents.priceaction_read as pa
from app.agents.orchestrator import _level_action
from app.agents.priceaction_read import PriceActionRead, interpret_price_action
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead, TradeProposal


def _tech(price=100.0, tf="1h"):
    ind = {"last_close": price, "atr14": 2.0, "macd_hist": 0.6, "macd_hist_prev": 0.3,
           "rsi14": 58.0, "rsi14_prev": 55.0, "adx": 26.0, "plus_di": 27.0, "minus_di": 15.0,
           "chan_r2": 0.7, "chan_pos": 0.9, "vol_trend": 1.0, "structure": 1.0, "choch": 0.0}
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe=tf, trend="up", indicators=ind, support_levels=[98.0], resistance_levels=[103.0]),
    ])


def _base():
    return TradeProposal(symbol="X", asset_class=AssetClass.CRYPTO, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _read(cat, conf=0.8):
    return PriceActionRead(category=cat, evidence="MACD building, volume expanding, price at upper band",
                           confidence=conf)


# ---------------------------------------------------------------- the classifier module

def test_interpret_none_without_llm(monkeypatch):
    monkeypatch.setattr(pa, "llm_available", lambda: False)
    t = _tech()
    assert interpret_price_action("AAA", Direction.LONG, 103.0, t.timeframes[0].indicators, "1h") is None


def test_interpret_parses_category(monkeypatch):
    monkeypatch.setattr(pa, "llm_available", lambda: True)
    monkeypatch.setattr(pa, "analyze", lambda **k: _read("likely_break", 0.72))
    pa._CACHE.clear()
    t = _tech()
    read = interpret_price_action("BBB", Direction.LONG, 103.0, t.timeframes[0].indicators, "1h")
    assert read is not None and read.category == "likely_break" and read.confidence == 0.72


def test_snapshot_carries_the_facts():
    t = _tech(price=100.0)
    snap = pa._snapshot("X", Direction.LONG, 103.0, t.timeframes[0].indicators, "1h")
    assert "Opposing level (resistance overhead): 103.0" in snap and "MACD hist: 0.6" in snap
    assert "distance from price: 1.5 ATR" in snap  # |103-100|/2


# ---------------------------------------------------------------- label -> action (engine decides)

def _act(monkeypatch, read, *, direction=Direction.LONG, level=103.0, priceaction_ai=True):
    if read is not None or priceaction_ai:
        monkeypatch.setattr(pa, "interpret_price_action", lambda *a, **k: read)
    t = _tech()
    base = _base()
    return _level_action(base, direction, level, t.timeframes[0].indicators, t, t.timeframes[0],
                         priceaction_ai, "X"), base


def test_action_fallback_when_ai_off(monkeypatch):
    (action, prop), _ = _act(monkeypatch, None, priceaction_ai=False)
    assert action == "fallback" and prop is None


def test_action_fallback_when_interpret_none(monkeypatch):
    (action, _p), _ = _act(monkeypatch, None)
    assert action == "fallback"


def test_action_fallback_when_low_confidence(monkeypatch):
    (action, _p), _ = _act(monkeypatch, _read("likely_break", 0.40))  # < _PA_AI_MIN_CONF
    assert action == "fallback"


def test_action_likely_break_enters_through(monkeypatch):
    (action, prop), base = _act(monkeypatch, _read("likely_break"))
    assert action == "enter" and prop is None
    assert base.priceaction_read and base.priceaction_read["category"] == "likely_break"


def test_action_likely_reject_waits(monkeypatch):
    (action, prop), _ = _act(monkeypatch, _read("likely_reject"))
    assert action == "decided" and prop.watch is True and "rejecting" in prop.rationale.lower()
    assert prop.priceaction_read["category"] == "likely_reject"


def test_action_indecision_waits(monkeypatch):
    (action, prop), _ = _act(monkeypatch, _read("indecision"))
    assert action == "decided" and prop.watch is True and "undecided" in prop.rationale.lower()


# ---------------------------------------------------------------- settings endpoint

def test_ai_priceaction_read_endpoint_persists(db_session):
    from app.api.settings_routes import AiPriceActionReadRequest, set_ai_priceaction_read
    from app.core.state import get_or_create_settings

    off = set_ai_priceaction_read(AiPriceActionReadRequest(enabled=False), session=db_session)
    assert off.app.ai_priceaction_read is False
    assert get_or_create_settings(db_session).ai_priceaction_read is False
    on = set_ai_priceaction_read(AiPriceActionReadRequest(enabled=True), session=db_session)
    assert on.app.ai_priceaction_read is True
