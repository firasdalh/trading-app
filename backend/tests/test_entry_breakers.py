"""Entry circuit breakers: trade-count, consecutive-loss, and performance/divergence pauses.

These are deterministic, history-derived "pause NEW entries" gates computed by
``service.entry_breaker_reason`` and enforced by the Risk Manager veto. All OFF by default (0 /
disabled), so they never change behaviour until explicitly enabled.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.state import get_or_create_risk_config
from app.models.db import Position
from app.models.enums import AssetClass, Direction, PositionStatus, RiskDecisionType
from app.models.schemas import AccountState, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal
from app.risk.service import entry_breaker_reason

NOW = datetime.now(timezone.utc)


def _rc(session, **kw):
    rc = get_or_create_risk_config(session)
    for k, v in kw.items():
        setattr(rc, k, v)
    session.commit()
    return rc


def _closed(session, *, pnl, risk_amount=100.0, minutes_ago=5, symbol="EURUSDm", direction="long"):
    p = Position(symbol=symbol, asset_class="forex", direction=direction, qty=0.1,
                 entry_price=1.10, stop_loss=1.09, take_profit=1.12,
                 status=PositionStatus.CLOSED.value, last_price=1.11,
                 realized_pnl=pnl, risk_amount=risk_amount,
                 closed_at=NOW - timedelta(minutes=minutes_ago))
    session.add(p)
    session.commit()
    return p


def _open(session, *, minutes_ago=1, symbol="EURUSDm"):
    p = Position(symbol=symbol, asset_class="forex", direction="long", qty=0.1,
                 entry_price=1.10, stop_loss=1.09, status=PositionStatus.OPEN.value,
                 opened_at=NOW - timedelta(minutes=minutes_ago))
    session.add(p)
    session.commit()
    return p


# --- disabled by default -----------------------------------------------------

def test_no_breaker_when_all_off(db_session):
    _closed(db_session, pnl=-50.0)
    _closed(db_session, pnl=-50.0)
    _closed(db_session, pnl=-50.0)
    assert entry_breaker_reason(db_session) is None


# --- trade-count cap ---------------------------------------------------------

def test_trade_count_trips_at_cap(db_session):
    _rc(db_session, max_trades_per_day=2)
    _open(db_session, symbol="EURUSDm")
    _open(db_session, symbol="GBPUSDm")
    reason = entry_breaker_reason(db_session)
    assert reason is not None and "daily trade cap" in reason


def test_trade_count_ok_below_cap(db_session):
    _rc(db_session, max_trades_per_day=3)
    _open(db_session)
    assert entry_breaker_reason(db_session) is None


# --- consecutive-loss breaker ------------------------------------------------

def test_consecutive_losses_trip_within_cooldown(db_session):
    _rc(db_session, max_consecutive_losses=3, breaker_cooldown_minutes=120)
    for i in range(3):
        _closed(db_session, pnl=-50.0, minutes_ago=5 + i)
    reason = entry_breaker_reason(db_session)
    assert reason is not None and "losses in a row" in reason


def test_consecutive_losses_reset_by_a_win(db_session):
    _rc(db_session, max_consecutive_losses=2, breaker_cooldown_minutes=120)
    _closed(db_session, pnl=-50.0, minutes_ago=30)
    _closed(db_session, pnl=-50.0, minutes_ago=20)
    _closed(db_session, pnl=40.0, minutes_ago=5)  # most recent WON -> streak 0
    assert entry_breaker_reason(db_session) is None


def test_consecutive_losses_probe_after_cooldown(db_session):
    _rc(db_session, max_consecutive_losses=2, breaker_cooldown_minutes=60)
    _closed(db_session, pnl=-50.0, minutes_ago=200)
    _closed(db_session, pnl=-50.0, minutes_ago=90)  # last close 90 min > 60 cooldown -> probe allowed
    assert entry_breaker_reason(db_session) is None


# --- performance / divergence breaker ----------------------------------------

def test_perf_breaker_trips_below_floor(db_session):
    _rc(db_session, perf_breaker_enabled=True, expectancy_window=4, min_expectancy_r=-0.2,
        breaker_cooldown_minutes=120)
    for i in range(4):
        _closed(db_session, pnl=-50.0, risk_amount=100.0, minutes_ago=5 + i)  # -0.5R each
    reason = entry_breaker_reason(db_session)
    assert reason is not None and "performance breaker" in reason


def test_perf_breaker_ok_above_floor(db_session):
    _rc(db_session, perf_breaker_enabled=True, expectancy_window=4, min_expectancy_r=-0.2)
    for i in range(4):
        _closed(db_session, pnl=30.0, risk_amount=100.0, minutes_ago=5 + i)  # +0.3R each
    assert entry_breaker_reason(db_session) is None


def test_perf_breaker_needs_a_full_window(db_session):
    _rc(db_session, perf_breaker_enabled=True, expectancy_window=5, min_expectancy_r=-0.2)
    for i in range(3):  # only 3 < 5 -> not enough history to judge
        _closed(db_session, pnl=-50.0, risk_amount=100.0, minutes_ago=5 + i)
    assert entry_breaker_reason(db_session) is None


# --- Risk Manager veto wiring ------------------------------------------------

def test_manager_vetoes_on_breaker_reason():
    acct = AccountState(equity=100_000.0, cash=100_000.0, open_positions=0,
                        total_risk_amount=0.0, daily_realized_pnl=0.0, trading_paused=False)
    limits = RiskLimits(risk_per_trade=0.01, max_open_positions=3, max_daily_loss=0.03,
                        max_total_exposure=0.06, per_pair_cooldown_minutes=30,
                        risk_per_trade_ceiling=0.02)
    prop = TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                         entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.7)
    d = evaluate_proposal(prop, acct, limits, now=NOW, qty_step=1,
                          breaker_reason="circuit breaker: 3 losses in a row")
    assert d.approved is False
    assert d.decision == RiskDecisionType.VETOED
    assert "circuit breaker" in d.reason
    assert d.checks.get("breaker_ok") is False


def test_manager_approves_when_no_breaker():
    acct = AccountState(equity=100_000.0, cash=100_000.0, open_positions=0,
                        total_risk_amount=0.0, daily_realized_pnl=0.0, trading_paused=False)
    limits = RiskLimits(risk_per_trade=0.01, max_open_positions=3, max_daily_loss=0.03,
                        max_total_exposure=0.06, per_pair_cooldown_minutes=30,
                        risk_per_trade_ceiling=0.02)
    prop = TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                         entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.7)
    d = evaluate_proposal(prop, acct, limits, now=NOW, qty_step=1, breaker_reason=None)
    assert d.approved is True
    assert d.checks.get("breaker_ok") is True
