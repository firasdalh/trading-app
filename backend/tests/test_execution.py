"""Tests for the Execution & Monitor agents and Modes B/C gating (the dangerous parts)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.state import get_or_create_daily_state, get_or_create_settings, live_execution_allowed
from app.execution.executor import ExecutionBlocked, execute_proposal
from app.execution.kill_switch import flatten_all
from app.execution.monitor import monitor_positions
from app.models.db import AppSettings, Order, Position, TradeProposalRecord
from app.models.enums import AssetClass, Direction, OrderStatus, PositionStatus, ProposalStatus
from sqlalchemy import select

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_close_pnl_scales_by_contract_size(db_session):
    """MT5 stores qty in lots; booked P&L must scale by the contract size (gold 0.01 lot,
    $24.66/oz move = -$24.66, not -$0.25)."""
    from app.execution.monitor import _close_position
    from app.models.schemas import OrderResult

    class _Broker:
        is_paper = True

        def contract_size(self, symbol):
            return 100.0

        def close_position(self, symbol):  # already closed by the broker's own SL
            return OrderResult(status=OrderStatus.REJECTED, error="no position")

    pos = Position(symbol="XAUUSDm", asset_class="metal", direction="short", qty=0.01,
                   entry_price=4449.192, status=PositionStatus.OPEN.value, last_price=4473.848)
    db_session.add(pos)
    db_session.commit()
    _close_position(db_session, pos, _Broker(), 4473.848, "stop")
    db_session.commit()
    assert pos.realized_pnl == round(-1 * 0.01 * 100 * (4473.848 - 4449.192), 2)  # -24.66


def _make_record(session, *, symbol="AAPL", direction=Direction.LONG, entry=100.0,
                 stop=95.0, tp=110.0, qty=10.0) -> TradeProposalRecord:
    rec = TradeProposalRecord(
        symbol=symbol, asset_class="stock", timeframe="1h", direction=direction.value,
        entry=entry, stop_loss=stop, take_profit=tp, confidence=0.7, rationale="test",
        reasoning={}, status=ProposalStatus.PENDING_APPROVAL.value,
        risk_decision="approved", approved_qty=qty, risk_amount=qty * abs(entry - stop),
    )
    session.add(rec)
    session.commit()
    return rec


# ---- executor ----

def test_execute_fills_logs_and_opens_position(db_session):
    rec = _make_record(db_session)
    result = execute_proposal(db_session, rec)
    assert result.status == OrderStatus.FILLED

    orders = db_session.scalars(select(Order)).all()
    assert len(orders) == 1
    order = orders[0]
    # Logged both before (submit_payload) and after (broker_response) submission.
    assert order.submit_payload is not None
    assert order.broker_response is not None
    assert order.status == OrderStatus.FILLED.value

    positions = db_session.scalars(select(Position)).all()
    assert len(positions) == 1 and positions[0].status == PositionStatus.OPEN.value
    db_session.refresh(rec)
    assert rec.status == ProposalStatus.EXECUTED.value


def test_kill_switch_blocks_execution(db_session):
    settings = get_or_create_settings(db_session)
    settings.kill_switch_engaged = True
    db_session.commit()
    rec = _make_record(db_session)
    with pytest.raises(ExecutionBlocked):
        execute_proposal(db_session, rec)
    # Nothing should have been submitted.
    assert db_session.scalars(select(Order)).all() == []


def test_execute_requires_approved_qty(db_session):
    rec = _make_record(db_session, qty=10.0)
    rec.approved_qty = 0.0
    db_session.commit()
    with pytest.raises(ExecutionBlocked):
        execute_proposal(db_session, rec)


# ---- stale-plan / price-drift guard ----

class _DriftBroker:
    """Fills at a fixed current price so we can drive the drift guard deterministically."""
    is_paper = True
    reconciles_positions = False

    def __init__(self, current: float):
        self.current = current
        self.name = "drift"

    def get_quote(self, symbol):
        from app.models.schemas import Quote
        return Quote(symbol=symbol, price=self.current, ts=NOW)

    def submit_order(self, request):
        from app.models.schemas import OrderResult
        return OrderResult(broker_order_id="drift-1", status=OrderStatus.FILLED,
                           filled_qty=request.qty, avg_fill_price=self.current, raw={})


def _exec_with_current(db_session, monkeypatch, rec, current):
    from app.execution import executor as ex
    monkeypatch.setattr(ex, "get_broker_for", lambda ac, bm: _DriftBroker(current))
    return execute_proposal(db_session, rec)


def test_drift_guard_refuses_when_rr_breaks(db_session, monkeypatch):
    """The USDJPYm case: a triggered arm approved late fills far above the plan, collapsing the
    reward:risk (planned ~4R -> ~0.5R). The guard must refuse rather than open a broken trade."""
    rec = _make_record(db_session, symbol="USDJPYm", entry=162.423, stop=162.338, tp=162.762, qty=1.43)
    with pytest.raises(ExecutionBlocked, match="reward:risk"):
        _exec_with_current(db_session, monkeypatch, rec, current=162.621)
    assert db_session.scalars(select(Position)).all() == []  # nothing opened


def test_drift_guard_refuses_when_over_risked(db_session, monkeypatch):
    """R:R can still clear the floor while the actual stop distance balloons past what was sized —
    the position would carry a multiple of the intended risk. Refuse."""
    rec = _make_record(db_session, entry=100.0, stop=99.0, tp=110.0)  # planned risk 1, ~10R
    with pytest.raises(ExecutionBlocked, match="stop distance"):
        _exec_with_current(db_session, monkeypatch, rec, current=101.5)  # risk 2.5 > 1.25x planned
    assert db_session.scalars(select(Position)).all() == []


def test_drift_guard_allows_fill_at_plan(db_session, monkeypatch):
    """A normal immediate fill (price ~ the plan) passes untouched — the guard is only for drift."""
    rec = _make_record(db_session, entry=100.0, stop=95.0, tp=110.0)
    result = _exec_with_current(db_session, monkeypatch, rec, current=100.02)
    assert result.status == OrderStatus.FILLED
    assert len(db_session.scalars(select(Position)).all()) == 1


def test_drift_guard_allows_favorable_drift(db_session, monkeypatch):
    """Drift in the trader's favour (a better entry) improves R:R and must never be blocked."""
    rec = _make_record(db_session, entry=100.0, stop=95.0, tp=110.0)  # long
    result = _exec_with_current(db_session, monkeypatch, rec, current=99.0)  # cheaper entry
    assert result.status == OrderStatus.FILLED
    assert len(db_session.scalars(select(Position)).all()) == 1


