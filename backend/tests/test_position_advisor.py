"""Tests for the open-position management advisor (protect winners / cut losers around news)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.agents.position_advisor as advisor
from app.data.providers import CalendarEvent
from app.models.schemas import PositionAdvice, PositionView

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_advisor_state():
    advisor._reset_auto_state()
    yield
    advisor._reset_auto_state()


def _pos(symbol="XAUUSDm", direction="short", pnl=10.0, stop=4473.0) -> PositionView:
    return PositionView(
        id=1, symbol=symbol, asset_class="metal", direction=direction, qty=1.0,
        entry_price=4449.0, stop_loss=stop, take_profit=4397.0, status="open",
        last_price=4439.0, unrealized_pnl=pnl,
    )


class _Cal:
    def __init__(self, events):
        self._events = events

    def get_events(self, symbol, lookahead_hours=24):
        return self._events


def _patch(monkeypatch, positions, events, thesis=None):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: positions)
    monkeypatch.setattr(advisor, "get_calendar_provider", lambda: _Cal(events))
    # Isolate the event/protection logic from the (broker-dependent) thesis re-check unless a
    # test explicitly wants a thesis.
    monkeypatch.setattr(advisor, "_position_context", lambda session, p: None)
    monkeypatch.setattr(advisor, "_position_thesis", lambda session, p, ctx=None: thesis)


def _event(mins_from_now=45, importance="high"):
    return CalendarEvent(label="US: ISM Services PMI", when=NOW + timedelta(minutes=mins_from_now),
                         importance=importance, country="US")


def test_winning_into_event_says_lock_in(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=10.0)], [_event(45)])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "warn"
    assert "winning into" in a.headline.lower()
    assert "lock it in" in a.detail.lower() or "breakeven" in a.detail.lower()
    assert a.event_label == "US: ISM Services PMI"


def test_losing_into_event_says_cut(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=-15.0)], [_event(30)])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "warn"
    assert "losing into" in a.headline.lower()
    assert "clos" in a.detail.lower() or "reduc" in a.detail.lower()


def test_no_stop_into_event_is_danger(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=5.0, stop=None)], [_event(20)])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "danger"
    assert "protect" in a.headline.lower()


def test_no_event_winner_holds(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "info"
    assert "profit" in a.headline.lower()


def test_far_off_event_is_not_imminent(monkeypatch):
    # An event 6h out should not trigger the news branch.
    _patch(monkeypatch, [_pos(pnl=8.0)], [_event(360)])
    [a] = advisor.advise_positions(session=None)
    assert a.event_label is None and a.severity == "info"


# ---- thesis re-check folding ----

def test_invalidated_thesis_escalates_to_danger(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [],
           thesis={"label": "invalidated", "note": "Plan check: trend flipped."})
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "danger" and a.thesis == "invalidated"
    assert "thesis broken" in a.headline.lower()
    assert "plan check" in a.detail.lower()


def test_weakening_thesis_escalates_info_to_warn(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [],
           thesis={"label": "weakening", "note": "Plan check: momentum rolling over."})
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "warn" and a.thesis == "weakening"


def test_intact_thesis_stays_info_and_appends_note(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [],
           thesis={"label": "intact", "note": "Plan check: thesis intact."})
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "info" and a.thesis == "intact"
    assert "thesis intact" in a.detail.lower()


def test_event_keeps_headline_even_when_thesis_invalidated(monkeypatch):
    # News is the nearer concern: it keeps the headline, but the thesis still bumps severity.
    _patch(monkeypatch, [_pos(pnl=10.0)], [_event(30)],
           thesis={"label": "invalidated", "note": "Plan check: trend flipped."})
    [a] = advisor.advise_positions(session=None)
    assert "winning into" in a.headline.lower() and a.severity == "danger"


# ---- auto-watch config + tick ----

def test_run_advisor_stamps_last_run(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [])
    out = advisor.run_advisor(db_session)
    assert "last_run_at" in out and out["advice"] == []
    cfg = advisor.get_or_create_advisor_config(db_session)
    assert cfg.last_run_at is not None


def test_advisor_tick_respects_disabled(db_session):
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.enabled = False
    db_session.commit()
    assert advisor.advisor_tick(db_session)["ran"] is False


def test_advisor_tick_runs_when_enabled(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [])
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.enabled = True
    cfg.interval_seconds = 60
    db_session.commit()
    assert advisor.advisor_tick(db_session)["ran"] is True
    # Interval hasn't elapsed -> should not run again immediately.
    second = advisor.advisor_tick(db_session)
    assert second["ran"] is False and second["reason"] == "interval not elapsed"


# ---- auto-execute ----

def _adv(symbol="XAUUSDm", thesis="intact", event=None, sev="info"):
    return PositionAdvice(symbol=symbol, direction="short", unrealized_pnl=10.0, has_stop=True,
                          severity=sev, headline="h", detail="d", thesis=thesis, event_label=event)


class _Result:
    def __init__(self, value="filled", error=None):
        self.status = type("S", (), {"value": value})()
        self.error = error


class _Broker:
    def __init__(self, is_paper=True):
        self.is_paper = is_paper
        self.closed = []
        self.sltp = []

    def close_position(self, symbol):
        self.closed.append(symbol)
        return _Result("filled")

    def set_sl_tp(self, symbol, sl, tp):
        self.sltp.append((symbol, sl, tp))
        return _Result("filled")


def test_auto_decision_closes_invalidated():
    p = _pos(direction="short")
    assert advisor._auto_decision(_adv(thesis="invalidated"), p, {}, None)["action"] == "close"


def test_auto_decision_breakeven_winning_into_news():
    # short winning, stop above entry (worse side) -> lock to breakeven.
    p = _pos(direction="short", pnl=10.0, stop=4460.0)  # entry 4449 -> stop above = worse
    decision = advisor._auto_decision(_adv(thesis="intact", event="US: ISM"), p, {}, None)
    assert decision is not None and decision["kind"] == "breakeven"


def test_auto_decision_protects_naked_position():
    p = _pos(direction="short", stop=None)
    decision = advisor._auto_decision(_adv(thesis="intact"), p, {"atr": 10.0, "last": 4449.0}, None)
    assert decision is not None and decision["kind"] == "protect"
    assert decision["stop"] > 4449.0  # protective stop above price for a short


def test_auto_decision_trails_beyond_target_r():
    # short, big profit (last well below entry) -> trail; plan_risk 10, profit ~50 = 5R.
    p = _pos(direction="short", stop=4460.0)
    decision = advisor._auto_decision(_adv(thesis="intact"), p,
                                      {"atr": 5.0, "last": 4399.0}, 10.0)
    assert decision is not None and decision["kind"] == "trail"
    assert decision["stop"] < 4460.0  # tighter than the current stop


def test_auto_decision_none_when_intact_no_event():
    assert advisor._auto_decision(_adv(thesis="intact"), _pos(), {}, None) is None


def _patch_exec(monkeypatch, broker, *, kill=False, live_ok=True):
    import app.brokers.registry as reg
    import app.core.state as state
    monkeypatch.setattr(state, "kill_switch_active", lambda session: kill)
    monkeypatch.setattr(state, "live_execution_allowed", lambda settings: live_ok)
    monkeypatch.setattr(state, "get_or_create_settings",
                        lambda session: type("S", (), {"broker_map": {}})())
    monkeypatch.setattr(reg, "get_broker_for", lambda ac, bm: broker)


def test_auto_execute_closes_on_paper(db_session, monkeypatch):
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "_CLOSE_CONFIRM", 1)  # close on first invalidation for this test
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker)
    actions = advisor._auto_execute(db_session, [_adv(thesis="invalidated", sev="danger")])
    assert broker.closed == ["XAUUSDm"]
    assert actions[0]["action"] == "close" and actions[0]["ok"] is True


def test_close_requires_hysteresis(db_session, monkeypatch):
    # Default _CLOSE_CONFIRM=2: first invalidation is pending, second confirms the close.
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker)
    first = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert broker.closed == [] and first[0]["action"] == "close_pending"
    second = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert broker.closed == ["XAUUSDm"] and second[0]["ok"] is True


def test_auto_execute_halts_on_kill_switch(db_session, monkeypatch):
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker, kill=True)
    actions = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert actions == [] and broker.closed == []


def test_auto_execute_blocks_unconfirmed_live(db_session, monkeypatch):
    broker = _Broker(is_paper=False)
    monkeypatch.setattr(advisor, "_CLOSE_CONFIRM", 1)  # pass hysteresis so we reach the live gate
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker, live_ok=False)
    actions = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert broker.closed == [] and actions[0]["action"] == "blocked_live_unconfirmed"


def test_run_advisor_skips_execution_when_toggle_off(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "advise_positions", lambda session: [_adv(thesis="invalidated")])
    called = {"n": 0}
    monkeypatch.setattr(advisor, "_auto_execute", lambda s, a: called.__setitem__("n", called["n"] + 1) or [])
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = False
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert out["actions"] == [] and called["n"] == 0


def test_advisor_activity_flattens_actions(db_session):
    from app.api.settings_routes import advisor_activity
    from app.models.db import AgentRun

    db_session.add(AgentRun(agent="advisor", event="check", detail={"actions": [
        {"symbol": "XAUUSDm", "action": "set_stop", "kind": "breakeven", "ok": True, "reason": "+1R"},
        {"symbol": "BTCUSDm", "action": "close", "kind": "close", "ok": True, "reason": "invalidated"},
    ]}))
    db_session.commit()
    items = advisor_activity(limit=30, session=db_session)
    assert len(items) == 2
    assert items[0].symbol == "XAUUSDm" and items[0].kind == "breakeven" and items[0].ok is True
    assert {i.symbol for i in items} == {"XAUUSDm", "BTCUSDm"}


def test_reenter_opens_when_engine_and_risk_approve(db_session, monkeypatch):
    import app.agents.pipeline as pipeline
    import app.execution.executor as executor
    from app.models.db import TradeProposalRecord
    from app.models.enums import AssetClass, Direction, ProposalStatus, RiskDecisionType
    from app.models.schemas import AnalyzeResponse, RiskDecision, TradeProposal

    rec = TradeProposalRecord(symbol="EURUSDm", asset_class="forex", timeframe="1h", direction="long",
                              entry=1.16, stop_loss=1.155, take_profit=1.17, confidence=0.6,
                              rationale="x", status=ProposalStatus.PENDING_APPROVAL.value)
    db_session.add(rec)
    db_session.commit()

    prop = TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction=Direction.LONG,
                         entry=1.16, stop_loss=1.155, take_profit=1.17, confidence=0.6)
    risk = RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="EURUSDm")
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        lambda s, sym, ac, tf, use_llm=False:
                        AnalyzeResponse(proposal_id=rec.id, status="pending_approval", proposal=prop, risk=risk))
    monkeypatch.setattr(executor, "execute_proposal",
                        lambda session, record: setattr(record, "status", ProposalStatus.EXECUTED.value))

    out = advisor._maybe_reenter(db_session, "EURUSDm", "forex")
    assert out["action"] == "reenter" and out["ok"] is True and "opened long" in out["reason"].lower()


def test_reenter_skips_when_no_fresh_setup(db_session, monkeypatch):
    import app.agents.pipeline as pipeline
    from app.models.enums import AssetClass, Direction, RiskDecisionType
    from app.models.schemas import AnalyzeResponse, RiskDecision, TradeProposal

    prop = TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction=Direction.NO_TRADE)
    risk = RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="no", symbol="EURUSDm")
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        lambda *a, **k: AnalyzeResponse(proposal_id=0, status="x", proposal=prop, risk=risk))
    out = advisor._maybe_reenter(db_session, "EURUSDm", "forex")
    assert out["action"] == "reenter_skip" and out["ok"] is False


def test_run_advisor_reenters_after_close_when_enabled(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "advise_positions", lambda s: [])
    monkeypatch.setattr(advisor, "_auto_execute",
                        lambda s, a: [{"symbol": "EURUSDm", "action": "close", "ok": True, "asset_class": "forex"}])
    monkeypatch.setattr(advisor, "_maybe_reenter",
                        lambda s, sym, ac: {"symbol": sym, "action": "reenter", "kind": "open", "ok": True, "reason": "opened long"})
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = True
    cfg.auto_reenter = True
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert any(x["action"] == "reenter" for x in out["actions"])


def test_run_advisor_no_reenter_when_toggle_off(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "advise_positions", lambda s: [])
    monkeypatch.setattr(advisor, "_auto_execute",
                        lambda s, a: [{"symbol": "EURUSDm", "action": "close", "ok": True, "asset_class": "forex"}])
    monkeypatch.setattr(advisor, "_maybe_reenter",
                        lambda s, sym, ac: (_ for _ in ()).throw(AssertionError("should not re-enter")))
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = True
    cfg.auto_reenter = False
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert not any(x["action"] == "reenter" for x in out["actions"])


def test_run_advisor_executes_when_toggle_on(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "advise_positions", lambda session: [_adv(thesis="invalidated")])
    monkeypatch.setattr(advisor, "_auto_execute",
                        lambda s, a: [{"symbol": "XAUUSDm", "action": "close", "ok": True}])
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = True
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert out["actions"][0]["action"] == "close"
