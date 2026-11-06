"""Scalp mode's per-trade risk ceiling (RISK.md override 2026-11-02): the scalp ticket's orders and the
custom strategies' may risk up to 6% of equity; every other order keeps the configured 3%. Every path a
scalp order runs through carries it - the ticket, the re-size to its lots, the executor's last check."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import app.execution.executor as executor
import app.risk.service as risk_service
from app.api.proposal_routes import ManualTradeRequest, manual_trade
from app.models.db import TradeProposalRecord
from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import RiskDecision, TradeProposal
from app.risk.service import SCALP_RISK_CEILING, assess, record_risk_ceiling, scalp_risk_ceiling

SIX = {"override_risk_ceiling": 0.06}


def _long() -> TradeProposal:
    return TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                         entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.8)


def _record(tags, source="manual") -> TradeProposalRecord:
    return TradeProposalRecord(symbol="AAPL", asset_class="stock", timeframe="manual", direction="long",
                               entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.0, rationale="t",
                               source=source, reasoning={"manual": True, "tags": tags},
                               status="pending_approval", risk_decision="approved", approved_qty=1.0, risk_amount=5.0)


def test_only_scalp_orders_get_the_6_percent_ceiling():
    assert SCALP_RISK_CEILING == 0.06
    assert scalp_risk_ceiling("manual", ["scalp", "high"]) == SIX
    assert scalp_risk_ceiling("strategy:2", None) == SIX
    assert scalp_risk_ceiling("manual", ["swing"]) == {}
    assert scalp_risk_ceiling("hybrid", ["scalp"]) == {}          # the Hybrid never gets it
    assert scalp_risk_ceiling("auto_trade", None) == {}
    assert record_risk_ceiling(_record(["scalp"])) == SIX
    assert record_risk_ceiling(_record([])) == {}


def test_a_scalp_risks_6_percent_where_any_other_order_stops_at_3(db_session):
    plain = assess(db_session, _long(), override_risk_fraction=0.06)
    scalp = assess(db_session, _long(), override_risk_fraction=0.06, **SIX)
    assert plain.approved and scalp.approved, (plain.reason, scalp.reason)
    assert scalp.risk_amount == pytest.approx(2 * plain.risk_amount, rel=0.02)
    # and never past 6%, whatever is asked
    wild = assess(db_session, _long(), override_risk_fraction=0.5, override_risk_ceiling=0.5)
    assert wild.risk_amount == pytest.approx(scalp.risk_amount, rel=0.02)


def test_the_resize_to_the_tickets_lots_caps_a_scalp_at_6(db_session):
    rows = [_record(["scalp"]), _record(["swing"])]
    db_session.add_all(rows)
    db_session.commit()
    scalp, plain = (risk_service.size_preview(db_session, r, desired_lots=100_000.0) for r in rows)
    assert scalp["max_lots"] == pytest.approx(2 * plain["max_lots"], rel=0.02)
    assert scalp["capped"] and plain["capped"]


def _capture(calls):
    def fake(session, proposal, **kw):
        calls.append(kw)
        return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol=proposal.symbol,
                            approved_qty=0.05, risk_amount=10.0)
    return fake


def test_the_ticket_and_the_executor_pass_the_ceiling_only_for_scalps(db_session, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(risk_service, "assess", _capture(calls))
    req = dict(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction="long", stop_loss=1.08,
               take_profit=1.11, entry=1.10, execute=False, risk_pct=0.06)
    manual_trade(ManualTradeRequest(**req, tags=["Scalp", "high"]), session=db_session)
    manual_trade(ManualTradeRequest(**req, tags=["breakout"]), session=db_session)
    assert calls[0].get("override_risk_ceiling") == 0.06 and "override_risk_ceiling" not in calls[1]

    class _Q:
        price = 100.0

    class _B:
        name = "sim"
        is_paper = True

        def get_quote(self, s):
            return _Q()

        def spread(self, s):
            return 0.0

    monkeypatch.setattr("app.risk.service.current_equity", lambda s: 1000.0)
    for tags, six in ((["scalp"], True), (["swing"], False)):
        rec = _record(tags)
        rec.status, rec.approved_qty, rec.risk_amount = "approved", 0.05, 10.0
        db_session.add(rec)
        db_session.commit()
        executor._final_risk_gate(db_session, _B(), rec)
        assert (calls[-1].get("override_risk_ceiling") == 0.06) is six


def test_the_ticket_and_the_board_accept_up_to_6_percent():
    from app.api.strategy_routes import StrategyIn

    base = dict(symbol="EURUSDm", direction="long")
    assert ManualTradeRequest(**base, risk_pct=0.06).risk_pct == 0.06
    with pytest.raises(ValidationError):
        ManualTradeRequest(**base, risk_pct=0.061)
    assert StrategyIn(risk_pct=0.06).risk_pct == 0.06
    with pytest.raises(ValidationError):
        StrategyIn(risk_pct=0.0601)
