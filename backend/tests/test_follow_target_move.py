"""The strategy's 🎯 target moved from the chart (2026-11-22): the lock moves with it, the broker's safety take
profit goes 2R past it, the trade row mirrors it - and only while the trade waits for its target."""
from __future__ import annotations

import pytest
from sqlalchemy import select

import app.agents.strategy as st
from app.agents.manual_follow import FollowRefused
from app.models.db import AgentRun
from tests.test_follow_target import SYM, _fe, _following, _moves, _quote
from tests.test_strategy import _strategy, engine  # noqa: F401 - engine: the fixture


def test_moving_it_moves_the_lock_and_the_safety_target(db_session, engine):  # noqa: F811
    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, follow_target=True)
    pos = _following(db_session, cfg)                  # entry 110, 1R = 2, target 112
    _quote(holder, 111.0)
    out = st.move_follow_target(db_session, SYM, "long", 113.0)
    assert out["target"] == 113.0 and out["broker_tp"] == 117.0
    assert moves == [(SYM, None, 117.0, "long")]       # only the take profit; the stop is left
    db_session.refresh(cfg)
    db_session.refresh(pos)
    fe = _fe(cfg)
    assert fe["target"] == 113.0 and fe["lock"] == pytest.approx(112.8) and fe["mode"] == "waiting"
    assert pos.take_profit == 117.0
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_target_moved")).one().detail
    assert ev["frm"] == 112.0 and ev["to"] == 113.0 and ev["by"] == "chart"
    assert "113" in cfg.state[SYM]["note"]
    # and the tick reads the new target: short of it, nothing happens
    _quote(holder, 112.5)
    st.follow_targets(db_session, cfg)
    assert _fe(cfg)["mode"] == "waiting"


@pytest.mark.parametrize("target,bid,why", [
    (110.9, 111.0, "above the current price"),        # behind the price: it would fire at once
    (109.0, 108.5, "profit side of the entry"),
])
def test_a_bad_level_is_refused_and_nothing_moves(db_session, engine, target, bid, why):  # noqa: F811
    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg)
    _quote(holder, bid)
    with pytest.raises(FollowRefused, match=why):
        st.move_follow_target(db_session, SYM, "long", target)
    assert moves == [] and _fe(cfg)["target"] == 112.0


def test_past_its_target_it_follows_the_line_and_cant_be_moved(db_session, engine):  # noqa: F811
    _, holder = engine
    _moves(holder)
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg, mode="following")
    _quote(holder, 112.5)
    with pytest.raises(FollowRefused, match="past its target"):
        st.move_follow_target(db_session, SYM, "long", 114.0)


def test_a_refused_safety_target_changes_nothing(db_session, engine):  # noqa: F811
    _, holder = engine
    _moves(holder, status="rejected")
    cfg = _strategy(db_session, follow_target=True)
    _following(db_session, cfg)
    _quote(holder, 111.0)
    with pytest.raises(FollowRefused, match="refused the safety"):
        st.move_follow_target(db_session, SYM, "long", 113.0)
    assert _fe(cfg)["target"] == 112.0


def test_no_strategy_keeps_it(db_session, engine):  # noqa: F811
    with pytest.raises(FollowRefused, match="no strategy keeps"):
        st.move_follow_target(db_session, SYM, "long", 113.0)


def test_the_chart_endpoint_moves_a_strategy_target_when_no_manual_one_is_on(db_session, engine, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.main import app

    seen = []
    monkeypatch.setattr(st, "move_follow_target", lambda session, symbol, side, target: seen.append((symbol, side, target))
                        or {"target": target})
    r = TestClient(app).post("/api/positions/follow/target", headers={"X-Desk": "1"},
                             json={"symbol": SYM, "asset_class": "crypto", "direction": "long", "target": 113.0})
    assert r.status_code == 200 and seen == [(SYM, "long", 113.0)]


def test_the_tick_and_a_move_take_turns(db_session, engine):  # noqa: F811
    assert st.STATE_LOCK.acquire(blocking=False)
    try:
        import threading

        done = []
        t = threading.Thread(target=lambda: done.append(st.STATE_LOCK.acquire(timeout=0.2)))
        t.start()
        t.join()
        assert done == [False]          # held by one (the tick) = the other (a move) waits
    finally:
        st.STATE_LOCK.release()
