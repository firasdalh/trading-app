"""The Journal can leave the trades opened by hand out of its cards (2026-11-18, the user after a big manual BTC loss:
"remove the manual history ... I don't want to see this loss in my journal"). A view only: `hide_manual` on each
card's endpoint; without it every trade is there, as before, for the desk's own checks."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.db import Position
from app.models.enums import PositionStatus
from app.models.schemas import PositionView

DAY = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _bt(i, source, pnl, opened, symbol="BTCUSDm", direction="long", entry=100.0):
    return PositionView(id=i, symbol=symbol, asset_class="crypto", direction=direction, qty=0.1, entry_price=entry,
                        status="closed", last_price=entry, unrealized_pnl=0.0, realized_pnl=pnl, opened_at=opened,
                        closed_at=opened + timedelta(minutes=10), source=source)


def _row(session, source, opened, risk=10.0, pnl=0.0, symbol="BTCUSDm", direction="long", entry=100.0):
    p = Position(symbol=symbol, asset_class="crypto", direction=direction, qty=0.1, entry_price=entry,
                 status=PositionStatus.CLOSED.value, last_price=entry, realized_pnl=pnl, risk_amount=risk,
                 source=source, opened_at=opened, closed_at=opened + timedelta(minutes=10), broker_env="paper")
    session.add(p)
    session.commit()
    return p


@pytest.fixture
def book(db_session, monkeypatch):
    """Today: a strategy win, a desk ticket's loss (the app's row holds its risk), a phone loss (untagged ->
    'manual'), and a long manual source cut short in the comment."""
    import app.risk.service as svc

    _row(db_session, "strategy:1", DAY + timedelta(minutes=5), risk=10.0, pnl=20.0, symbol="XAUUSDm")
    _row(db_session, "manual", DAY + timedelta(minutes=30), risk=50.0, pnl=-100.0)
    trades = [_bt(4, "manual_from_ticket", -5.0, DAY + timedelta(minutes=50), symbol="EURUSDm"),
              _bt(3, "manual", -1920.0, DAY + timedelta(minutes=40)),                       # the phone
              _bt(2, "manual", -100.0, DAY + timedelta(minutes=30, seconds=1)),             # the desk ticket
              _bt(1, "strategy:1", 20.0, DAY + timedelta(minutes=5, seconds=1), symbol="XAUUSDm")]
    monkeypatch.setattr(svc, "broker_closed_trades", lambda session: [t.model_copy() for t in trades])
    return db_session


def test_the_trade_log_and_the_equity_leave_out_every_manual_trade_when_asked(book):
    from app.api.journal_routes import closed_trades, equity

    assert [t.source for t in closed_trades(limit=100, session=book)] == [
        "manual_from_ticket", "manual", "manual", "strategy:1"]                        # as before: every trade
    assert [t.source for t in closed_trades(limit=100, hide_manual=True, session=book)] == ["strategy:1"]
    assert [t.source for t in equity(hide_manual=True, session=book)] == ["strategy:1"]
    assert len(equity(session=book)) == 4


def test_the_cards_count_only_what_is_left(book):
    from app.api.journal_routes import breakdown, by_session, daily_review, stats

    bd = breakdown(hide_manual=True, session=book)
    assert [g.label for g in bd.by_source] == ["strategy:1"]
    assert set(bd.by_pair_source) == {"strategy:1"} and [g.label for g in bd.by_pair] == ["XAUUSDm"]
    assert bd.daily[0].total.net_pnl == 20.0
    assert {g.label for g in breakdown(session=book).by_source} == {"strategy:1", "manual", "manual_from_ticket"}

    st = stats(hide_manual=True, session=book)
    assert st.trades == 1 and st.total_r == 2.0                    # the desk ticket's -2R is out
    assert stats(session=book).trades == 2

    sv = by_session(hide_manual=True, session=book)
    assert sv.trades == 1 and all(x.source == "strategy:1" for s in sv.sessions for x in s.sources)

    rv = daily_review(hide_manual=True, session=book)
    assert [t["source"] for t in rv["trades"]] == ["strategy:1"]
    assert rv["net_pnl"] == 20.0 and rv["longest_losing_run"] == 0
    assert daily_review(session=book)["net_pnl"] == -2005.0        # the desk's own readers still see the day


def test_the_query_flag_reaches_the_cards(book, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: book
    try:
        c = TestClient(app)
        assert [t["source"] for t in c.get("/api/journal/trades?hide_manual=true").json()] == ["strategy:1"]
        assert len(c.get("/api/journal/trades").json()) == 4
        assert c.get("/api/journal/sessions?hide_manual=true").json()["trades"] == 1
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_reflection_leaves_out_the_hand_opened_rows_when_asked(book):
    from app.agents.reflection import _closed_positions

    assert {p.source for p in _closed_positions(book)} == {"strategy:1", "manual"}
    assert {p.source for p in _closed_positions(book, hide_manual=True)} == {"strategy:1"}
