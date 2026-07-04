"""AI-led decision path: the AI Orchestrator decides; thin guardrails + the Risk Manager police it.

Covers the mapping (LLM decision -> TradeProposal), each capital-protective guardrail, and the
fallbacks (LLM off / LLM failure -> the deterministic engine)."""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.orchestrator as orch
from app.agents.orchestrator import run_orchestrator
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import (
    FundamentalRead,
    TechnicalRead,
    TimeframeRead,
    TradeDecisionLLM,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ind(trend: str, entry: float, atr_v: float) -> dict:
    if trend == "up":
        e20, e50 = entry + 1, entry - 1
    elif trend == "down":
        e20, e50 = entry - 1, entry + 1
    else:
        e20 = e50 = entry
    return {"last_close": entry, "atr14": atr_v, "adx": 30.0,
            "macd_hist": 1.0 if trend == "up" else -1.0, "ema20": e20, "ema50": e50, "vol_ratio": 1.3}


def _tech(entry: float = 100.0, atr_v: float = 2.0, trend: str = "up", macro: str = "up") -> TechnicalRead:
    return TechnicalRead(symbol="X", overall_trend=trend, confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend=trend, indicators=_ind(trend, entry, atr_v),
                      support_levels=[entry - 5], resistance_levels=[entry + 5]),
        TimeframeRead(timeframe="1d", trend=macro, indicators=_ind(macro, entry, atr_v)),
    ])


def _fund() -> FundamentalRead:
    return FundamentalRead(symbol="X", bias=TradingBias.NEUTRAL)


def _decision(**kw) -> TradeDecisionLLM:
    base = dict(action="long", conviction=0.7, entry=100.0, stop_loss=97.0, take_profit=106.0,
                rationale="clean pullback in an uptrend", key_risks=["news"])
    base.update(kw)
    return TradeDecisionLLM(**base)


def _patch_llm(monkeypatch, decision):
    """The AI is enabled+available and `analyze` returns `decision` (or raises via None)."""
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **kwargs: decision)


def _run(ai_led=True, use_llm=True):
    return run_orchestrator("X", AssetClass.FOREX, "1h", _tech(), _fund(), now=NOW,
                            use_llm=use_llm, ai_led=ai_led)


# ---- the AI decides ----

def test_ai_led_long_maps_to_proposal(monkeypatch):
    _patch_llm(monkeypatch, _decision())
    p = _run()
    assert p.direction == Direction.LONG and p.strategy == "ai" and p.review_decision == "ai"
    assert p.confidence == 0.7
    assert p.entry == 100.0 and p.stop_loss == 97.0 and p.take_profit == 106.0  # market entry @ last
    assert "AI-led LONG" in p.rationale


def test_ai_led_stand_aside(monkeypatch):
    _patch_llm(monkeypatch, _decision(action="stand_aside", entry=None, stop_loss=None, take_profit=None))
    p = _run()
    assert p.direction == Direction.NO_TRADE and p.strategy == "stand_aside"
    assert "stood aside" in p.rationale.lower()


# ---- guardrails block bad AI calls ----

def test_guardrail_low_rr_blocks(monkeypatch):
    _patch_llm(monkeypatch, _decision(take_profit=101.0))  # risk 3, reward 1 -> 0.33R
    p = _run()
    assert p.direction == Direction.NO_TRADE and "reward:risk" in p.rationale


def test_guardrail_wrong_side_blocks(monkeypatch):
    _patch_llm(monkeypatch, _decision(stop_loss=105.0))  # stop above entry for a long
    p = _run()
    assert p.direction == Direction.NO_TRADE and "wrong side" in p.rationale


def test_guardrail_against_macro_blocks(monkeypatch):
    # A long while the higher-timeframe (1d) trend is down -> blocked.
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **kwargs: _decision())
    p = run_orchestrator("X", AssetClass.FOREX, "1h", _tech(macro="down"), _fund(), now=NOW,
                         use_llm=True, ai_led=True)
    assert p.direction == Direction.NO_TRADE and "higher-timeframe" in p.rationale


def test_guardrail_hair_trigger_stop_blocks(monkeypatch):
    _patch_llm(monkeypatch, _decision(stop_loss=99.9, take_profit=140.0))  # risk 0.1 < 0.25*ATR(2)
    p = _run()
    assert p.direction == Direction.NO_TRADE and "ATR" in p.rationale


# ---- fallbacks: AI off / LLM failure -> deterministic engine ----

def test_llm_failure_falls_back_to_deterministic(monkeypatch):
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **kwargs: None)  # AI call (and review) fail
    p = _run()
    assert p.strategy != "ai"             # came from the deterministic engine
    assert p.direction == Direction.LONG  # clean uptrend still trades deterministically


def test_ai_gated_off_when_use_llm_false(monkeypatch):
    # The cheap scanner loop (use_llm=False) must NOT call the AI even with ai_led on.
    called = {"n": 0}
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **kwargs: called.__setitem__("n", called["n"] + 1))
    p = _run(ai_led=True, use_llm=False)
    assert called["n"] == 0 and p.strategy != "ai" and p.direction == Direction.LONG


# ---- ai_review gate: AI out of the driver's seat ----

def test_ai_review_off_skips_the_veto(monkeypatch):
    # ai_review=False: the confirm/veto reviewer is NEVER called; the deterministic LONG stands.
    called = {"n": 0}
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **k: called.__setitem__("n", called["n"] + 1))
    p = run_orchestrator("X", AssetClass.FOREX, "1h", _tech(), _fund(), now=NOW,
                         use_llm=True, ai_led=False, ai_review=False)
    assert called["n"] == 0 and p.direction == Direction.LONG and p.review_decision != "veto"


def test_ai_review_on_can_veto(monkeypatch):
    # ai_review=True (legacy): a veto verdict turns the deterministic LONG into NO_TRADE.
    from app.models.enums import ReviewDecision
    from app.models.schemas import TradeReviewLLM
    veto = TradeReviewLLM(decision=ReviewDecision.VETO, confidence=0.2,
                          rationale="too extended", concerns=["chasing"])
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **k: veto)
    p = run_orchestrator("X", AssetClass.FOREX, "1h", _tech(), _fund(), now=NOW,
                         use_llm=True, ai_led=False, ai_review=True)
    assert p.direction == Direction.NO_TRADE and p.review_decision == "veto"


def test_ai_review_endpoint_persists(db_session):
    from app.api.settings_routes import AiReviewRequest, set_ai_review
    from app.core.state import get_or_create_settings

    off = set_ai_review(AiReviewRequest(enabled=False), session=db_session)
    assert off.app.ai_review_enabled is False
    assert get_or_create_settings(db_session).ai_review_enabled is False
    on = set_ai_review(AiReviewRequest(enabled=True), session=db_session)
    assert on.app.ai_review_enabled is True


# ---- the settings toggle (the revert switch) persists ----

def test_ai_led_mode_endpoint_persists(db_session):
    from app.api.settings_routes import AiLedModeRequest, set_ai_led_mode
    from app.core.state import get_or_create_settings

    off = set_ai_led_mode(AiLedModeRequest(enabled=False), session=db_session)
    assert off.app.ai_led_mode is False
    assert get_or_create_settings(db_session).ai_led_mode is False

    on = set_ai_led_mode(AiLedModeRequest(enabled=True), session=db_session)
    assert on.app.ai_led_mode is True
