"""Orchestrator: the deterministic engine decides; the capital-protective guardrails (used by the AI
decider) and the optional confirm/veto reviewer police it.

Covers each guardrail (via _apply_guardrails, the shared check the AI decider reuses) and the ai_review
confirm/veto gate. (The old AI-led decision branch was removed — the AI decider now lives at the
pipeline level in ai_decider.py; see test_ai_decider.py.)"""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.orchestrator as orch
from app.agents.orchestrator import _apply_guardrails, run_orchestrator
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import FundamentalRead, TechnicalRead, TimeframeRead, TradeProposal

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


def _prop(direction=Direction.LONG, entry=100.0, stop=97.0, tp=106.0) -> TradeProposal:
    return TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h", direction=direction,
                         entry=entry, stop_loss=stop, take_profit=tp, confidence=0.7, technical=_tech())


# ---- capital-protective guardrails (the AI decider reuses _apply_guardrails) ----

def test_guardrail_passes_clean_long():
    p = _apply_guardrails(_prop(), _tech())
    assert p.direction == Direction.LONG


def test_guardrail_low_rr_blocks():
    p = _apply_guardrails(_prop(tp=101.0), _tech())  # risk 3, reward 1 -> 0.33R
    assert p.direction == Direction.NO_TRADE and "reward:risk" in p.rationale


def test_guardrail_wrong_side_blocks():
    p = _apply_guardrails(_prop(stop=105.0), _tech())  # stop above entry for a long
    assert p.direction == Direction.NO_TRADE and "wrong side" in p.rationale


def test_guardrail_against_macro_blocks():
    # a long while the higher-timeframe (1d) trend is down -> blocked
    p = _apply_guardrails(_prop(), _tech(macro="down"))
    assert p.direction == Direction.NO_TRADE and "higher-timeframe" in p.rationale


def test_guardrail_hair_trigger_stop_blocks():
    p = _apply_guardrails(_prop(stop=99.9, tp=140.0), _tech())  # risk 0.1 < 0.25*ATR(2)
    assert p.direction == Direction.NO_TRADE and "ATR" in p.rationale


# ---- ai_review gate (confirm / veto over the deterministic setup) ----

def test_ai_review_off_skips_the_veto(monkeypatch):
    # ai_review=False: the confirm/veto reviewer is NEVER called; the deterministic LONG stands.
    called = {"n": 0}
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **k: called.__setitem__("n", called["n"] + 1))
    p = run_orchestrator("X", AssetClass.FOREX, "1h", _tech(), _fund(), now=NOW,
                         use_llm=True, ai_review=False)
    assert called["n"] == 0 and p.direction == Direction.LONG and p.review_decision != "veto"


def test_ai_review_on_can_veto(monkeypatch):
    # ai_review=True: a veto verdict turns the deterministic LONG into NO_TRADE.
    from app.models.enums import ReviewDecision
    from app.models.schemas import TradeReviewLLM
    veto = TradeReviewLLM(decision=ReviewDecision.VETO, confidence=0.2,
                          rationale="too extended", concerns=["chasing"])
    monkeypatch.setattr(orch, "llm_available", lambda: True)
    monkeypatch.setattr(orch, "analyze", lambda **k: veto)
    p = run_orchestrator("X", AssetClass.FOREX, "1h", _tech(), _fund(), now=NOW,
                         use_llm=True, ai_review=True)
    assert p.direction == Direction.NO_TRADE and p.review_decision == "veto"


def test_ai_review_endpoint_persists(db_session):
    from app.api.settings_routes import AiReviewRequest, set_ai_review
    from app.core.state import get_or_create_settings

    off = set_ai_review(AiReviewRequest(enabled=False), session=db_session)
    assert off.app.ai_review_enabled is False
    assert get_or_create_settings(db_session).ai_review_enabled is False
    on = set_ai_review(AiReviewRequest(enabled=True), session=db_session)
    assert on.app.ai_review_enabled is True
