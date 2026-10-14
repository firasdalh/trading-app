"""GET /api/positions/live carries the stop each trade was OPENED with (UX review 2026-10-14).

The desk measured R against the CURRENT stop, so widening a stop made the loss shown in R smaller
(EUR showed -0.58R in the sidebar while the advisor said -1.21R). R is now measured against the risk
taken at entry everywhere, which needs the entry stop on the live position. POST /positions/sl-tp
overwrites Position.stop_loss, so the source is the executor's Order row (the stop it submitted),
with the executed proposal as the fallback, matched to the trade by its open time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.db import Order, Position, TradeProposalRecord
from app.models.enums import OrderStatus, PositionStatus, ProposalStatus
from app.models.schemas import PositionView

OPEN_AT = datetime(2026, 10, 13, 17, 14, 39, tzinfo=timezone.utc)


def _live(symbol="EURUSDm", direction="short", stop=1.12238, opened_at=OPEN_AT, entry=1.11913):
    return PositionView(id=99, symbol=symbol, asset_class="forex", direction=direction, qty=0.24,
                        entry_price=entry, stop_loss=stop, take_profit=1.1167, status="open",
                        last_price=1.12108, unrealized_pnl=-46.8, opened_at=opened_at)


def _order(db, *, symbol="EURUSDm", side="sell", stop=1.120607, at=OPEN_AT, status=OrderStatus.FILLED):
    o = Order(symbol=symbol, asset_class="forex", side=side, order_type="market", qty=0.24,
              stop_loss=stop, take_profit=1.114578, status=status.value,
              created_at=at.replace(tzinfo=None))   # SQLite keeps it naive, like the real rows
    db.add(o)
    db.commit()
    return o


def _get(db, monkeypatch, positions):
    from app.api.settings_routes import live_positions
    from app.risk import service
    monkeypatch.setattr(service, "live_broker_positions", lambda session: positions)
    return live_positions(db)


def test_initial_stop_comes_from_the_submitted_order_not_the_moved_stop(db_session, monkeypatch):
    # The app's own row was overwritten by a manual widen (sl-tp mirrors the new stop into it).
    db_session.add(Position(symbol="EURUSDm", asset_class="forex", direction="short", qty=0.24,
                            entry_price=1.11913, stop_loss=1.12238, take_profit=1.1167,
                            status=PositionStatus.OPEN.value, risk_amount=77.98,
                            opened_at=(OPEN_AT + timedelta(seconds=1)).replace(tzinfo=None)))
    _order(db_session, at=OPEN_AT + timedelta(milliseconds=200))
    out = _get(db_session, monkeypatch, [_live()])
    assert out[0].stop_loss == 1.12238              # the live stop is untouched
    assert out[0].initial_stop_loss == 1.120607     # the stop it was opened with


def test_an_order_from_an_earlier_trade_is_not_used(db_session, monkeypatch):
    # Same pair and side, but days before this trade opened (a since-closed trade).
    _order(db_session, stop=1.1215, at=OPEN_AT - timedelta(days=4))
    out = _get(db_session, monkeypatch, [_live()])
    assert out[0].initial_stop_loss is None


def test_the_opposite_side_and_unfilled_orders_are_ignored(db_session, monkeypatch):
    _order(db_session, side="buy", stop=1.1150)
    _order(db_session, stop=1.1300, status=OrderStatus.REJECTED)
    out = _get(db_session, monkeypatch, [_live()])
    assert out[0].initial_stop_loss is None


def test_nearest_order_wins_when_several_are_in_the_window(db_session, monkeypatch):
    _order(db_session, stop=1.1210, at=OPEN_AT - timedelta(minutes=9))
    _order(db_session, stop=1.120607, at=OPEN_AT + timedelta(seconds=1))
    out = _get(db_session, monkeypatch, [_live()])
    assert out[0].initial_stop_loss == 1.120607


def test_falls_back_to_the_executed_proposal(db_session, monkeypatch):
    db_session.add(TradeProposalRecord(symbol="EURUSDm", asset_class="forex", timeframe="1h",
                                       direction="short", entry=1.1190, stop_loss=1.1206, take_profit=1.1145,
                                       status=ProposalStatus.EXECUTED.value,
                                       created_at=(OPEN_AT - timedelta(hours=3)).replace(tzinfo=None)))
    db_session.commit()
    out = _get(db_session, monkeypatch, [_live()])
    assert out[0].initial_stop_loss == 1.1206


def test_a_terminal_trade_has_no_initial_stop(db_session, monkeypatch):
    # Opened directly in MT5: no order, no proposal, no app row -> None (UI uses the current stop).
    out = _get(db_session, monkeypatch, [_live(symbol="XAUUSDm", stop=4181.881, entry=4157.365)])
    assert out[0].initial_stop_loss is None


def test_matches_through_the_apps_own_row_when_the_broker_clock_is_offset(db_session, monkeypatch):
    # Broker server time 3h ahead of UTC: its fill stamp misses the order, the app's open row hits it.
    db_session.add(Position(symbol="EURUSDm", asset_class="forex", direction="short", qty=0.24,
                            entry_price=1.11913, stop_loss=1.12238, take_profit=1.1167,
                            status=PositionStatus.OPEN.value, opened_at=OPEN_AT.replace(tzinfo=None)))
    _order(db_session)
    out = _get(db_session, monkeypatch, [_live(opened_at=OPEN_AT + timedelta(hours=3))])
    assert out[0].initial_stop_loss == 1.120607


def test_a_lookup_failure_never_costs_the_position_list(db_session, monkeypatch):
    from app.api import settings_routes

    def boom(session, positions):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(settings_routes, "_fill_initial_stops", boom)
    out = _get(db_session, monkeypatch, [_live()])
    assert len(out) == 1 and out[0].initial_stop_loss is None and out[0].id == 1
