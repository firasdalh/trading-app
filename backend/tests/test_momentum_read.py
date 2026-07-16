"""AI momentum classifier + the deterministic engine's label->action mapping (enter/wait/reject/arm)."""
from __future__ import annotations

import app.agents.momentum_read as mr
from app.agents.momentum_read import MomentumRead, interpret_momentum
from app.agents.orchestrator import _momentum_action
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead, TradeProposal


def _tech(price=100.0, ema20=100.0, macd=-0.5, macdp=-0.2, rsi=45.0, atr=2.0,
          sup=98.0, res=106.0, swing_high=None, swing_low=None, tf="1h"):
    ind = {"last_close": price, "ema20": ema20, "atr14": atr, "macd_hist": macd,
           "macd_hist_prev": macdp, "rsi14": rsi, "adx": 22.0, "plus_di": 20.0, "minus_di": 25.0}
    if swing_high is not None:
        ind["swing_high"] = swing_high
    if swing_low is not None:
        ind["swing_low"] = swing_low
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe=tf, trend="up", indicators=ind, support_levels=[sup], resistance_levels=[res]),
        TimeframeRead(timeframe="4h", trend="up", indicators={"macd_hist": 0.8, "ema20": 100.0}),
    ])


def _base():
    return TradeProposal(symbol="X", asset_class=AssetClass.CRYPTO, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _read(cat, conf=0.8):
    return MomentumRead(category=cat, evidence="cited MACD/RSI/HTF facts", confidence=conf)


# ---------------------------------------------------------------- the classifier module

def test_interpret_none_without_llm(monkeypatch):
    monkeypatch.setattr(mr, "llm_available", lambda: False)
    t = _tech()
    assert interpret_momentum("AAA", Direction.LONG, t.timeframes[0].indicators, t, "1h") is None


def test_interpret_parses_category(monkeypatch):
    monkeypatch.setattr(mr, "llm_available", lambda: True)
    monkeypatch.setattr(mr, "analyze", lambda **k: _read("healthy_pullback", 0.72))
    mr._CACHE.clear()
    t = _tech()
    read = interpret_momentum("BBB", Direction.LONG, t.timeframes[0].indicators, t, "1h")
    assert read is not None and read.category == "healthy_pullback" and read.confidence == 0.72


def test_snapshot_carries_the_facts():
    t = _tech(macd=-0.5, macdp=-0.2, rsi=45.0)
    snap = mr._snapshot("X", Direction.LONG, t.timeframes[0].indicators, t, "1h")
    assert "MACD hist: -0.5" in snap and "RSI(14): 45.0" in snap and "Higher-TF (4h)" in snap


# ---------------------------------------------------------------- label -> action (engine decides)

def _act(monkeypatch, read, *, direction=Direction.LONG, price=100.0, ema20=100.0,
         momentum_ai=True, **tech_kw):
    if read is not None or momentum_ai:
        monkeypatch.setattr(mr, "interpret_momentum", lambda *a, **k: read)
    t = _tech(price=price, ema20=ema20, **tech_kw)
    return _momentum_action(_base(), direction, t.timeframes[0].indicators, t.timeframes[0],
                            t, t.timeframes[0].indicators["atr14"], price, momentum_ai, "X")


def test_action_fallback_when_ai_off(monkeypatch):
    action, prop = _act(monkeypatch, None, momentum_ai=False)
    assert action == "fallback" and prop is None


def test_action_fallback_when_interpret_none(monkeypatch):
    action, _ = _act(monkeypatch, None)
    assert action == "fallback"


def test_action_fallback_when_low_confidence(monkeypatch):
    action, _ = _act(monkeypatch, _read("healthy_pullback", 0.40))  # < _MOM_AI_MIN_CONF
    assert action == "fallback"


def test_action_probable_reversal_rejects(monkeypatch):
    action, prop = _act(monkeypatch, _read("probable_reversal"))
    assert action == "decided" and prop.conditional is None and prop.watch is True
    assert "reversal" in prop.rationale.lower()
    # the classification is surfaced to the UI ("What the analysis saw")
    assert prop.momentum_read and prop.momentum_read["category"] == "probable_reversal"


def test_action_weak_momentum_waits(monkeypatch):
    # Weak momentum -> the engine WAITS (watch, not a market entry). Whether a clean resumption stop
    # gets armed depends on level geometry (covered by _conditional_resumption's own tests).
    action, prop = _act(monkeypatch, _read("weak_momentum"), price=100.0, swing_high=104.0)
    assert action == "decided" and prop.watch is True and "weak momentum" in prop.rationale.lower()


def test_action_healthy_at_value_enters(monkeypatch):
    # price within ~1 ATR of value -> the engine takes the normal MARKET entry (fall through).
    action, prop = _act(monkeypatch, _read("healthy_pullback"), price=100.5, ema20=100.0)
    assert action == "enter" and prop is None


def test_action_healthy_stretched_arms_dip_limit(monkeypatch):
    # price 105 is > ema20(100) + 1*ATR(2) -> stretched -> arm a buy-the-dip LIMIT at value.
    action, prop = _act(monkeypatch, _read("healthy_pullback"), price=105.0, ema20=100.0,
                        res=108.0, swing_low=99.0)
    assert action == "decided" and prop.conditional is not None
    assert prop.conditional.order_type == "buy_limit"


# ---------------------------------------------------------------- settings endpoint

def test_ai_momentum_read_endpoint_persists(db_session):
    from app.api.settings_routes import AiMomentumReadRequest, set_ai_momentum_read
    from app.core.state import get_or_create_settings

    off = set_ai_momentum_read(AiMomentumReadRequest(enabled=False), session=db_session)
    assert off.app.ai_momentum_read is False
    assert get_or_create_settings(db_session).ai_momentum_read is False
    on = set_ai_momentum_read(AiMomentumReadRequest(enabled=True), session=db_session)
    assert on.app.ai_momentum_read is True
