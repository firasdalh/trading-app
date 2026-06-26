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
                        lambda s, sym, ac, tf, use_llm=False, cache=None, read_llm=None: table[sym])
    monkeypatch.setattr(risk_service, "live_broker_positions", lambda s: [])

    res = opportunities(session=db_session)
    assert [o.symbol for o in res] == ["BBB", "AAA"]  # actionable+approved first
    assert res[0].risk_approved and res[0].rr == 2.0
    assert res[1].direction == "no_trade" and res[1].risk_approved is False


def test_opportunities_deep_pass_applies_llm_veto(db_session, monkeypatch):
    """The actionable deterministic candidate is re-judged by the LLM reviewer; a veto turns it
    into NO_TRADE in the scan too, so the list matches 'Run analysis' (no silent disagreement)."""
    db_session.add(WatchItem(symbol="BBB", asset_class="forex", timeframe="1h", enabled=True))
    db_session.commit()

    det_good = (TradeProposal(symbol="BBB", asset_class=AssetClass.FOREX, direction=Direction.LONG,
                              entry=1.10, stop_loss=1.09, take_profit=1.12, confidence=0.78,
                              rationale="confluence"),
                RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="BBB"))
    llm_veto = (TradeProposal(symbol="BBB", asset_class=AssetClass.FOREX, direction=Direction.NO_TRADE,
                              confidence=0.0, rationale="VETOED by AI review: chasing into resistance"),
                RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="no trade", symbol="BBB"))

    def fake_preview(s, sym, ac, tf, use_llm=False, cache=None, read_llm=None):
        return llm_veto if use_llm else det_good

    monkeypatch.setattr(pipeline, "preview_symbol", fake_preview)
    monkeypatch.setattr(risk_service, "live_broker_positions", lambda s: [])
    monkeypatch.setattr("app.agents.llm.llm_available", lambda: True)

    res = opportunities(session=db_session)
    assert res[0].symbol == "BBB"
    assert res[0].direction == "no_trade" and res[0].risk_approved is False
    assert res[0].confidence == 0.0 and "VETOED" in res[0].rationale


def test_events_soon_lists_upcoming_medium_only(monkeypatch):
    """The soft heads-up lists upcoming medium/high events within the window and excludes past /
    far-out ones. Display-only — it never gates the trade."""
    from datetime import datetime, timedelta, timezone

    import app.data.providers as providers
    from app.api.watchlist_routes import _events_soon
    from app.data.providers import CalendarEvent

    now = datetime.now(timezone.utc)
    evs = [
        CalendarEvent(label="US: Fed Speech", when=now + timedelta(minutes=40), importance="medium", country="US"),
        CalendarEvent(label="US: CPI", when=now + timedelta(hours=3), importance="high", country="US"),
        CalendarEvent(label="US: Released", when=now - timedelta(hours=1), importance="high", country="US"),
        CalendarEvent(label="US: FarOut", when=now + timedelta(hours=20), importance="medium", country="US"),
    ]

    class _Cal:
        def get_events(self, symbol, lookahead_hours=24, include_medium=False):
            return evs

    monkeypatch.setattr(providers, "get_calendar_provider", lambda: _Cal())
    note = _events_soon("USDJPYm", hours=8)
    assert note and "Fed Speech" in note and "CPI" in note
    assert "Released" not in note and "FarOut" not in note   # past + beyond-window excluded
    assert "(medium)" in note and "(high)" in note


def test_opportunities_marks_already_open(db_session, monkeypatch):
    db_session.add(WatchItem(symbol="BBB", asset_class="forex", timeframe="1h", enabled=True))
    db_session.commit()
    table = _props()
    monkeypatch.setattr(pipeline, "preview_symbol",
                        lambda s, sym, ac, tf, use_llm=False, cache=None, read_llm=None: table["BBB"])
    monkeypatch.setattr(risk_service, "live_broker_positions",
                        lambda s: [type("P", (), {"symbol": "BBB", "direction": "long"})()])
    res = opportunities(session=db_session)
    assert res[0].already_open is True
