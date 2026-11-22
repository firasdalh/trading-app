"""The manual trades' follow levels (SL1 / TP1 / TP2 / SL2) and auto (2026-11-22): every NEW manual trade is followed on
the user's levels a moment after it opens; one switched off stays manual; a strategy's / automation's is not touched."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.agents.manual_follow as mf
from app.models.db import AgentRun, Position
from app.models.enums import PositionStatus
from app.models.schemas import PositionView
from tests.test_manual_follow import NOW, SYM, broker  # noqa: F401 - broker: the fixture


def _p(side="long", entry=1.1000, stop=1.0980, tp=1.1018, opened=None, symbol=SYM):
    return PositionView(id=1, symbol=symbol, asset_class="forex", direction=side, qty=0.1, entry_price=entry,
                        stop_loss=stop, take_profit=tp, status="open", last_price=entry, unrealized_pnl=0.0,
                        opened_at=opened)


@pytest.fixture(autouse=True)
def _fresh():
    mf._AUTO_TRIED.clear()


def test_the_defaults_and_a_bad_value_is_refused(db_session):
    assert mf.settings(db_session) == mf.DEFAULTS
    with pytest.raises(mf.FollowRefused, match="tp2_r must be between"):
        mf.save_settings(db_session, {"tp2_r": 50})
    with pytest.raises(mf.FollowRefused, match="sl1_mode must be"):
        mf.save_settings(db_session, {"sl1_mode": "nope"})
    out = mf.save_settings(db_session, {"tp1_mode": "r", "tp1_r": 1.5, "tp2_r": 3, "sl2_r": 0.2, "junk": 1})
    assert out["tp1_mode"] == "r" and out["tp1_r"] == 1.5 and out["tp2_r"] == 3.0 and out["sl2_r"] == 0.2
    assert "junk" not in out
    assert db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_settings")).first() is not None


def test_the_levels_come_from_the_settings(db_session, broker):
    b = broker([_p()], bid=1.1005)
    mf.save_settings(db_session, {"tp1_mode": "r", "tp1_r": 1.5, "tp2_r": 3.0, "sl2_r": 0.25})
    fe = mf.start(db_session, SYM, "forex", "long")
    # R = 0.0020 (entry 1.1000, stop 1.0980): TP1 1.5R, TP2 3R past it, SL2 0.25R short of TP1
    assert fe["target"] == pytest.approx(1.1030) and fe["broker_tp"] == pytest.approx(1.1090)
    assert fe["lock"] == pytest.approx(1.1025) and fe["safety_r"] == 3.0 and fe["lock_r"] == 0.25
    assert b.moves == [(SYM, None, pytest.approx(1.1090), "long")]
    # moving TP1 keeps the trade's own multiples
    fe = mf.move(db_session, SYM, "long", 1.1040)
    assert fe["broker_tp"] == pytest.approx(1.1100) and fe["lock"] == pytest.approx(1.1035)


def test_keep_uses_the_trades_own_tp_and_a_trade_without_one_gets_tp1_in_r(db_session, broker):
    broker([_p(tp=None)], bid=1.1005)
    fe = mf.start(db_session, SYM, "forex", "long")
    assert fe["target"] == pytest.approx(1.1020)              # no TP of its own: 1.0R


def test_sl1_at_atr_places_the_stop(db_session, broker, monkeypatch):
    b = broker([_p(stop=None, tp=None)], bid=1.1005)
    monkeypatch.setattr(mf, "_atr1m", lambda broker, symbol: 0.0010)
    mf.save_settings(db_session, {"sl1_atr": 2.0})
    fe = mf.start(db_session, SYM, "forex", "long")           # no stop of its own: 2 x ATR = 0.0020 under the entry
    assert b.moves[-1][1] == pytest.approx(1.0980) and fe["risk"] == pytest.approx(0.0020)
    assert fe["target"] == pytest.approx(1.1020)
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "follow_on")).one().detail
    assert ev["stop"] == pytest.approx(1.0980) and ev["was_sl"] is None


def test_sl1_atr_mode_replaces_the_stop_even_when_it_has_one(db_session, broker, monkeypatch):
    b = broker([_p(stop=1.0990)], bid=1.1005)
    monkeypatch.setattr(mf, "_atr1m", lambda broker, symbol: 0.0010)
    mf.save_settings(db_session, {"sl1_mode": "atr", "sl1_atr": 1.5})
    fe = mf.start(db_session, SYM, "forex", "long")
    assert b.moves[-1][1] == pytest.approx(1.0985) and fe["risk"] == pytest.approx(0.0015)


def test_start_twice_leaves_it_as_it_is(db_session, broker):
    b = broker([_p()], bid=1.1005)
    one = mf.start(db_session, SYM, "forex", "long")
    two = mf.start(db_session, SYM, "forex", "long", target=1.1050)
    assert two == one and len(b.moves) == 1


# ----------------------------------------------------------------- auto

def test_auto_follows_a_new_manual_trade_after_the_grace(db_session, broker):
    b = broker([], bid=1.1005)
    mf.save_settings(db_session, {"auto": True})
    opened = NOW - timedelta(seconds=5)
    b.positions = [_p(opened=opened)]
    assert mf.tick(db_session, NOW) == []                    # 5 s old: time to set its levels by hand
    out = mf.tick(db_session, NOW + timedelta(seconds=12))
    assert out == [{"symbol": SYM, "auto": "long"}]
    fe = next(iter(mf._entries(db_session).values()))
    assert fe["auto"] is True and fe["target"] == 1.1018 and fe["fp"].endswith(opened.isoformat())


def test_auto_leaves_the_trades_open_when_it_was_switched_on(db_session, broker):
    b = broker([_p(opened=NOW - timedelta(minutes=10))], bid=1.1005)
    mf.save_settings(db_session, {"auto": True})
    assert mf.tick(db_session, NOW) == [] and mf._entries(db_session) == {} and b.moves == []


def test_a_trade_switched_off_stays_manual(db_session, broker):
    b = broker([], bid=1.1005)
    mf.save_settings(db_session, {"auto": True})
    b.positions = [_p(opened=NOW - timedelta(seconds=60))]
    mf.tick(db_session, NOW)
    assert mf._entries(db_session)
    mf.stop(db_session, SYM, "long")
    assert mf.tick(db_session, NOW + timedelta(seconds=5)) == [] and mf._entries(db_session) == {}


def test_auto_never_takes_a_strategy_or_automation_trade(db_session, broker):
    b = broker([], bid=1.1005)
    mf.save_settings(db_session, {"auto": True})
    b.positions = [_p(opened=NOW - timedelta(seconds=60))]
    db_session.add(Position(symbol=SYM, asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                            stop_loss=1.098, take_profit=1.1018, status=PositionStatus.OPEN.value, source="hybrid"))
    db_session.commit()
    assert mf.tick(db_session, NOW) == [] and mf._entries(db_session) == {} and b.moves == []


def test_auto_takes_the_tickets_own_manual_row(db_session, broker):
    b = broker([], bid=1.1005)
    mf.save_settings(db_session, {"auto": True})
    b.positions = [_p(opened=NOW - timedelta(seconds=60))]
    db_session.add(Position(symbol=SYM, asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                            stop_loss=1.098, take_profit=1.1018, status=PositionStatus.OPEN.value, source="manual"))
    db_session.commit()
    assert mf.tick(db_session, NOW) == [{"symbol": SYM, "auto": "long"}]


def test_a_refused_auto_trade_is_tried_once_and_logged(db_session, broker):
    b = broker([], bid=1.1005)
    mf.save_settings(db_session, {"auto": True})
    b.positions = [_p(stop=None, opened=NOW - timedelta(seconds=60))]     # no stop, and no ATR to place one
    assert mf.tick(db_session, NOW) == []
    assert mf.tick(db_session, NOW + timedelta(seconds=1)) == []
    refused = db_session.scalars(select(AgentRun).where(AgentRun.event == "auto_follow_refused")).all()
    assert len(refused) == 1 and "needs a stop" in refused[0].detail["reason"]


def test_auto_off_does_nothing(db_session, broker):
    b = broker([_p(opened=NOW - timedelta(seconds=60))], bid=1.1005)
    assert mf.tick(db_session, NOW) == [] and b.moves == []


def test_the_settings_endpoints(db_session, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.get("/api/positions/follow/settings").json() == mf.DEFAULTS
    r = c.post("/api/positions/follow/settings", headers={"X-Desk": "1"}, json={"tp1_mode": "r", "tp1_r": 2})
    assert r.status_code == 200 and r.json()["tp1_r"] == 2.0
    r = c.post("/api/positions/follow/settings", headers={"X-Desk": "1"}, json={"sl2_r": 5})
    assert r.status_code == 409 and "sl2_r" in r.json()["detail"]
