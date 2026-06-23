"""risk/service helpers: the loss-aware cooldown lookup (last_dir_loss_at)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.db import Position
from app.models.enums import PositionStatus
from app.risk.service import last_dir_loss_at

NOW = datetime(2026, 6, 23, 16, 0, tzinfo=timezone.utc)


def _closed(session, **kw) -> Position:
    base = dict(symbol="XAGGBPm", asset_class="metal", direction="short", qty=0.01,
                entry_price=46.93, stop_loss=47.22, take_profit=46.30,
                status=PositionStatus.CLOSED.value, last_price=47.22, realized_pnl=-18.0,
                closed_at=NOW - timedelta(minutes=5))
    base.update(kw)
    p = Position(**base)
    session.add(p)
    session.commit()
    return p


def test_returns_recent_stopout(db_session):
    _closed(db_session)
    assert last_dir_loss_at(db_session, "XAGGBPm", "short") is not None


def test_none_when_last_trade_won(db_session):
    _closed(db_session, realized_pnl=12.0, last_price=46.50)
    assert last_dir_loss_at(db_session, "XAGGBPm", "short") is None


def test_ignores_opposite_direction(db_session):
    _closed(db_session, direction="short", realized_pnl=-18.0)
    assert last_dir_loss_at(db_session, "XAGGBPm", "long") is None


def test_infers_loss_from_exit_when_pnl_missing(db_session):
    # No realized_pnl recorded -> infer from exit vs entry (a short stopped ABOVE entry = a loss).
    _closed(db_session, realized_pnl=None, last_price=47.22, entry_price=46.93)
    assert last_dir_loss_at(db_session, "XAGGBPm", "short") is not None


def test_uses_most_recent_close_only(db_session):
    # An older loss followed by a newer WIN -> no cooldown (the latest close won, setup worked).
    _closed(db_session, realized_pnl=-18.0, closed_at=NOW - timedelta(hours=3))
    _closed(db_session, realized_pnl=15.0, last_price=46.40, closed_at=NOW - timedelta(minutes=5))
    assert last_dir_loss_at(db_session, "XAGGBPm", "short") is None
