"""Tests for the watchlist Opportunities scan (rank the best setups across all pairs)."""
from __future__ import annotations

import app.agents.pipeline as pipeline
import app.risk.service as risk_service
from app.api.watchlist_routes import opportunities
from app.models.db import WatchItem
from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import RiskDecision, TradeProposal


def _props():
    no = (TradeProposal(symbol="AAA", asset_class=AssetClass.FOREX, direction=Direction.NO_TRADE,
                        confidence=0.0, rationale="chop"),
          RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="ranging", symbol="AAA"))
    good = (TradeProposal(symbol="BBB", asset_class=AssetClass.FOREX, direction=Direction.LONG,
                         entry=1.10, stop_loss=1.09, take_profit=1.12, confidence=0.72, rationale="confluence"),
            RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="BBB"))
    return {"AAA": no, "BBB": good}


def test_opportunities_ranks_actionable_approved_first(db_session, monkeypatch):
    db_session.add_all([
        WatchItem(symbol="AAA", asset_class="forex", timeframe="1h", enabled=True),
        WatchItem(symbol="BBB", asset_class="forex", timeframe="1h", enabled=True),
    ])
    db_session.commit()
    table = _props()
    monkeypatch.setattr(pipeline, "preview_symbol",
                        lambda s, sym, ac, tf, use_llm=False, cache=None: table[sym])
    monkeypatch.setattr(risk_service, "live_broker_positions", lambda s: [])

    res = opportunities(session=db_session)
    assert [o.symbol for o in res] == ["BBB", "AAA"]  # actionable+approved first
    assert res[0].risk_approved and res[0].rr == 2.0
    assert res[1].direction == "no_trade" and res[1].risk_approved is False


def test_opportunities_marks_already_open(db_session, monkeypatch):
    db_session.add(WatchItem(symbol="BBB", asset_class="forex", timeframe="1h", enabled=True))
    db_session.commit()
    table = _props()
    monkeypatch.setattr(pipeline, "preview_symbol",
                        lambda s, sym, ac, tf, use_llm=False, cache=None: table["BBB"])
    monkeypatch.setattr(risk_service, "live_broker_positions",
                        lambda s: [type("P", (), {"symbol": "BBB", "direction": "long"})()])
    res = opportunities(session=db_session)
    assert res[0].already_open is True
