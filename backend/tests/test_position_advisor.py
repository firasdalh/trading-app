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


def _patch(monkeypatch, positions, events):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: positions)
    monkeypatch.setattr(advisor, "get_calendar_provider", lambda: _Cal(events))


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
