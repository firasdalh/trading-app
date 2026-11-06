"""GET /api/journal/chart-trades: one pair's trades for the chart's MT5-style history. With no broker deal
history (the simulator here) it draws from the app's own closed positions - the pair's, in the window,
whatever the journal's "start fresh" marker says."""
from datetime import datetime, timedelta, timezone

from app.api.journal_routes import chart_trades
from app.core.state import get_or_create_settings
from app.models.db import Position
from app.models.enums import AssetClass, PositionStatus

NOW = datetime.now(timezone.utc)


def _pos(session, symbol, closed_ago_h, status=PositionStatus.CLOSED.value, **kw):
    closed = NOW - timedelta(hours=closed_ago_h)
    p = Position(symbol=symbol, asset_class="forex", direction=kw.pop("direction", "long"), qty=0.1,
                 entry_price=1.1, last_price=kw.pop("exit", 1.102), realized_pnl=kw.pop("pnl", 20.0),
                 status=status, source=kw.pop("source", "strategy:1"),
                 opened_at=closed - timedelta(minutes=5), closed_at=closed if status == PositionStatus.CLOSED.value else None)
    session.add(p)
    session.commit()
    return p


def test_the_pairs_closed_trades_in_the_window(db_session):
    a = _pos(db_session, "EURUSDm", 2)
    b = _pos(db_session, "eurusdm", 30, direction="short", exit=1.104, pnl=-8.5, source="manual")
    _pos(db_session, "EURUSDm", 24 * 10)                                   # before the window
    _pos(db_session, "GBPUSDm", 2)                                         # another pair
    _pos(db_session, "EURUSDm", 1, status=PositionStatus.OPEN.value)       # still open: the chart draws it itself
    settings = get_or_create_settings(db_session)
    settings.journal_reset_at = NOW - timedelta(hours=1)                   # the journal's marker: not the chart's
    db_session.commit()

    rows = chart_trades(symbol="EURUSDm", asset_class=AssetClass.FOREX, days=7, session=db_session)
    assert [r["id"] for r in rows] == [b.id, a.id]                         # oldest first
    short = rows[0]
    assert (short["direction"], short["entry_price"], short["pnl"], short["source"], short["open"]) == (
        "short", 1.1, -8.5, "manual", False)
    assert short["opened_at"] == (b.opened_at.replace(tzinfo=timezone.utc) if b.opened_at.tzinfo is None
                                  else b.opened_at).isoformat()
    assert [(x["price"], x["volume"], x["pnl"], x["by"]) for x in short["exits"]] == [(1.104, 0.1, -8.5, None)]
