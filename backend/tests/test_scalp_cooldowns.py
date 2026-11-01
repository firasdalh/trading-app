"""Scalp mode's cooldowns (RISK.md override 2026-11-01): a hand-placed order tagged "scalp" skips the
30-min per-pair cooldown and gets a 5-min loss cooldown instead of 180. Everything else keeps the
RISK.md defaults, and every path a scalp order runs through (the ticket, a re-size to its lots, the
executor's last check) carries the waiver."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.execution.executor as executor
import app.risk.service as risk_service
from app.api.proposal_routes import ManualTradeRequest, manual_trade
from app.models.db import Position, TradeProposalRecord
from app.models.enums import AssetClass, Direction, PositionStatus, RiskDecisionType
from app.models.schemas import RiskDecision, TradeProposal
from app.risk.service import assess, record_cooldowns, scalp_cooldowns

SCALP = {"override_cooldown_minutes": 0, "override_loss_cooldown_minutes": 5}


def _closed(session, minutes_ago: float, pnl: float, direction: str = "long") -> None:
    session.add(Position(symbol="AAPL", asset_class="stock", direction=direction, qty=1.0, entry_price=100.0,
                         status=PositionStatus.CLOSED.value, last_price=100.0 + pnl, realized_pnl=pnl,
                         closed_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
    session.commit()


def _long() -> TradeProposal:
    return TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                         entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.8)


def test_only_a_manual_order_tagged_scalp_gets_the_waiver():
    assert scalp_cooldowns("manual", ["scalp", "medium"]) == SCALP
    assert scalp_cooldowns("manual", ["swing"]) == {}
    assert scalp_cooldowns("manual", None) == {}
    assert scalp_cooldowns("hybrid", ["scalp"]) == {}          # the Hybrid never gets it
    assert scalp_cooldowns("auto_trade", None) == {}
    assert scalp_cooldowns("strategy:3", None) == SCALP        # the user's own strategies do
    assert scalp_cooldowns(None, ["scalp"]) == {}


def test_record_cooldowns_reads_the_stored_tags():
    rec = TradeProposalRecord(symbol="AAPL", asset_class="stock", timeframe="manual", direction="long",
                              source="manual", reasoning={"manual": True, "tags": ["scalp", "high"]})
    assert record_cooldowns(rec) == SCALP
    rec.reasoning = {"manual": True, "tags": []}
    assert record_cooldowns(rec) == {}
    rec.reasoning = None
    assert record_cooldowns(rec) == {}


def test_a_win_a_minute_ago_blocks_a_normal_trade_but_not_a_scalp(db_session):
    _closed(db_session, minutes_ago=1, pnl=+2.0)
    plain = assess(db_session, _long())
    assert not plain.approved and "per-pair cooldown" in plain.reason
    scalp = assess(db_session, _long(), **SCALP)
    assert scalp.approved, scalp.reason


def test_after_a_losing_scalp_the_same_side_waits_five_minutes(db_session):
    _closed(db_session, minutes_ago=2, pnl=-2.0)
    d = assess(db_session, _long(), **SCALP)
    assert not d.approved and "loss cooldown" in d.reason
    # the other side is never blocked by the loss cooldown
    short = _long().model_copy(update={"direction": Direction.SHORT, "stop_loss": 105.0, "take_profit": 90.0})
    assert assess(db_session, short, **SCALP).approved


def test_five_minutes_after_a_loss_a_scalp_goes_while_a_normal_trade_still_waits(db_session):
    _closed(db_session, minutes_ago=6, pnl=-2.0)
    assert assess(db_session, _long(), **SCALP).approved
    plain = assess(db_session, _long())
    assert not plain.approved and "cooldown" in plain.reason


def _capture(calls):
    def fake(session, proposal, **kw):
        calls.append(kw)
        return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol=proposal.symbol,
                            approved_qty=0.05, risk_amount=10.0)
    return fake


def _req(**kw) -> ManualTradeRequest:
    base = dict(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction="long",
                stop_loss=1.08, take_profit=1.11, entry=1.10, execute=False)
    base.update(kw)
    return ManualTradeRequest(**base)


def test_the_ticket_passes_the_waiver_only_for_scalp_tags(db_session, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(risk_service, "assess", _capture(calls))
    manual_trade(_req(tags=["Scalp ", "medium"]), session=db_session)
    manual_trade(_req(tags=["breakout"]), session=db_session)
    assert calls[0]["override_cooldown_minutes"] == 0 and calls[0]["override_loss_cooldown_minutes"] == 5
    assert "override_cooldown_minutes" not in calls[1] and "override_loss_cooldown_minutes" not in calls[1]


def test_the_resize_to_the_tickets_lots_keeps_the_waiver(db_session, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(risk_service, "assess", _capture(calls))
    rec = TradeProposalRecord(symbol="AAPL", asset_class="stock", timeframe="manual", direction="long",
                              entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.0, rationale="t",
                              source="manual", reasoning={"manual": True, "tags": ["scalp"]},
                              status="pending_approval", risk_decision="approved", approved_qty=1.0, risk_amount=5.0)
    db_session.add(rec)
    db_session.commit()
    risk_service.size_preview(db_session, rec, desired_lots=1.0)
    assert calls and calls[-1]["override_cooldown_minutes"] == 0 and calls[-1]["override_loss_cooldown_minutes"] == 5


def test_the_executor_last_check_keeps_the_waiver(db_session, monkeypatch):
    calls: list[dict] = []

    class _Q:
        price = 100.0

    class _B:
        name = "sim"
        is_paper = True

        def get_quote(self, s):
            return _Q()

        def spread(self, s):
            return 0.0

    monkeypatch.setattr("app.risk.service.assess", _capture(calls))
    monkeypatch.setattr("app.risk.service.current_equity", lambda s: 1000.0)
    for tags, waived in ((["scalp", "low"], True), (["swing"], False)):
        rec = TradeProposalRecord(symbol="AAPL", asset_class="stock", timeframe="manual", direction="long",
                                  entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.0, rationale="t",
                                  source="manual", reasoning={"manual": True, "tags": tags},
                                  status="approved", risk_decision="approved", approved_qty=0.05, risk_amount=10.0)
        db_session.add(rec)
        db_session.commit()
        executor._final_risk_gate(db_session, _B(), rec)
        assert ("override_cooldown_minutes" in calls[-1]) is waived


def test_a_strategy_record_keeps_the_waiver_to_the_executor():
    rec = TradeProposalRecord(symbol="AAPL", asset_class="stock", timeframe="5s", direction="long",
                              source="strategy:7", reasoning={"strategy_id": 7})
    assert record_cooldowns(rec) == SCALP
