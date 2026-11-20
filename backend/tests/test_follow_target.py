"""Follow the target (2026-11-19, strategy follow_target): the broker's take profit goes further out as a safety net
and the engine keeps the real target itself - at the target, with the 30s SuperTrend still the trade's way, the stop
is locked just under it and then follows that line; otherwise the trade is closed there, as the broker's would."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.agents.strategy as st
from app.models.db import AgentRun, Position
from app.models.enums import OrderStatus, PositionStatus
from app.models.schemas import OrderResult
from tests.test_strategy import NOW, RISING, _strategy, engine  # noqa: F401 - engine: the fixture

SYM = "BTCUSDm"


def _quote(holder, bid, ask=None, stops_level=0.0):
    holder["broker"].scalp_spec = lambda symbol: {"bid": bid, "ask": ask if ask is not None else bid + 0.1,
                                                  "spread": 0.1, "digits": 2, "stops_level": stops_level}


def _moves(holder, status="submitted"):
    moves = []

    def set_sl_tp(symbol, stop_loss=None, take_profit=None, direction=None):
        moves.append((symbol, stop_loss, take_profit, direction))
        return OrderResult(status=OrderStatus(status), error=None if status == "submitted" else "Invalid stops")
    holder["broker"].set_sl_tp = set_sl_tp
    return moves


def _read(monkeypatch, direction, line=None):
    calls = []

    def fake(broker, symbol, now):
        calls.append(now)
        return (direction, line)
    monkeypatch.setattr(st, "_follow_read", fake)
    return calls


def _following(db_session, cfg, side="long", mode="waiting", target=112.0, lock=111.8, stop=108.0, **extra):
    """A strategy trade whose target the app keeps: entry 110, 1R = 2 -> target 0.9R = 111.8... (here 112 / 111.8)."""
    pos = Position(symbol=SYM, asset_class="crypto", direction=side, qty=0.1, entry_price=110.0, stop_loss=stop,
                   take_profit=116.0 if side == "long" else 104.0, status=PositionStatus.OPEN.value,
                   source=f"strategy:{cfg.id}", opened_at=NOW - timedelta(minutes=20), risk_amount=5.0,
                   initial_risk_amount=5.0)
    db_session.add(pos)
    cfg.state = {SYM: {"follow": {"side": side, "mode": mode, "target": target, "lock": lock, "risk": 2.0, **extra}}}
    db_session.commit()
    return pos


def _fe(cfg):
    return (cfg.state or {}).get(SYM, {}).get("follow")


# ----------------------------------------------------------------- the order

def test_with_follow_on_the_broker_gets_a_safety_target_and_the_app_keeps_the_real_one(db_session, engine):  # noqa: F811
    calls, _ = engine
    cfg = _strategy(db_session, target_r=0.9, follow_target=True, exit_rules=[])
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert out[0].get("opened") == "long" and "follow" not in out[0]
    prop = calls["assess"][0][0]
    risk = prop.entry - prop.stop_loss
    assert prop.take_profit == pytest.approx(prop.entry + (0.9 + st.FOLLOW_SAFETY_R) * risk, abs=0.011)
    fe = _fe(cfg)
    assert fe["mode"] == "waiting" and fe["side"] == "long"
    assert fe["target"] == pytest.approx(prop.entry + 0.9 * risk, abs=0.011)
    assert fe["lock"] == pytest.approx(fe["target"] - st.FOLLOW_LOCK_R * risk, abs=0.011)
    opened = db_session.scalars(select(AgentRun).where(AgentRun.event == "opened")).one().detail
    assert opened["target"] == fe["target"] and opened["broker_tp"] == prop.take_profit and opened["follow"] is True
    # The board says the app keeps it.
    st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))
    assert "the app keeps its target" in cfg.state[SYM]["note"]


def test_without_follow_the_target_is_the_brokers_as_before(db_session, engine):  # noqa: F811
    calls, _ = engine
    cfg = _strategy(db_session, target_r=0.9, exit_rules=[])
    st.run_strategy(db_session, cfg, now=NOW)
    prop = calls["assess"][0][0]
    assert prop.take_profit == pytest.approx(prop.entry + 0.9 * (prop.entry - prop.stop_loss), abs=0.011)
    assert _fe(cfg) is None


# ----------------------------------------------------------------- at the target

def test_short_of_the_target_nothing_happens(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    moves = _moves(holder)
    reads = _read(monkeypatch, 1, 109.0)
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg)
    _quote(holder, 111.9)
    st.follow_targets(db_session, cfg, NOW)
    assert moves == [] and calls["close"] == [] and reads == []
    assert _fe(cfg)["mode"] == "waiting"


def test_at_the_target_with_the_30s_supertrend_on_its_side_the_stop_is_locked_and_it_follows(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    moves = _moves(holder)
    _read(monkeypatch, 1, 110.5)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)
    _quote(holder, 112.05)
    out = st.follow_targets(db_session, cfg, NOW)
    assert out == [{"symbol": SYM, "following": "long"}]
    assert moves == [(SYM, 111.8, None, "long")]                     # the safety target is left where it is
    assert calls["close"] == []
    db_session.refresh(pos)
    assert pos.stop_loss == 111.8 and pos.risk_amount == 0.0
    fe = _fe(cfg)
    assert fe["mode"] == "following" and fe["mark"] == st.bar_mark("30s", NOW) and fe["lock"] == 111.8
    assert "past its target, following" in cfg.state[SYM]["note"]
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_started")).one().detail
    assert ev["target"] == 112.0 and ev["stop"] == 111.8 and ev["line"] == 110.5


def test_a_sell_reads_the_ask_the_other_way_round(db_session, engine, monkeypatch):  # noqa: F811
    _, holder = engine
    moves = _moves(holder)
    _read(monkeypatch, -1, 109.5)
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg, side="short", target=108.0, lock=108.2, stop=112.0)
    _quote(holder, 108.0, ask=108.1)                                  # the bid is there, the ask is not yet
    st.follow_targets(db_session, cfg, NOW)
    assert moves == []
    _quote(holder, 107.85, ask=107.95)
    st.follow_targets(db_session, cfg, NOW)
    assert moves == [(SYM, 108.2, None, "short")] and _fe(cfg)["mode"] == "following"


def test_at_the_target_with_the_30s_supertrend_against_it_the_target_is_taken(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    moves = _moves(holder)
    _read(monkeypatch, -1, 113.0)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)
    _quote(holder, 112.0)
    st.follow_targets(db_session, cfg, NOW)
    assert moves == [] and [c[0] for c in calls["close"]] == [pos.id]
    assert "30s SuperTrend is not with the trade, so it is taken" in calls["close"][0][1]
    assert _fe(cfg) is None


def test_an_unreadable_supertrend_takes_the_target(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    monkeypatch.setattr(st, "_follow_read", lambda broker, symbol, now: None)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)
    _quote(holder, 112.3)
    st.follow_targets(db_session, cfg, NOW)
    assert [c[0] for c in calls["close"]] == [pos.id]


def test_a_refused_lock_takes_the_target(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    _moves(holder, status="error")
    _read(monkeypatch, 1, 110.5)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)
    _quote(holder, 112.1)
    st.follow_targets(db_session, cfg, NOW)
    assert [c[0] for c in calls["close"]] == [pos.id] and "could not be locked" in calls["close"][0][1]
    assert db_session.scalars(select(AgentRun).where(AgentRun.event == "stop_move_failed")).one()


def test_a_lock_inside_the_brokers_minimum_distance_takes_the_target(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    moves = _moves(holder)
    _read(monkeypatch, 1, 110.5)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)
    _quote(holder, 112.0, stops_level=0.5)                            # 112.0 - 111.8 = 0.2 < 0.5
    st.follow_targets(db_session, cfg, NOW)
    assert moves == [] and [c[0] for c in calls["close"]] == [pos.id]


def test_a_stop_already_past_the_lock_is_left_as_it_is(db_session, engine, monkeypatch):  # noqa: F811
    _, holder = engine
    moves = _moves(holder)
    _read(monkeypatch, 1, 110.5)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg, stop=111.9)                     # moved by hand, tighter than 111.8
    _quote(holder, 112.2)
    st.follow_targets(db_session, cfg, NOW)
    assert moves == []
    db_session.refresh(pos)
    assert pos.stop_loss == 111.9 and _fe(cfg)["mode"] == "following" and _fe(cfg)["lock"] == 111.9


def test_another_ticket_on_the_side_is_never_touched(db_session, engine, monkeypatch):  # noqa: F811
    # The broker closes and moves every ticket on a side: taking this target would take the user's trade too.
    calls, holder = engine
    moves = _moves(holder)
    reads = _read(monkeypatch, 1, 110.5)
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg)
    db_session.add(Position(symbol=SYM, asset_class="crypto", direction="long", qty=0.1, entry_price=111.0,
                            stop_loss=105.0, status=PositionStatus.OPEN.value, source="manual", opened_at=NOW))
    db_session.commit()
    _quote(holder, 112.2)
    st.follow_targets(db_session, cfg, NOW)
    st.follow_targets(db_session, cfg, NOW + timedelta(seconds=40))
    assert moves == [] and calls["close"] == [] and reads == []
    assert _fe(cfg)["mode"] == "shared" and "another trade shares its side" in cfg.state[SYM]["note"]
    assert len(db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_shared")).all()) == 1


# ----------------------------------------------------------------- following

def test_following_moves_the_stop_up_the_line_once_a_30s_close_and_only_up(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, follow_target=True)
    t1 = NOW + timedelta(seconds=2)                                  # past the 1 s grace after the 12:00:30 close
    mark = st.bar_mark("30s", t1)
    pos = _following(db_session, cfg, mode="following", stop=111.8, mark=mark - 1)
    _quote(holder, 113.5)
    reads = _read(monkeypatch, 1, 112.3)
    st.follow_targets(db_session, cfg, t1)
    assert moves == [(SYM, 112.3, None, "long")] and _fe(cfg)["mark"] == mark
    db_session.refresh(pos)
    assert pos.stop_loss == 112.3
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "stop_moved")).one().detail
    assert ev["follow"] is True and ev["tf"] == "30s" and ev["frm"] == 111.8
    st.follow_targets(db_session, cfg, t1 + timedelta(seconds=5))    # the same 30s bar: not read again
    assert len(reads) == 1
    _read(monkeypatch, 1, 112.0)                                     # the next close, a lower line: the stop stays
    st.follow_targets(db_session, cfg, t1 + timedelta(seconds=30))
    assert len(moves) == 1 and calls["close"] == [] and _fe(cfg)["mark"] == mark + 1


def test_following_never_puts_the_stop_at_the_price(db_session, engine, monkeypatch):  # noqa: F811
    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg, mode="following", stop=111.8, mark=st.bar_mark("30s", NOW) - 1)
    _quote(holder, 112.2)
    _read(monkeypatch, 1, 112.25)                                    # the line over the bid
    st.follow_targets(db_session, cfg, NOW)
    assert moves == []


def test_following_closes_the_trade_when_the_line_turns(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    _moves(holder)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg, mode="following", stop=112.3, mark=st.bar_mark("30s", NOW) - 1)
    _quote(holder, 112.6)
    _read(monkeypatch, -1, 113.4)
    st.follow_targets(db_session, cfg, NOW)
    assert [c[0] for c in calls["close"]] == [pos.id] and "turned against the trade" in calls["close"][0][1]
    assert _fe(cfg) is None


def test_a_failed_close_is_tried_again_ten_seconds_later(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg)
    _quote(holder, 112.1)
    _read(monkeypatch, -1, 113.0)
    tries = []
    monkeypatch.setattr("app.execution.monitor._close_position", lambda *a: tries.append(1) or False)
    st.follow_targets(db_session, cfg, NOW)
    st.follow_targets(db_session, cfg, NOW + timedelta(seconds=3))
    assert len(tries) == 1 and _fe(cfg)["wait_until"]
    st.follow_targets(db_session, cfg, NOW + timedelta(seconds=11))
    assert len(tries) == 2


def test_a_trade_gone_drops_its_entry(db_session, engine, monkeypatch):  # noqa: F811
    calls, holder = engine
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)
    pos.status = PositionStatus.CLOSED.value                         # its stop, or by hand
    db_session.commit()
    st.follow_targets(db_session, cfg, NOW)
    assert _fe(cfg) is None and calls["close"] == []


def test_every_second_even_for_a_stopped_strategy(db_session, engine, monkeypatch):  # noqa: F811
    # Its broker target is only a safety net far out: a strategy stopped on the board still keeps the real one.
    calls, holder = engine
    moves = _moves(holder)
    _read(monkeypatch, 1, 110.5)
    cfg = _strategy(db_session, follow_target=True, enabled=False)
    _following(db_session, cfg)
    _quote(holder, 112.05)
    st.strategy_tick(db_session, now=NOW)
    assert moves == [(SYM, 111.8, None, "long")] and _fe(cfg)["mode"] == "following"


def test_follow_levels_name_the_target_for_the_chart(db_session, engine):  # noqa: F811
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg)
    lv = st.follow_levels(db_session)
    assert lv[(SYM, "long")]["target"] == 112.0 and lv[(SYM, "long")]["mode"] == "waiting"


# ----------------------------------------------------------------- the board

def test_the_board_saves_it_off_by_default_never_without_a_target_and_records_the_change(db_session, engine):  # noqa: F811
    from app.api.strategy_routes import StrategyIn, create_strategy, update_strategy

    base = dict(name="f", pairs=[{"symbol": SYM, "asset_class": "crypto"}],
                entry_long=[[{"type": "trend", "tf": "1m", "params": {}}]], target_r=0.9)
    view = create_strategy(StrategyIn(**base), db_session)
    assert view.follow_target is False
    view = update_strategy(view.id, StrategyIn(**base, follow_target=True), db_session)
    assert view.follow_target is True
    edit = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).all()[-1].detail
    assert "follow the target: off → on" in edit["changes"]
    view = update_strategy(view.id, StrategyIn(**{**base, "target_r": None}, follow_target=True), db_session)
    assert view.follow_target is False                                # nothing to follow without a target


def test_a_lesson_never_switches_it(db_session, engine):  # noqa: F811
    from app.api.strategy_routes import _NOT_LEARNED, _config_of

    cfg = _strategy(db_session, follow_target=True)
    assert "follow_target" in _NOT_LEARNED and _config_of(cfg)["follow_target"] is True
