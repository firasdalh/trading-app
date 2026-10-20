"""An unreadable broker must never look like a flat account (go-live review 2026-10-13).

MT5 returns None when a terminal call fails — e.g. right after the PC wakes from sleep. That used to
be read as "no positions / nothing realized", which phantom-closed open trades, let the openers see
free room, disabled anti-stacking + the correlation guard, and un-latched the daily-loss breaker.
Every reader now raises and every gate fails CLOSED. Also covers the guarded manual SL/TP endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.brokers.base import BrokerError
from app.core.state import get_or_create_daily_state, get_or_create_risk_config, get_or_create_settings
from app.models.db import AgentRun, Position, TradeProposalRecord
from app.models.enums import AssetClass, Direction, OrderStatus, PositionStatus, ProposalStatus
from app.models.schemas import AccountState, OrderResult, PositionView, TradeProposal

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


class _DownBroker:
    """A broker whose open-book / history reads fail (terminal unreachable)."""
    is_paper = True
    name = "mt5"
    reconciles_positions = True

    def get_open_positions(self):
        raise BrokerError("MT5 positions_get failed: (-10004, 'No IPC connection')")

    def get_realized_pnl(self, since):
        raise BrokerError("MT5 deal history unavailable")


def test_live_broker_positions_raises_instead_of_returning_empty(db_session, monkeypatch):
    from app.risk import service
    monkeypatch.setattr(service, "get_broker_for", lambda ac, bm: _DownBroker())
    with pytest.raises(BrokerError):
        service.live_broker_positions(db_session)


def test_hybrid_does_not_scan_blind_when_book_unreadable(db_session, monkeypatch):
    """End to end through the real reader: Hybrid's 'could not read open positions' guard fires
    (it never could before — the reader swallowed the error and reported an empty, roomy book)."""
    import app.agents.hybrid as hybrid
    from app.models.db import WatchItem
    from app.risk import service

    db_session.add(WatchItem(symbol="EURUSDm", asset_class="forex", timeframe="1h", enabled=True))
    hybrid.get_or_create_hybrid_config(db_session).enabled = True
    db_session.commit()
    monkeypatch.setattr(service, "get_broker_for", lambda ac, bm: _DownBroker())
    monkeypatch.setattr(hybrid, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(hybrid, "preview_symbol",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned blind")))
    out = hybrid.run_hybrid(db_session)
    assert "could not read open positions" in (out.get("reason") or "")


def test_assess_refuses_when_book_unreadable(db_session, monkeypatch):
    from app.risk import service

    def _boom(session):
        raise BrokerError("MT5 positions_get failed")

    monkeypatch.setattr(service, "live_broker_positions", _boom)
    proposal = TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                             entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.8)
    d = service.assess(db_session, proposal)
    assert not d.approved and "unavailable" in d.reason


def test_executor_refuses_when_book_unreadable(db_session, monkeypatch):
    from app.execution.executor import ExecutionBlocked, execute_proposal
    from app.risk import service

    def _boom(session):
        raise BrokerError("MT5 positions_get failed")

    monkeypatch.setattr(service, "live_broker_positions", _boom)
    rec = TradeProposalRecord(
        symbol="AAPL", asset_class="stock", timeframe="1h", direction="long", entry=100.0,
        stop_loss=95.0, take_profit=110.0, confidence=0.7, rationale="t", reasoning={},
        status=ProposalStatus.PENDING_APPROVAL.value, risk_decision="approved", approved_qty=10.0,
        risk_amount=50.0)
    db_session.add(rec)
    db_session.commit()
    with pytest.raises(ExecutionBlocked, match="not opening blind"):
        execute_proposal(db_session, rec)


def test_daily_breaker_stays_latched_when_account_unreadable(db_session, monkeypatch):
    """2026-09-25 15:27: a failed read looked like realized 0 / floating 0 and RESUMED trading for
    40 s mid-drawdown. A tripped breaker must keep its state until the account can be read."""
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.trading_paused = True
    daily.pause_reason = "daily loss limit reached"
    db_session.commit()
    monkeypatch.setattr(service, "get_broker_for", lambda ac, bm: _DownBroker())
    assert service.evaluate_daily_pause(db_session) is True
    db_session.refresh(daily)
    assert daily.trading_paused is True


def test_daily_breaker_not_tripped_by_a_failed_read(db_session, monkeypatch):
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    db_session.commit()
    monkeypatch.setattr(service, "get_broker_for", lambda ac, bm: _DownBroker())
    assert service.evaluate_daily_pause(db_session) is False


# ---------------------------------------------------------------- manual SL/TP endpoint ----

class _SlBroker:
    """One open XAUUSDm position; $10 per price point per lot."""
    is_paper = True
    name = "mt5"

    def __init__(self, *, direction="long", stop=95.0, price=100.0, qty=1.0, entry=100.0):
        self.pos = PositionView(id=1, symbol="XAUUSDm", asset_class="metal", direction=direction, qty=qty,
                                entry_price=entry, stop_loss=stop, take_profit=110.0 if direction == "long" else 90.0,
                                status="open", last_price=price, unrealized_pnl=0.0)
        self.calls = []

    def get_open_positions(self):
        return [self.pos]

    def get_account(self):
        return AccountState(equity=10_000.0, cash=10_000.0, open_positions=1)

    def risk_per_lot(self, symbol, entry, stop):
        return abs(entry - stop) * 10.0

    def set_sl_tp(self, symbol, stop_loss=None, take_profit=None, direction=None):
        self.calls.append((stop_loss, take_profit))
        return OrderResult(status=OrderStatus.SUBMITTED)


def _sltp(db_session, monkeypatch, broker, *, sl=None, tp=None, risk=0.03):
    import app.brokers.registry as reg
    from app.api.settings_routes import SlTpRequest, set_position_sl_tp
    monkeypatch.setattr(reg, "get_broker_for", lambda ac, bm: broker)
    get_or_create_risk_config(db_session).risk_per_trade = risk     # $300 on the $10k account at 3%
    db_session.commit()
    return set_position_sl_tp(SlTpRequest(symbol="XAUUSDm", asset_class=AssetClass.METAL,
                                          stop_loss=sl, take_profit=tp), db_session)


def _status(fn):
    with pytest.raises(HTTPException) as e:
        fn()
    return e.value.status_code, e.value.detail


def test_sltp_tighten_is_allowed_and_mirrored_to_our_row(db_session, monkeypatch):
    row = Position(symbol="XAUUSDm", asset_class="metal", direction="long", qty=1.0, entry_price=100.0,
                   stop_loss=95.0, take_profit=110.0, status=PositionStatus.OPEN.value, risk_amount=50.0)
    db_session.add(row)
    db_session.commit()
    broker = _SlBroker()
    _sltp(db_session, monkeypatch, broker, sl=97.0)
    assert broker.calls == [(97.0, None)]
    db_session.refresh(row)
    assert row.stop_loss == 97.0 and row.risk_amount == 30.0      # exposure now sees the real risk
    audit = db_session.scalars(select(AgentRun).where(AgentRun.event == "sl_tp_modified")).all()
    assert len(audit) == 1 and audit[0].detail["risk_after"] == 30.0


def test_sltp_widening_within_the_limit_is_allowed(db_session, monkeypatch):
    broker = _SlBroker()
    _sltp(db_session, monkeypatch, broker, sl=80.0)                # $200 <= $300 limit
    assert broker.calls == [(80.0, None)]


def test_sltp_widening_past_the_limit_is_allowed_with_a_warning(db_session, monkeypatch):
    # Manual control is the user's (2026-10-14): a wider stop goes through, with the new risk shown.
    row = Position(symbol="XAUUSDm", asset_class="metal", direction="long", qty=1.0, entry_price=100.0,
                   stop_loss=95.0, take_profit=110.0, status=PositionStatus.OPEN.value, risk_amount=50.0)
    db_session.add(row)
    db_session.commit()
    broker = _SlBroker()
    res = _sltp(db_session, monkeypatch, broker, sl=60.0)            # $400 > the $300 per-trade risk
    assert broker.calls == [(60.0, None)]
    assert res["warning"] and "4.0%" in res["warning"]
    db_session.refresh(row)
    assert row.stop_loss == 60.0 and row.risk_amount == 400.0       # exposure budget sees the real risk


def test_sltp_within_the_limit_has_no_warning(db_session, monkeypatch):
    res = _sltp(db_session, monkeypatch, _SlBroker(), sl=80.0)      # $200 <= $300
    assert res["warning"] is None


def test_sltp_removing_a_stop_is_refused(db_session, monkeypatch):
    broker = _SlBroker()
    code, detail = _status(lambda: _sltp(db_session, monkeypatch, broker, sl=0.0))
    assert code == 400 and "removing a stop" in detail and broker.calls == []


def test_sltp_stop_on_the_wrong_side_is_refused(db_session, monkeypatch):
    broker = _SlBroker(price=100.0)
    code, _ = _status(lambda: _sltp(db_session, monkeypatch, broker, sl=101.0))   # long stop above price
    assert code == 400 and broker.calls == []
    short = _SlBroker(direction="short", stop=105.0, price=100.0)
    code, _ = _status(lambda: _sltp(db_session, monkeypatch, short, tp=101.0))   # short target above price
    assert code == 400 and short.calls == []


def test_sltp_protecting_an_unprotected_trade_is_always_allowed(db_session, monkeypatch):
    broker = _SlBroker(stop=None)
    _sltp(db_session, monkeypatch, broker, sl=50.0)                # $500 risk, but it was unlimited
    assert broker.calls == [(50.0, None)]


def test_sltp_manual_moves_work_under_kill_switch(db_session, monkeypatch):
    # The kill-switch halts NEW orders; managing an open trade's stop by hand stays the user's call.
    get_or_create_settings(db_session).kill_switch_engaged = True
    db_session.commit()
    broker = _SlBroker()
    _sltp(db_session, monkeypatch, broker, sl=90.0)
    _sltp(db_session, monkeypatch, broker, sl=98.0)
    assert broker.calls == [(90.0, None), (98.0, None)]
