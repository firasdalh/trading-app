"""The ACCOUNT's daily profit target (2026-11-16): "a daily target to win - $500, $600, $700, $1,000 - when I reach
it, we stop trading for that day"."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.risk.service as svc
from app.core.state import get_or_create_daily_state, get_or_create_risk_config
from app.models.db import AgentRun
from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import AccountState, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal


def _target(session, amount):
    rc = get_or_create_risk_config(session)
    rc.daily_profit_target = amount
    session.commit()


@pytest.fixture
def closed_today(monkeypatch):
    """The account's closed P&L today as the broker's history would give it."""
    box = {"pnl": 0.0}
    monkeypatch.setattr(svc, "realized_today", lambda session, strict=False: box["pnl"])
    monkeypatch.setattr("app.api.routes.realized_today", lambda session, strict=False: box["pnl"])
    return box


def test_no_target_never_stops_anything(db_session, closed_today):
    closed_today["pnl"] = 10_000.0
    assert svc.daily_target_state(db_session) is None
    assert svc.daily_target_reason(db_session) is None


def test_reaching_the_target_stops_new_trades_for_the_day(db_session, closed_today):
    _target(db_session, 500.0)
    closed_today["pnl"] = 320.0
    st = svc.daily_target_state(db_session)
    assert st["hit"] is False and st["realized"] == 320.0 and st["target"] == 500.0 and st["hit_at"] is None
    assert svc.daily_target_reason(db_session) is None
    closed_today["pnl"] = 512.4
    reason = svc.daily_target_reason(db_session)
    assert reason.startswith("daily profit target reached: +$512.40 closed today, target $500.00")
    rows = db_session.scalars(select(AgentRun).where(AgentRun.event == "daily_target_reached")).all()
    assert len(rows) == 1 and rows[0].detail == {"target": 500.0, "realized": 512.4}
    svc.daily_target_reason(db_session)
    assert len(db_session.scalars(select(AgentRun).where(AgentRun.event == "daily_target_reached")).all()) == 1


def test_reached_stays_reached_even_when_an_open_trade_then_loses(db_session, closed_today):
    _target(db_session, 500.0)
    closed_today["pnl"] = 500.0                      # exactly the target counts
    assert svc.daily_target_state(db_session)["hit"] is True
    closed_today["pnl"] = 380.0                      # a trade still open then closed at a loss
    st = svc.daily_target_state(db_session)
    assert st["hit"] is True and st["hit_pnl"] == 500.0 and st["realized"] == 380.0


def test_a_raised_target_trades_again_and_a_lower_one_stays_reached(db_session, closed_today):
    _target(db_session, 500.0)
    closed_today["pnl"] = 520.0
    assert svc.daily_target_state(db_session)["hit"] is True
    _target(db_session, 700.0)                       # the user's choice: keep trading toward $700
    assert svc.daily_target_state(db_session)["hit"] is False
    closed_today["pnl"] = 705.0
    assert svc.daily_target_state(db_session)["hit"] is True
    _target(db_session, 600.0)                       # lowered under the one reached: still reached
    assert svc.daily_target_state(db_session)["hit"] is True


def test_a_failed_broker_read_keeps_what_was_latched(db_session, monkeypatch):
    _target(db_session, 500.0)
    monkeypatch.setattr(svc, "realized_today", lambda session, strict=False: (_ for _ in ()).throw(RuntimeError("MT5")))
    assert svc.daily_target_state(db_session)["hit"] is False
    d = get_or_create_daily_state(db_session)
    d.target_hit_at, d.target_hit_pnl, d.target_hit_target = svc.datetime.now(svc.timezone.utc), 510.0, 500.0
    db_session.commit()
    assert svc.daily_target_state(db_session)["hit"] is True


def test_a_new_day_starts_with_no_target_reached(db_session, closed_today, monkeypatch):
    _target(db_session, 500.0)
    closed_today["pnl"] = 600.0
    assert svc.daily_target_state(db_session)["hit"] is True
    monkeypatch.setattr("app.core.state.today_str", lambda: "2099-01-02")
    closed_today["pnl"] = 0.0
    assert svc.daily_target_state(db_session)["hit"] is False


def test_the_risk_manager_refuses_every_new_entry_with_its_reason(db_session, closed_today):
    _target(db_session, 500.0)
    closed_today["pnl"] = 650.0
    reason = svc.daily_target_reason(db_session)
    prop = TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                         entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.7)
    acct = AccountState(equity=100_000.0, cash=100_000.0, open_positions=0,
                        total_risk_amount=0.0, daily_realized_pnl=0.0, trading_paused=False)
    limits = RiskLimits(risk_per_trade=0.01, max_open_positions=3, max_daily_loss=0.03,
                        max_total_exposure=0.06, per_pair_cooldown_minutes=30, risk_per_trade_ceiling=0.02)
    dec = evaluate_proposal(prop, acct, limits, now=svc.datetime.now(svc.timezone.utc), qty_step=1,
                            breaker_reason=reason)
    assert dec.approved is False and dec.decision == RiskDecisionType.VETOED
    assert "daily profit target reached" in dec.reason


def test_assess_asks_for_the_target_after_the_other_breakers(db_session, monkeypatch):
    seen = {}

    def fake_eval(proposal, account, limits, **kw):
        seen["breaker"] = kw["breaker_reason"]
        raise StopIteration

    monkeypatch.setattr(svc, "entry_breaker_reason", lambda s: None)
    monkeypatch.setattr(svc, "daily_target_reason", lambda s: "daily profit target reached: test")
    monkeypatch.setattr(svc, "evaluate_proposal", fake_eval)
    prop = TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, timeframe="1m", direction=Direction.LONG,
                         entry=1.10, stop_loss=1.09, take_profit=1.12, confidence=0.7)
    with pytest.raises(StopIteration):
        svc.assess(db_session, prop)
    assert seen["breaker"] == "daily profit target reached: test"


def test_the_board_sets_and_clears_it(db_session):
    from app.api.settings_routes import RiskUpdateRequest, update_risk

    res = update_risk(RiskUpdateRequest(daily_profit_target=750), db_session)
    assert res.risk.daily_profit_target == 750.0
    rows = db_session.scalars(select(AgentRun).where(AgentRun.event == "risk_changed")).all()
    assert rows[-1].detail["daily_profit_target"] == [None, "750.0"]
    assert update_risk(RiskUpdateRequest(daily_profit_target=0), db_session).risk.daily_profit_target is None
    with pytest.raises(HTTPException):
        update_risk(RiskUpdateRequest(daily_profit_target=-5), db_session)


def test_the_header_reads_it_with_the_days_closed_pnl(db_session, closed_today):
    from app.api.routes import read_risk_state

    _target(db_session, 500.0)
    closed_today["pnl"] = 120.0
    view = read_risk_state(db_session)
    assert view.daily_target["target"] == 500.0 and view.daily_target["hit"] is False
    closed_today["pnl"] = 530.0
    view = read_risk_state(db_session)
    assert view.daily_target["hit"] is True and view.daily_target["hit_pnl"] == 530.0
    assert isinstance(view.daily_target["hit_at"], str)


def test_a_strategy_opens_nothing_once_the_account_reached_it(db_session, monkeypatch):
    import app.agents.strategy as st
    from tests.test_strategy import NOW, _Broker, RISING, _strategy

    from app.data import ohlcv_cache

    ohlcv_cache._cache.clear()
    calls = []
    monkeypatch.setattr("app.brokers.registry.get_broker_for", lambda ac, bm: _Broker(RISING))
    monkeypatch.setattr("app.risk.service.assess", lambda *a, **k: calls.append(a))
    st._BAR_CACHE.clear()
    st._LAST_LOOK.clear()
    _target(db_session, 500.0)
    d = get_or_create_daily_state(db_session)
    d.target_hit_at, d.target_hit_pnl, d.target_hit_target = NOW - timedelta(minutes=5), 512.0, 500.0
    db_session.commit()
    cfg = _strategy(db_session)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls == []
    assert out[0]["note"] == ("account daily target reached - +$512.00 closed today, target $500.00: "
                              "no new trades until 00:00 UTC")
    _target(db_session, 800.0)                       # raised past it: the strategy goes for its entry again
    st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))
    assert len(calls) == 1
