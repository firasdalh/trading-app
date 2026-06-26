"""The daily-loss pause: the manual Resume endpoint clears it, and the dashboard view reports it as
paused ONLY while the breaker is armed (so 'Breaker OFF' doesn't leave a misleading PAUSED badge)."""
from __future__ import annotations

from app.api.routes import read_risk_state, resume_trading
from app.core.state import get_or_create_daily_state, get_or_create_risk_config


def _pause(session, reason="daily loss limit reached") -> None:
    d = get_or_create_daily_state(session)
    d.trading_paused = True
    d.pause_reason = reason
    session.commit()


def test_resume_clears_pause(db_session):
    _pause(db_session)
    view = resume_trading(session=db_session)
    assert view.trading_paused is False and view.pause_reason is None
    assert get_or_create_daily_state(db_session).trading_paused is False


def test_pause_view_gated_by_breaker(db_session):
    _pause(db_session)
    rc = get_or_create_risk_config(db_session)

    rc.daily_loss_breaker_enabled = True
    db_session.commit()
    assert read_risk_state(session=db_session).trading_paused is True

    rc.daily_loss_breaker_enabled = False
    db_session.commit()
    v = read_risk_state(session=db_session)
    assert v.trading_paused is False and v.pause_reason is None
    # The stored flag survives (a new UTC day resets it); only the reported/effective value is gated.
    assert get_or_create_daily_state(db_session).trading_paused is True
