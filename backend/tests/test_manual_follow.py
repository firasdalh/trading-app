"""Follow the target on a manual trade (2026-11-22): the app keeps the soft target - the user moves it - and the
broker's take profit sits 2R past it; at the target the strategy's rule: 30s SuperTrend with the trade -> lock the
stop just short of the target and trail the line, otherwise take the target."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.agents.manual_follow as mf
import app.agents.strategy as st
import app.brokers.registry as registry
from app.models.db import AgentRun, Position
from app.models.enums import OrderStatus, PositionStatus
from app.models.schemas import OrderResult, PositionView

NOW = datetime(2026, 11, 23, 9, 0, 7, tzinfo=timezone.utc)
SYM = "EURUSDm"


class FakeBroker:
    name = "fake"
    is_paper = True

    def __init__(self, positions, bid, stops_level=0.0, sl_status="submitted"):
        self.positions = positions
        self.bid = bid
        self.stops_level = stops_level
        self.sl_status = sl_status
        self.moves, self.closes = [], []

    def get_open_positions(self):
        return list(self.positions)

    def scalp_spec(self, symbol):
        return {"bid": self.bid, "ask": round(self.bid + 0.0001, 5), "spread": 0.0001, "digits": 5,
                "stops_level": self.stops_level}

    def set_sl_tp(self, symbol, stop_loss=None, take_profit=None, direction=None):
        self.moves.append((symbol, stop_loss, take_profit, direction))
        if self.sl_status == "submitted":
            for p in self.positions:
                if p.direction == direction:
                    if stop_loss is not None:
                        p.stop_loss = stop_loss
                    if take_profit is not None:
                        p.take_profit = take_profit
        return OrderResult(status=OrderStatus(self.sl_status), error=None if self.sl_status == "submitted" else "Invalid stops")

    def close_position(self, symbol, direction=None):
        self.closes.append((symbol, direction))
        self.positions = [p for p in self.positions if p.direction != direction]
        return OrderResult(status=OrderStatus.FILLED, avg_fill_price=self.bid)

    def get_closed_trades(self, since):
        return []


def _pos(side="long", entry=1.1000, stop=1.0980, tp=1.1018, n=1):
    return [PositionView(id=i + 1, symbol=SYM, asset_class="forex", direction=side, qty=0.1, entry_price=entry,
                         stop_loss=stop, take_profit=tp, status="open", last_price=entry, unrealized_pnl=0.0)
            for i in range(n)]


@pytest.fixture()
def broker(monkeypatch):
    holder = {}

    def make(positions, bid, **kw):
        b = FakeBroker(positions, bid, **kw)
        holder["b"] = b
        monkeypatch.setattr(registry, "get_broker_for", lambda *a, **k: holder["b"])
        return b
    return make


def _read(monkeypatch, direction, line=None):
    calls = []

    def fake(broker, symbol, now):
        calls.append(now)
        return (direction, line)
    monkeypatch.setattr(st, "_follow_read", fake)
    return calls


def _entry(db_session):
    return mf._entries(db_session).get(f"{SYM.upper()}|long") or mf._entries(db_session).get("EURUSD|long") \
        or next(iter(mf._entries(db_session).values()), None)


# ----------------------------------------------------------------- switching it on

def test_on_the_broker_tp_goes_2r_past_the_target_and_the_app_keeps_the_target(db_session, broker):
    b = broker(_pos(), bid=1.1005)
    fe = mf.start(db_session, SYM, "forex", "long")        # the take profit now is the target
    assert fe["target"] == 1.1018 and fe["risk"] == pytest.approx(0.0020)
    assert fe["broker_tp"] == pytest.approx(1.1018 + 2 * 0.0020)
    assert fe["lock"] == pytest.approx(1.1018 - 0.1 * 0.0020)
    assert b.moves == [(SYM, None, pytest.approx(1.1058), "long")]       # only the take profit; the stop untouched
    assert fe["mode"] == "waiting" and _entry(db_session)["target"] == 1.1018
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_on")).one()
    assert ev.agent == "manual" and ev.detail["was_tp"] == 1.1018


def test_on_with_a_target_given(db_session, broker):
    broker(_pos(tp=None), bid=1.1005)
    fe = mf.start(db_session, SYM, "forex", "long", target=1.1030)
    assert fe["target"] == 1.1030 and fe["broker_tp"] == pytest.approx(1.1070)


@pytest.mark.parametrize("kw,target,bid,why", [
    ({"stop": None}, None, 1.1005, "needs a stop"),
    ({}, 1.1003, 1.1005, "above the current price"),       # behind the price: it would fire at once
    ({}, 1.0990, 1.0985, "profit side of the entry"),
    ({"n": 2}, None, 1.1005, "tickets are open"),
])
def test_on_is_refused_with_the_reason(db_session, broker, kw, target, bid, why):
    b = broker(_pos(**kw), bid=bid)
    with pytest.raises(mf.FollowRefused, match=why):
        mf.start(db_session, SYM, "forex", "long", target=target)
    assert b.moves == [] and mf._entries(db_session) == {}


def test_a_strategy_trade_is_left_to_its_board(db_session, broker):
    broker(_pos(), bid=1.1005)
    db_session.add(Position(symbol=SYM, asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                            stop_loss=1.098, take_profit=1.1018, status=PositionStatus.OPEN.value, source="strategy:1"))
    db_session.commit()
    with pytest.raises(mf.FollowRefused, match="strategy's trade"):
        mf.start(db_session, SYM, "forex", "long")


def test_a_refused_safety_target_switches_nothing_on(db_session, broker):
    broker(_pos(), bid=1.1005, sl_status="rejected")
    with pytest.raises(mf.FollowRefused, match="refused the safety"):
        mf.start(db_session, SYM, "forex", "long")
    assert mf._entries(db_session) == {}


# ----------------------------------------------------------------- moving it, switching it off

def test_moving_the_target_moves_the_lock_and_the_safety_tp(db_session, broker):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    fe = mf.move(db_session, SYM, "long", 1.1025)
    assert fe["target"] == 1.1025 and fe["lock"] == pytest.approx(1.1023) and fe["broker_tp"] == pytest.approx(1.1065)
    assert b.moves[-1] == (SYM, None, pytest.approx(1.1065), "long")
    with pytest.raises(mf.FollowRefused, match="above the current price"):
        mf.move(db_session, SYM, "long", 1.1004)
    assert _entry(db_session)["target"] == 1.1025


def test_off_while_waiting_puts_the_take_profit_back_on_the_target(db_session, broker):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    out = mf.stop(db_session, SYM, "long")
    assert out == {"off": True, "take_profit": 1.1018}
    assert b.moves[-1] == (SYM, None, 1.1018, "long") and mf._entries(db_session) == {}


# ----------------------------------------------------------------- every second

def test_short_of_the_target_nothing_happens(db_session, broker, monkeypatch):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    reads = _read(monkeypatch, 1, 1.1010)
    b.bid, b.moves = 1.1017, []
    assert mf.tick(db_session, NOW) == [] and b.moves == [] and reads == [] and b.closes == []


def test_at_the_target_with_the_30s_supertrend_on_its_side_the_stop_locks_then_trails(db_session, broker, monkeypatch):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    _read(monkeypatch, 1, 1.1010)
    b.bid, b.moves = 1.1019, []
    assert mf.tick(db_session, NOW) == [{"symbol": SYM, "following": "long"}]
    assert b.moves == [(SYM, pytest.approx(1.1016), None, "long")] and b.closes == []
    fe = _entry(db_session)
    assert fe["mode"] == "following" and fe["mark"] == st.bar_mark("30s", NOW) and "wait_until" not in fe
    # the same 30s bar: nothing new
    assert mf.tick(db_session, NOW + timedelta(seconds=1)) == []
    # the next 30s close: the line is above the lock -> the stop moves up to it
    _read(monkeypatch, 1, 1.10175)
    b.bid = 1.1025
    assert mf.tick(db_session, NOW + timedelta(seconds=30)) == [{"symbol": SYM, "stop": pytest.approx(1.10175)}]
    # the line turns: the trade is closed
    _read(monkeypatch, -1, 1.1030)
    assert mf.tick(db_session, NOW + timedelta(seconds=60))[0]["closed"] == "long"
    assert b.closes == [(SYM, "long")] and mf._entries(db_session) == {}
    assert db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_closed")).one().detail["side"] == "long"


def test_at_the_target_with_the_supertrend_against_it_the_target_is_taken(db_session, broker, monkeypatch):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    _read(monkeypatch, -1, 1.1030)
    b.bid = 1.1019
    out = mf.tick(db_session, NOW)
    assert out[0]["closed"] == "long" and "not with the trade" in out[0]["why"]
    assert b.closes == [(SYM, "long")] and mf._entries(db_session) == {}


def test_a_trade_closed_elsewhere_drops_its_entry(db_session, broker, monkeypatch):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    b.positions = []                                   # its stop, or closed by hand
    assert mf.tick(db_session, NOW) == [{"symbol": SYM, "ended": "long"}]
    assert mf._entries(db_session) == {}


def test_a_second_ticket_on_the_side_at_the_target_leaves_it_to_its_stop(db_session, broker, monkeypatch):
    b = broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    b.positions = _pos(n=2)
    _read(monkeypatch, 1, 1.1010)
    b.bid, b.moves = 1.1019, []
    assert mf.tick(db_session, NOW) == [] and b.moves == [] and b.closes == []
    assert _entry(db_session)["mode"] == "shared"


def test_the_live_positions_say_it_follows_and_where(db_session, broker):
    broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    lv = mf.levels(db_session)
    (sym, side), fe = next(iter(lv.items()))
    assert side == "long" and fe["target"] == 1.1018 and fe["mode"] == "waiting"


def test_the_strategy_tick_runs_it(db_session, broker, monkeypatch):
    broker(_pos(), bid=1.1005)
    mf.start(db_session, SYM, "forex", "long")
    seen = []
    monkeypatch.setattr(mf, "tick", lambda session, now=None: seen.append(now) or [])
    st.strategy_tick(db_session, NOW)
    assert seen == [NOW]
