"""The Journal's cards on the account's own trades (2026-11-11, the user on a screenshot of the Journal: "is it
correct? Fix it if not."): the broker's history, with MT5's tag for who opened each trade, R from the app's own row
for the SAME trade, and never a demo account's trades in a live account's numbers."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.db import Position
from app.models.enums import PositionStatus
from app.models.schemas import PositionView

T0 = datetime(2026, 11, 10, 14, 24, 3, tzinfo=timezone.utc)


def _bt(i, source, pnl, opened, entry=100.0, symbol="BTCUSDm", direction="short"):
    """A broker trade as the MT5 adapter reads it: `source` from its entry deal's comment."""
    return PositionView(id=i, symbol=symbol, asset_class="crypto", direction=direction, qty=0.1, entry_price=entry,
                        status="closed", last_price=entry, unrealized_pnl=0.0, realized_pnl=pnl, opened_at=opened,
                        closed_at=opened + timedelta(minutes=20), source=source)


def _row(session, source, opened, risk=10.0, pnl=0.0, entry=100.0, env="live", symbol="BTCUSDm", direction="short"):
    """The app's own record of a trade it placed (its P&L is the app's estimate; the broker's is the truth)."""
    p = Position(symbol=symbol, asset_class="crypto", direction=direction, qty=0.1, entry_price=entry,
                 status=PositionStatus.CLOSED.value, last_price=entry, realized_pnl=pnl, risk_amount=risk,
                 source=source, opened_at=opened, closed_at=opened + timedelta(minutes=20), broker_env=env)
    session.add(p)
    session.commit()
    return p


@pytest.fixture
def broker(monkeypatch):
    """The broker's history the journal reads, set per test (a fresh copy per read: the journal tags the rows)."""
    import app.risk.service as svc

    held = {"trades": []}
    monkeypatch.setattr(svc, "broker_closed_trades", lambda session: [t.model_copy() for t in held["trades"]])
    return held


def test_the_breakdown_counts_the_phone_and_reads_r_from_the_apps_row(db_session, broker):
    from app.api.journal_routes import breakdown

    _row(db_session, "strategy:1", T0, risk=10.0, pnl=18.0)                             # the app's record: $18
    _row(db_session, "strategy:1", T0 - timedelta(days=3), risk=10.0, pnl=-50.0, env="paper")   # the demo account's
    broker["trades"] = [_bt(1, "strategy:1", 20.0, T0 + timedelta(seconds=1)),           # the broker booked $20
                        _bt(2, "manual", -30.0, T0 + timedelta(seconds=40))]             # the phone, same pair + side
    by = {g.label: g for g in breakdown(session=db_session).by_source}
    assert set(by) == {"strategy:1", "manual"}
    assert by["strategy:1"].trades == 1 and by["strategy:1"].net_pnl == 20.0 and by["strategy:1"].total_r == 2.0
    assert by["manual"].trades == 1 and by["manual"].net_pnl == -30.0 and by["manual"].total_r is None


def test_performance_counts_this_accounts_trades_with_a_risk_in_the_brokers_dollars(db_session, broker):
    """The screenshot's card read 480 trades: 227 were the demo account's (-$1,577)."""
    from app.api.journal_routes import stats

    _row(db_session, "strategy:1", T0, risk=10.0, pnl=18.0)
    _row(db_session, "manual", T0 + timedelta(hours=1), risk=20.0, pnl=-19.0)          # a desk trade the app placed
    for k in range(5):                                                                 # the demo account's losers
        _row(db_session, "strategy:1", T0 - timedelta(days=5, hours=k), risk=10.0, pnl=-10.0, env="paper")
    broker["trades"] = [_bt(1, "strategy:1", 20.0, T0 + timedelta(seconds=1)),
                        _bt(2, "manual", -20.0, T0 + timedelta(hours=1, seconds=2)),
                        _bt(3, "manual", -99.0, T0 + timedelta(hours=2))]               # the phone: no risk, no R
    s = stats(session=db_session)
    assert s.trades == 2 and s.wins == 1 and s.losses == 1
    assert s.total_r == 1.0 and s.expectancy_r == 0.5            # +20/10 = +2R, -20/20 = -1R: the broker's $
    assert s.profit_factor == 1.0


def test_by_pair_for_each_source(db_session, broker):
    """2026-11-11: "By pair (all-time) - I want it also for the strategy"."""
    from app.api.journal_routes import breakdown

    _row(db_session, "strategy:1", T0, risk=10.0)
    broker["trades"] = [_bt(1, "strategy:1", 20.0, T0 + timedelta(seconds=1)),
                        _bt(2, "strategy:1", -5.0, T0 + timedelta(hours=1), symbol="XAUUSDm", entry=4000.0),
                        _bt(3, "manual", -30.0, T0 + timedelta(hours=2))]
    b = breakdown(session=db_session)
    assert {g.label: g.net_pnl for g in b.by_pair} == {"BTCUSDm": -10.0, "XAUUSDm": -5.0}
    mine = b.by_pair_source["strategy:1"]
    assert [(g.label, g.trades, g.net_pnl) for g in mine] == [("BTCUSDm", 1, 20.0), ("XAUUSDm", 1, -5.0)]
    assert mine[0].total_r == 2.0                                 # R where the app's row holds the risk
    assert [(g.label, g.net_pnl) for g in b.by_pair_source["manual"]] == [("BTCUSDm", -30.0)]


