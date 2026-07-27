"""AI regime-texture classifier + the deterministic engine's label->regime mapping (promote/demote)."""
from __future__ import annotations

import app.agents.regime_read as rr
from app.agents.orchestrator import _regime_refine
from app.agents.regime_read import RegimeRead, interpret_regime
from app.models.enums import AssetClass, Direction
from app.models.schemas import TechnicalRead, TimeframeRead, TradeProposal


def _tech(adx=21.0, tf="1h"):
    ind = {"last_close": 100.0, "atr14": 2.0, "adx": adx, "plus_di": 24.0, "minus_di": 18.0,
           "chan_slope": 0.05, "chan_r2": 0.7, "chan_pos": 0.6, "vol_atr_ratio": 1.1,
           "structure": 1.0, "choch": 0.0, "vol_trend": 1.0,
           "ema20": 101.0, "ema50": 100.0, "ema200": 98.0}
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe=tf, trend="up", indicators=ind, support_levels=[98.0], resistance_levels=[106.0]),
        TimeframeRead(timeframe="4h", trend="up", indicators={"ema20": 101.0, "ema50": 100.0, "ema200": 98.0}),
    ])


def _base():
    return TradeProposal(symbol="X", asset_class=AssetClass.CRYPTO, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _read(cat, conf=0.8):
    return RegimeRead(category=cat, evidence="ADX 21, +DI>-DI, r2 0.7, EMA stack bullish", confidence=conf)


# ---------------------------------------------------------------- the classifier module

def test_interpret_none_without_llm(monkeypatch):
    monkeypatch.setattr(rr, "llm_available", lambda: False)
    t = _tech()
    assert interpret_regime("AAA", t.timeframes[0].indicators, t, "1h") is None


def test_interpret_parses_category(monkeypatch):
    monkeypatch.setattr(rr, "llm_available", lambda: True)
    monkeypatch.setattr(rr, "analyze", lambda **k: _read("emerging_trend", 0.72))
    rr._CACHE.clear()
    t = _tech()
    read = interpret_regime("BBB", t.timeframes[0].indicators, t, "1h")
    assert read is not None and read.category == "emerging_trend" and read.confidence == 0.72


def test_snapshot_carries_the_facts():
    t = _tech(adx=21.0)
    snap = rr._snapshot("X", t.timeframes[0].indicators, t, "1h")
    assert "ADX: 21.0" in snap and "fit r2 0.7" in snap and "EMA stack (entry TF): bullish" in snap


# ---------------------------------------------------------------- label -> regime (engine decides)

def _refine(monkeypatch, read, *, regime_ai=True):
    if read is not None or regime_ai:
        monkeypatch.setattr(rr, "interpret_regime", lambda *a, **k: read)
    t = _tech()
    base = _base()
    out = _regime_refine(base, t.timeframes[0].indicators, t, t.timeframes[0], regime_ai, "X")
    return out, base


def test_refine_none_when_ai_off(monkeypatch):
    out, _ = _refine(monkeypatch, None, regime_ai=False)
    assert out is None


def test_refine_none_when_interpret_none(monkeypatch):
    out, _ = _refine(monkeypatch, None)
    assert out is None


def test_refine_none_when_low_confidence(monkeypatch):
    out, _ = _refine(monkeypatch, _read("emerging_trend", 0.40))  # < _REGIME_AI_MIN_CONF
    assert out is None


def test_refine_emerging_trend_promotes(monkeypatch):
    out, base = _refine(monkeypatch, _read("emerging_trend"))
    assert out == "trending"
    assert base.regime_read and base.regime_read["category"] == "emerging_trend"


def test_refine_choppy_range_demotes(monkeypatch):
    out, base = _refine(monkeypatch, _read("choppy_range"))
    assert out == "ranging" and base.regime_read["category"] == "choppy_range"


def test_refine_transition_stands_pat(monkeypatch):
    out, base = _refine(monkeypatch, _read("transition"))
    assert out is None and base.regime_read["category"] == "transition"


# ---------------------------------------------------------------- settings endpoint

def test_ai_regime_read_endpoint_persists(db_session):
    from app.api.settings_routes import AiRegimeReadRequest, set_ai_regime_read
    from app.core.state import get_or_create_settings

    off = set_ai_regime_read(AiRegimeReadRequest(enabled=False), session=db_session)
    assert off.app.ai_regime_read is False
    assert get_or_create_settings(db_session).ai_regime_read is False
    on = set_ai_regime_read(AiRegimeReadRequest(enabled=True), session=db_session)
    assert on.app.ai_regime_read is True
