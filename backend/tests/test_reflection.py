"""Tests for the read-only Reflection/Journal agent (deterministic path)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.reflection import latest_reflection, run_reflection
from app.models.db import Position
from app.models.enums import Direction, PositionStatus

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def _closed(session, *, symbol, direction, pnl, hold_h, entry=100.0):
    opened = NOW - timedelta(hours=hold_h)
    pos = Position(
        symbol=symbol, asset_class="stock", direction=direction.value, qty=10.0,
        entry_price=entry, last_price=entry + (pnl / 10.0), status=PositionStatus.CLOSED.value,
        realized_pnl=pnl, opened_at=opened, closed_at=NOW, risk_amount=100.0,
    )
    session.add(pos)
    session.commit()
    return pos


def test_reflection_empty(db_session):
    report = run_reflection(db_session)
    assert report.trades_reviewed == 0
    assert "No closed trades" in report.summary


def test_reflection_computes_stats(db_session):
    _closed(db_session, symbol="AAPL", direction=Direction.LONG, pnl=200.0, hold_h=2)
    _closed(db_session, symbol="AAPL", direction=Direction.LONG, pnl=-100.0, hold_h=8)
    _closed(db_session, symbol="MSFT", direction=Direction.SHORT, pnl=-50.0, hold_h=10)

    report = run_reflection(db_session)
    assert report.trades_reviewed == 3
    assert report.win_rate == round(1 / 3, 3)
    assert report.net_pnl == 50.0
    # Losers held longer than the winner -> should surface that pattern + a lesson.
    joined = " ".join(report.patterns + report.lessons).lower()
    assert "held longer" in joined or "cutting losers" in joined
    assert report.patterns  # non-empty


def test_reflection_is_persisted_and_retrievable(db_session):
    _closed(db_session, symbol="AAPL", direction=Direction.LONG, pnl=10.0, hold_h=1)
    run_reflection(db_session)
    latest = latest_reflection(db_session)
    assert latest is not None
    assert latest.trades_reviewed == 1


def test_low_profit_factor_lesson(db_session):
    _closed(db_session, symbol="AAPL", direction=Direction.LONG, pnl=10.0, hold_h=1)
    _closed(db_session, symbol="AAPL", direction=Direction.LONG, pnl=-100.0, hold_h=1)
    report = run_reflection(db_session)
    assert report.profit_factor is not None and report.profit_factor < 1
    assert any("profit factor" in lesson.lower() for lesson in report.lessons)