# ---- live gating (unit) ----

def test_live_execution_gate():
    paper = AppSettings(id=1, broker_env="paper")
    assert live_execution_allowed(paper) is True

    live_unconfirmed = AppSettings(id=1, broker_env="live", live_confirmed_at=None)
    assert live_execution_allowed(live_unconfirmed) is False

    # A confirmation from before the process started must NOT count (re-confirm each restart).
    stale = AppSettings(id=1, broker_env="live",
                        live_confirmed_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert live_execution_allowed(stale) is False

    fresh = AppSettings(id=1, broker_env="live",
                        live_confirmed_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert live_execution_allowed(fresh) is True


# ---- monitor ----

def _open_position(session, *, direction, entry, stop, tp, qty=1.0, symbol="AAPL",
                   starting_equity=100_000.0):
    daily = get_or_create_daily_state(session, starting_equity=starting_equity)
    daily.starting_equity = starting_equity
    pos = Position(symbol=symbol, asset_class="stock", direction=direction.value, qty=qty,
                   entry_price=entry, stop_loss=stop, take_profit=tp,
                   status=PositionStatus.OPEN.value, last_price=entry, risk_amount=qty * abs(entry - stop))
    session.add_all([pos, daily])
    session.commit()
    return pos


def test_monitor_closes_on_target(db_session):
    # Long with target well below the synthetic price (~100.7) -> target hit immediately.
    pos = _open_position(db_session, direction=Direction.LONG, entry=50.0, stop=1.0, tp=60.0)
    summary = monitor_positions(db_session)
    assert summary["closed"] == 1
    db_session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.realized_pnl is not None and pos.realized_pnl > 0


def test_monitor_closes_on_stop_and_pauses_on_daily_loss(db_session):
    # Long entered far above price with a high stop -> stop hit -> large loss -> daily pause.
    _open_position(db_session, direction=Direction.LONG, entry=200.0, stop=300.0, tp=400.0,
                   qty=1.0, starting_equity=1000.0)
    summary = monitor_positions(db_session)
    assert summary["closed"] == 1
    daily = get_or_create_daily_state(db_session)
    assert daily.realized_pnl < 0
    assert daily.trading_paused is True  # loss exceeded 3% of 1000


# ---- reconciliation: app positions the broker no longer holds ----

def _pv(symbol, direction, asset_class="metal"):
    from app.models.schemas import PositionView
    return PositionView(id=1, symbol=symbol, asset_class=asset_class, direction=direction,
                        qty=0.01, entry_price=100.0, stop_loss=None, take_profit=None,
                        status="open", last_price=100.0, unrealized_pnl=0.0)


class _FakeBroker:
    """Minimal broker for reconciliation tests: controllable open book + durability flag."""
    def __init__(self, *, reconciles, book=None, raises=False):
        self.reconciles_positions = reconciles
        self._book = book or []
        self._raises = raises

    def get_open_positions(self):
        if self._raises:
            raise RuntimeError("broker down")
        return self._book


def test_reconcile_closes_phantom_on_durable_broker(db_session, monkeypatch):
    """A durable broker (MT5) that no longer reports a position -> the stale app row is closed,
    so it stops blocking new trades (anti-stacking) and inflating exposure."""
    from app.execution import monitor as monitor_mod

    real = Position(symbol="BTCUSDm", asset_class="metal", direction="short", qty=0.01,
                    entry_price=100.0, status=PositionStatus.OPEN.value, last_price=100.0)
    phantom = Position(symbol="XAUUSDm", asset_class="metal", direction="short", qty=0.01,
                       entry_price=2000.0, status=PositionStatus.OPEN.value, last_price=2000.0)
    db_session.add_all([real, phantom])
    db_session.commit()

    broker = _FakeBroker(reconciles=True, book=[_pv("BTCUSDm", "short")])  # XAU is gone at broker
    monkeypatch.setattr(monitor_mod, "get_broker_for", lambda ac, bm: broker)

    settings = get_or_create_settings(db_session)
    n = monitor_mod._reconcile_closed_at_broker(db_session, settings, [real, phantom])
    db_session.commit()
    assert n == 1
    db_session.refresh(real)
    db_session.refresh(phantom)
    assert real.status == PositionStatus.OPEN.value
    assert phantom.status == PositionStatus.CLOSED.value
    assert phantom.closed_at is not None


def test_reconcile_skips_non_durable_broker(db_session, monkeypatch):
    """The sim broker forgets positions on restart, so its empty book must NOT close DB rows."""
    from app.execution import monitor as monitor_mod

    pos = Position(symbol="AAPL", asset_class="stock", direction="long", qty=1.0,
                   entry_price=100.0, status=PositionStatus.OPEN.value, last_price=100.0)
    db_session.add(pos)
    db_session.commit()

    broker = _FakeBroker(reconciles=False, book=[])  # sim: empty, but not authoritative
    monkeypatch.setattr(monitor_mod, "get_broker_for", lambda ac, bm: broker)

    settings = get_or_create_settings(db_session)
    n = monitor_mod._reconcile_closed_at_broker(db_session, settings, [pos])
    assert n == 0
    db_session.refresh(pos)
    assert pos.status == PositionStatus.OPEN.value


def test_reconcile_skips_on_broker_outage(db_session, monkeypatch):
    """If the (durable) broker can't be reached, never close positions on the outage."""
    from app.execution import monitor as monitor_mod

    pos = Position(symbol="EURUSDm", asset_class="forex", direction="long", qty=1.0,
                   entry_price=1.1, status=PositionStatus.OPEN.value, last_price=1.1)
    db_session.add(pos)
    db_session.commit()

    broker = _FakeBroker(reconciles=True, raises=True)
    monkeypatch.setattr(monitor_mod, "get_broker_for", lambda ac, bm: broker)

    settings = get_or_create_settings(db_session)
    n = monitor_mod._reconcile_closed_at_broker(db_session, settings, [pos])
    assert n == 0
    db_session.refresh(pos)
    assert pos.status == PositionStatus.OPEN.value


# ---- flatten ----

def test_close_one_books_pnl(db_session):
    from app.execution.monitor import close_one
    pos = _open_position(db_session, direction=Direction.LONG, entry=100.0, stop=90.0, tp=110.0)
    res = close_one(db_session, pos.id)
    assert res["closed"] is True
    db_session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.realized_pnl is not None
    # Closing an already-closed position is rejected.
    assert close_one(db_session, pos.id)["closed"] is False


def test_daily_pause_triggers_on_loss_beyond_limit(db_session):
    from app.risk.service import evaluate_daily_pause
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = -40.0  # > 3% of 1000 = 30 (sim brokers report no deal history)
    db_session.commit()
    assert evaluate_daily_pause(db_session) is True
    db_session.refresh(daily)
    assert daily.trading_paused is True and daily.pause_reason


def test_daily_pause_not_triggered_on_small_loss(db_session):
    from app.risk.service import evaluate_daily_pause
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = -10.0  # < 30 limit
    db_session.commit()
    assert evaluate_daily_pause(db_session) is False
    db_session.refresh(daily)
    assert daily.trading_paused is False


def test_daily_pause_triggers_on_floating_loss_alone(db_session, monkeypatch):
    """The failure mode Task 4 fixes: ZERO closed trades, but ONE open position with a floating loss
    exceeding the daily limit -> the breaker must still trip (it no longer waits for realization)."""
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = 0.0                                   # nothing closed
    db_session.commit()
    monkeypatch.setattr(service, "total_unrealized", lambda s: -45.0)  # floating loss > 30 (3%)
    assert service.evaluate_daily_pause(db_session) is True
    db_session.refresh(daily)
    assert daily.trading_paused is True and "floating" in (daily.pause_reason or "")


def test_daily_pause_triggers_on_realized_plus_floating_combined(db_session, monkeypatch):
    """Neither realized (-20) nor floating (-15) breaches the 30 limit alone, but together (-35) they
    do -> the breaker evaluates the account's TOTAL day loss."""
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = -20.0
    db_session.commit()
    monkeypatch.setattr(service, "total_unrealized", lambda s: -15.0)
    assert service.evaluate_daily_pause(db_session) is True


def test_daily_pause_not_triggered_when_floating_profit_offsets_realized(db_session, monkeypatch):
    """A realized loss under the limit stays under when open positions are in profit (net day P&L is
    what matters) -> no false trip."""
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = -20.0
    db_session.commit()
    monkeypatch.setattr(service, "total_unrealized", lambda s: 8.0)  # net -12, under the 30 limit
    assert service.evaluate_daily_pause(db_session) is False
    db_session.refresh(daily)
    assert daily.trading_paused is False


def test_daily_pause_auto_resumes_when_floating_recovers(db_session, monkeypatch):
    """Dynamic breaker: a pause caused only by an open floating loss auto-resumes once the float
    recovers back within the limit (realized alone never breached)."""
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = 0.0
    db_session.commit()
    monkeypatch.setattr(service, "total_unrealized", lambda s: -45.0)   # trips on floating
    assert service.evaluate_daily_pause(db_session) is True
    db_session.refresh(daily); assert daily.trading_paused is True
    monkeypatch.setattr(service, "total_unrealized", lambda s: -5.0)    # float recovers -> resume
    assert service.evaluate_daily_pause(db_session) is False
    db_session.refresh(daily); assert daily.trading_paused is False and daily.pause_reason is None


def test_daily_pause_realized_breach_stays_latched_despite_floating_profit(db_session, monkeypatch):
    """A REALIZED loss beyond the limit latches for the day: an open winner masking it must NOT
    auto-resume trading (closed losses are locked in)."""
    from app.risk import service
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.starting_equity = 1000.0
    daily.realized_pnl = -35.0                                          # realized alone past 30 -> latched
    db_session.commit()
    monkeypatch.setattr(service, "total_unrealized", lambda s: 20.0)    # net -15, but realized is locked
    assert service.evaluate_daily_pause(db_session) is True
    db_session.refresh(daily); assert daily.trading_paused is True


def test_paused_account_vetoes_new_trade(db_session):
    # When paused, the risk manager must veto new entries.
    from app.risk.service import assess
    from app.models.schemas import TradeProposal
    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.trading_paused = True
    db_session.commit()
    proposal = TradeProposal(symbol="AAPL", asset_class=AssetClass.STOCK, direction=Direction.LONG,
                             entry=100.0, stop_loss=95.0, take_profit=110.0, confidence=0.8)
    decision = assess(db_session, proposal)
    assert decision.approved is False


def test_flatten_closes_all(db_session):
    _open_position(db_session, direction=Direction.LONG, entry=100.0, stop=90.0, tp=110.0, symbol="AAPL")
    _open_position(db_session, direction=Direction.SHORT, entry=100.0, stop=110.0, tp=90.0, symbol="TSLA")
    res = flatten_all(db_session)
    assert res["closed"] == 2
    open_left = db_session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()
    assert open_left == []


# ---- Mode B auto-execute ----

def test_mode_b_auto_executes_paper(db_session):
    from app.agents.pipeline import analyze_symbol
    from app.models.enums import ExecutionMode

    settings = get_or_create_settings(db_session)
    settings.execution_mode = ExecutionMode.B_AUTO_PAPER.value
    settings.broker_env = "paper"
    db_session.commit()

    res = analyze_symbol(db_session, "AAPL", AssetClass.STOCK, "1h")
    # Mode B never leaves an approved proposal merely pending — it executes (paper).
    assert res.status != ProposalStatus.PENDING_APPROVAL.value
    if res.risk.approved:
        assert res.status == ProposalStatus.EXECUTED.value
        assert db_session.scalars(select(Position)).all()


def test_confidence_calibration_buckets(db_session):
    """Closed trades bucket by entry confidence with win rate + avg R — the calibration data that
    tells us whether the engine's confidence actually predicts outcomes."""
    from app.api.journal_routes import calibration

    def closed(conf, pnl, risk=10.0):
        return Position(symbol="X", asset_class="forex", direction="long", qty=1.0, entry_price=100.0,
                        status=PositionStatus.CLOSED.value, realized_pnl=pnl, risk_amount=risk,
                        confidence=conf)

    db_session.add_all([closed(0.75, 20.0), closed(0.72, -10.0), closed(0.95, 30.0)])
    db_session.commit()
    by = {b.bucket: b for b in calibration(session=db_session)}
    assert by["70-80%"].trades == 2 and by["70-80%"].wins == 1 and by["70-80%"].win_rate == 0.5
    assert abs(by["70-80%"].avg_r - 0.5) < 1e-6           # (+2R, -1R) -> mean 0.5R
    assert by["90-100%"].trades == 1 and by["90-100%"].win_rate == 1.0
    assert by["50-60%"].trades == 0 and by["50-60%"].win_rate is None


def test_journal_breakdown_by_source_and_period(db_session):
    """Closed trades group by source (who opened them) with win rate + net P&L + R, plus a daily/
    weekly/monthly split — the comparison that shows which mechanism actually makes money."""
    from app.api.journal_routes import breakdown
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def closed(source, pnl, i, risk=10.0, sym="EURUSDm"):
        return Position(symbol=sym, asset_class="forex", direction="long", qty=1.0, entry_price=100.0,
                        status=PositionStatus.CLOSED.value, realized_pnl=pnl, risk_amount=risk,
                        source=source, closed_at=base + timedelta(days=i))

    db_session.add_all([
        closed("ai", 20.0, 0), closed("ai", -10.0, 1),   # ai: 2 trades, 1 win, net +10, +1R total
        closed("rsi_over", 30.0, 2),                       # rsi_over: 1 win, net +30
        closed("armed", -5.0, 3),                          # armed: 1 loss, net -5
    ])
    db_session.commit()

    b = breakdown(session=db_session)
    by = {g.label: g for g in b.by_source}
    assert by["ai"].trades == 2 and by["ai"].wins == 1 and by["ai"].win_rate == 0.5
    assert by["ai"].net_pnl == 10.0 and by["ai"].total_r == 1.0 and by["ai"].avg_r == 0.5
    assert by["rsi_over"].net_pnl == 30.0 and by["rsi_over"].win_rate == 1.0
    assert by["armed"].losses == 1 and by["armed"].net_pnl == -5.0
    assert b.by_source[0].label == "rsi_over"                 # sorted by net P&L desc
    assert len(b.daily) == 4                                  # 4 distinct days
    assert len(b.monthly) == 1 and b.monthly[0].total.trades == 4   # all in 2026-07


def test_journal_stats_expectancy_and_drawdown(db_session):
    """Closed trades roll up into expectancy (mean R), profit factor, and max R-drawdown."""
    from app.api.journal_routes import stats

    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def closed(pnl, i, risk=10.0):
        return Position(symbol="X", asset_class="forex", direction="long", qty=1.0, entry_price=100.0,
                        status=PositionStatus.CLOSED.value, realized_pnl=pnl, risk_amount=risk,
                        closed_at=base + timedelta(hours=i))

    db_session.add_all([closed(20.0, 0), closed(-10.0, 1), closed(10.0, 2)])  # R: +2, -1, +1
    db_session.commit()
    s = stats(session=db_session)
    assert s.trades == 3 and s.wins == 2 and s.losses == 1
    assert s.expectancy_r == 0.67 and s.total_r == 2.0
    assert s.avg_win_r == 1.5 and s.avg_loss_r == -1.0
    assert s.profit_factor == 3.0          # gross win 30 / gross loss 10
    assert s.max_drawdown_r == 1.0         # cum 2 -> 1 -> 2; worst dip from peak = 1R
