"""Tests for the open-position management advisor (protect winners / cut losers around news)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.agents.position_advisor as advisor
from app.data.providers import CalendarEvent
from app.models.schemas import PositionView

NOW = datetime.now(timezone.utc)


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
    monkeypatch.setattr(advisor, "_position_thesis", lambda session, p: thesis)


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