def test_a_legacy_tag_still_finds_its_row_for_the_source_and_the_r(db_session, broker):
    from app.api.journal_routes import breakdown

    _row(db_session, "hybrid", T0, risk=5.0, pnl=4.0, symbol="USDJPYm", entry=155.2)
    broker["trades"] = [_bt(1, "app", 5.0, T0 + timedelta(seconds=1), entry=155.21, symbol="USDJPYm")]
    by = {g.label: g for g in breakdown(session=db_session).by_source}
    assert by["hybrid"].trades == 1 and by["hybrid"].total_r == 1.0


def test_the_equity_series_is_every_trade_oldest_first_with_r_where_the_app_kept_the_risk(db_session, broker):
    """2026-11-11: "enhance the performance visualization" - the Journal's charts draw this."""
    from app.api.journal_routes import equity

    _row(db_session, "strategy:1", T0, risk=10.0, pnl=18.0)
    broker["trades"] = [_bt(2, "manual", -30.0, T0 + timedelta(hours=2)),               # newest first, as MT5 gives
                        _bt(1, "strategy:1", 20.0, T0 + timedelta(seconds=1))]
    pts = equity(session=db_session)
    assert [(p.source, p.pnl, p.r) for p in pts] == [("strategy:1", 20.0, 2.0), ("manual", -30.0, None)]
    assert pts[0].closed_at < pts[1].closed_at and pts[0].closed_at.tzinfo is not None
    assert pts[0].symbol == "BTCUSDm" and pts[0].direction == "short"


def test_score_vs_result_leaves_out_trades_without_a_score(db_session, monkeypatch):
    """A strategy's trades carry confidence 0.0: all 191 sat in "0-50%" as if the engine had scored them low."""
    import app.risk.service as svc
    from app.api.journal_routes import calibration
    from app.core.state import get_or_create_settings

    monkeypatch.setattr(svc, "broker_closed_trades", lambda session: None)
    get_or_create_settings(db_session).broker_env = "live"
    db_session.commit()
    strat = _row(db_session, "strategy:1", T0, pnl=10.0)
    hyb = _row(db_session, "hybrid", T0 + timedelta(hours=1), pnl=-5.0)
    strat.confidence, hyb.confidence = 0.0, 0.85
    db_session.commit()
    by = {b.bucket: b.trades for b in calibration(session=db_session)}
    assert by["0-50%"] == 0 and by["80-90%"] == 1


def test_todays_review_is_the_brokers_day_with_the_phone_in(db_session, broker):
    """The header's DAY P&L counts the broker's day; Today's review read the app's rows (no phone trade, the app's
    estimate of each P&L) and averaged its tags over the demo account's trades too."""
    from app.api.journal_routes import daily_review

    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    mine = _row(db_session, "manual", day + timedelta(minutes=1), risk=10.0, pnl=11.0)   # a desk trade, tagged
    mine.review = {"note": "plan", "tags": ["plan"]}
    for k in range(12):                                                                   # the demo's tagged trades
        demo = _row(db_session, "manual", day - timedelta(days=9, hours=k), risk=10.0, pnl=-10.0, env="paper")
        demo.review = {"tags": ["plan"]}
    db_session.commit()
    broker["trades"] = [_bt(3, "manual", -40.0, day + timedelta(minutes=30)),            # the phone
                        _bt(2, "manual", 12.0, day + timedelta(minutes=1, seconds=2)),    # the desk trade: $12 booked
                        _bt(1, "strategy:1", 5.0, day - timedelta(hours=3))]              # yesterday
    rv = daily_review(session=db_session)
    assert [(t["pnl"], t["r"], t["tags"]) for t in rv["trades"]] == [(12.0, 1.2, ["plan"]), (-40.0, None, [])]
    assert rv["net_pnl"] == -28.0 and rv["total_r"] == 1.2 and rv["longest_losing_run"] == 1
    assert rv["by_tag"] == [] and rv["tags_below_10"] == ["plan"]          # 1 trade on this account, not 13


def test_without_broker_history_the_journal_keeps_to_the_account_it_trades(db_session, monkeypatch):
    import app.risk.service as svc
    from app.api.journal_routes import _journal_trades, breakdown, calibration, stats
    from app.core.state import get_or_create_settings

    monkeypatch.setattr(svc, "broker_closed_trades", lambda session: None)
    get_or_create_settings(db_session).broker_env = "live"
    db_session.commit()
    live = _row(db_session, "strategy:1", T0, risk=10.0, pnl=10.0)
    demo = _row(db_session, "strategy:1", T0 - timedelta(days=5), risk=10.0, pnl=-10.0, env="paper")
    live.confidence = demo.confidence = 0.75
    db_session.commit()
    assert [t.id for t in _journal_trades(db_session)] == [live.id]
    assert stats(session=db_session).trades == 1
    assert [g.trades for g in breakdown(session=db_session).by_source] == [1]
    by = {b.bucket: b for b in calibration(session=db_session)}
    assert by["70-80%"].trades == 1 and by["70-80%"].wins == 1
