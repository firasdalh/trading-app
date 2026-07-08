"""Manual quick-trade endpoint: it ALWAYS runs through the deterministic Risk Manager + gates."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.execution.executor as executor
import app.risk.service as risk_service
from app.api.proposal_routes import ManualTradeRequest, manual_trade
from app.models.enums import AssetClass, ProposalStatus, RiskDecisionType
from app.models.schemas import OrderResult, RiskDecision


def _approved(qty=0.05, risk=30.0) -> RiskDecision:
    return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok",
                        symbol="EURUSDm", approved_qty=qty, risk_amount=risk)


def _vetoed() -> RiskDecision:
    return RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="exposure budget full",
                        symbol="EURUSDm", approved_qty=0.0, risk_amount=0.0)


def _req(**kw) -> ManualTradeRequest:
    base = dict(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction="long",
                stop_loss=1.08, take_profit=1.11, entry=1.10, execute=False)
    base.update(kw)
    return ManualTradeRequest(**base)


def test_manual_trade_sizes_and_queues(db_session, monkeypatch):
    # execute=False -> risk-sized and queued for approval, not opened
    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved())
    out = manual_trade(_req(), session=db_session)
    assert out.proposal.direction.value == "long"
    assert out.risk.approved and out.risk.approved_qty == 0.05
    assert out.status == ProposalStatus.PENDING_APPROVAL.value


def test_manual_trade_opens_when_execute_and_approved(db_session, monkeypatch):
    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved())

    def _exec(session, record):
        record.status = ProposalStatus.EXECUTED.value
        session.commit()
        return OrderResult(order_id="1", status="filled", filled_qty=0.05, avg_price=1.10, symbol="EURUSDm")

    monkeypatch.setattr(executor, "execute_proposal", _exec)
    out = manual_trade(_req(execute=True), session=db_session)
    assert out.status == ProposalStatus.EXECUTED.value


def test_manual_trade_vetoed_by_risk_not_opened(db_session, monkeypatch):
    called = {"exec": 0}
    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _vetoed())
    monkeypatch.setattr(executor, "execute_proposal",
                        lambda *a, **k: called.__setitem__("exec", called["exec"] + 1))
    out = manual_trade(_req(execute=True), session=db_session)
    assert out.risk.approved is False and out.status == ProposalStatus.RISK_VETOED.value
    assert called["exec"] == 0  # never executed a risk-vetoed trade


def test_manual_trade_rejects_wrong_side_stop(db_session):
    # long with the stop ABOVE the entry is invalid -> 422, before any sizing/execution
    with pytest.raises(HTTPException) as ei:
        manual_trade(_req(stop_loss=1.12), session=db_session)
    assert ei.value.status_code == 422


def test_manual_trade_rejects_bad_direction(db_session):
    with pytest.raises(HTTPException) as ei:
        manual_trade(_req(direction="sideways"), session=db_session)
    assert ei.value.status_code == 422


def test_manual_preview_returns_max_lots_without_persisting(db_session, monkeypatch):
    from sqlalchemy import select

    from app.api.proposal_routes import manual_trade_preview
    from app.models.db import TradeProposalRecord

    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved(qty=0.08, risk=76.0))
    out = manual_trade_preview(_req(), session=db_session)
    assert out.approved and out.max_lots == 0.08 and out.risk_amount == 76.0
    assert db_session.scalars(select(TradeProposalRecord)).all() == []  # preview never persists
