"""Task 12 — structured decision logging (classify_gate + record_decision)."""
from __future__ import annotations

from app.agents.decision_log import classify_gate, record_decision
from app.models.db import AgentRun
from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import RiskDecision, TradeProposal


def _prop(direction=Direction.NO_TRADE, rationale="", review=None, conditional=None, watch=False):
    return TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=direction, confidence=0.5, rationale=rationale,
                         review_decision=review, conditional=conditional, watch=watch)


def _risk(approved=True):
    return RiskDecision(
        decision=RiskDecisionType.APPROVED if approved else RiskDecisionType.VETOED,
        approved=approved, reason="ok" if approved else "exposure full", symbol="EURUSDm")


def test_classify_gate_approved_and_vetoes():
    assert classify_gate(_prop(Direction.LONG), _risk(True)) == "approved"
    assert classify_gate(_prop(Direction.LONG, review="veto"), _risk(True)) == "ai_veto"
    assert classify_gate(_prop(Direction.LONG), _risk(False)) == "risk_veto"


def test_classify_gate_no_trade_reasons():
    assert classify_gate(_prop(rationale="Trend-only mode: standing aside — regime is ranging"), None) \
        == "regime_not_trending"
    assert classify_gate(_prop(rationale="No confluence: higher-timeframe trend is DOWN"), None) \
        == "mtf_conflict"
    assert classify_gate(_prop(rationale="Structure conflict: EMA trend reads up"), None) \
        == "structure_conflict"
    assert classify_gate(_prop(rationale="Volatile regime: volatility expanding"), None) == "volatility"
    assert classify_gate(_prop(rationale="No clear trend (EMAs sideways) — sitting out."), None) \
        == "no_trend"
    assert classify_gate(_prop(rationale="something novel"), None) == "no_trade_other"


def test_record_decision_persists_queryable_row(db_session):
    p = _prop(rationale="Trend-only mode: standing aside — regime is moderate")
    record_decision(db_session, "EURUSDm", "1h", p, None)
    db_session.commit()
    rows = db_session.query(AgentRun).filter(AgentRun.agent == "decision").all()
    assert len(rows) == 1
    d = rows[0].detail
    assert d["gate"] == "regime_not_trending" and d["actionable"] is False
    assert d["direction"] == "no_trade" and "rationale" in d and "indicators" in d
