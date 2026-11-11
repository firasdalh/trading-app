"""Trading sessions (app.core.sessions) and the journal's trades by session (GET /api/journal/sessions; asked
2026-11-10: "in which session ... to know which session is performing with us better")."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import sessions as sess
from app.models.schemas import PositionView


def _at(h, m=0):
    return datetime(2026, 11, 9, h, m, tzinfo=timezone.utc)


def test_every_utc_hour_falls_in_exactly_one_session():
    covered = [h for s in sess.SESSIONS for h in range(s["start"], s["end"])]
    assert sorted(covered) == list(range(24))
    assert [s["key"] for s in sess.SESSIONS] == ["asia", "middle_east", "london", "new_york", "between"]


@pytest.mark.parametrize("hm, key", [
    ((0, 0), "asia"), ((5, 59), "asia"), ((6, 0), "middle_east"), ((7, 59), "middle_east"),
    ((8, 0), "london"), ((12, 59), "london"), ((13, 0), "new_york"), ((20, 59), "new_york"),
    ((21, 0), "between"), ((23, 59), "between"),
])
def test_the_session_a_time_falls_in(hm, key):
    assert sess.session_of(_at(*hm)) == key


def test_naive_times_are_utc_and_other_zones_are_converted():
    dubai = timezone(timedelta(hours=4))
    assert sess.session_of(datetime(2026, 11, 9, 7, 30)) == "middle_east"                     # naive = UTC
    assert sess.session_of(datetime(2026, 11, 9, 10, 30, tzinfo=dubai)) == "middle_east"      # 06:30 UTC
    assert sess.session_of(datetime(2026, 11, 9, 1, 30, tzinfo=dubai)) == "between"           # 21:30 UTC, day before
    assert sess.session_of(None) is None
    assert sess.session_at(6) == "middle_east" and sess.session_at(24) == "asia"


def _trade(i, opened, pnl, source="strategy:1", closed=None):
    return PositionView(id=i, symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=100.0,
                        status="closed", last_price=101.0, realized_pnl=pnl, source=source, opened_at=opened,
                        closed_at=closed or (opened + timedelta(minutes=5)))


def test_the_journal_counts_each_trade_in_the_session_it_opened(db_session, monkeypatch):
    from app.api import journal_routes as jr

    trades = [
        _trade(1, _at(2), 10.0), _trade(2, _at(3), -4.0),                             # Asia: 1 win, 1 loss
        _trade(3, _at(7, 15), 6.0, source="manual"),                                  # Middle East
        _trade(4, _at(9), 5.0), _trade(5, _at(10), 3.0), _trade(6, _at(11), -2.0),    # London: 2 wins, 1 loss
        _trade(7, _at(20, 50), -7.0, closed=_at(21, 20)),                            # opened in New York
        _trade(8, None, 50.0, closed=_at(12)),                                        # no open time: left out
        _trade(9, _at(4), None),                                                      # no booked result: left out
    ]
    monkeypatch.setattr("app.risk.service.broker_closed_trades", lambda s: list(trades))
    view = jr.by_session(session=db_session)
    by = {s.key: s for s in view.sessions}
    assert [s.key for s in view.sessions] == ["asia", "middle_east", "london", "new_york", "between"]
    assert view.trades == 7 and view.skipped == 2 and view.since == _at(2)
    asia = by["asia"].total
    assert (asia.trades, asia.wins, asia.losses, asia.net_pnl, asia.win_rate, asia.profit_factor) == \
        (2, 1, 1, 6.0, 0.5, 2.5)
    assert by["middle_east"].total.trades == 1 and by["middle_east"].sources[0].source == "manual"
    assert by["middle_east"].total.profit_factor is None                       # no losing trade
    assert by["london"].total.net_pnl == 6.0 and by["london"].total.avg_pnl == 2.0
    assert [(x.source, x.stat.trades) for x in by["london"].sources] == [("strategy:1", 3)]
    # By the OPEN: closed at 21:20, it still belongs to New York.
    assert by["new_york"].total.trades == 1 and by["between"].total.trades == 0
    assert by["between"].total.win_rate is None and by["between"].total.avg_pnl is None
    assert (by["middle_east"].start_utc, by["middle_east"].end_utc) == (6, 8)
    assert [h.hour_utc for h in view.hours] == list(range(24))
    assert view.hours[7].session == "middle_east" and view.hours[7].total.trades == 1
    assert view.hours[20].total.net_pnl == -7.0 and view.hours[21].total.trades == 0


def test_the_trade_log_says_which_session_each_trade_opened_in(db_session, monkeypatch):
    from app.api import journal_routes as jr

    trades = [_trade(1, _at(7), 5.0), _trade(2, _at(14), -1.0), _trade(3, None, 2.0, closed=_at(15))]
    monkeypatch.setattr("app.risk.service.broker_closed_trades", lambda s: list(trades))
    out = jr.closed_trades(limit=10, session=db_session)
    assert [t.session for t in out] == ["middle_east", "new_york", None]
    assert len(jr.closed_trades(limit=1, session=db_session)) == 1


def test_without_broker_history_the_apps_own_rows_get_their_session(db_session, monkeypatch):
    from app.api import journal_routes as jr
    from app.models.db import Position
    from app.models.enums import PositionStatus

    monkeypatch.setattr("app.risk.service.broker_closed_trades", lambda s: None)
    db_session.add(Position(symbol="EURUSDm", asset_class="forex", direction="short", qty=1.0, entry_price=1.1,
                            status=PositionStatus.CLOSED.value, realized_pnl=-3.0, source="manual",
                            opened_at=datetime(2026, 11, 9, 9, 0), closed_at=datetime(2026, 11, 9, 9, 30)))
    db_session.commit()
    assert [t.session for t in jr.closed_trades(limit=10, session=db_session)] == ["london"]
    view = jr.by_session(session=db_session)
    assert {s.key: s.total.trades for s in view.sessions}["london"] == 1 and view.since == datetime(
        2026, 11, 9, 9, 0, tzinfo=timezone.utc)
