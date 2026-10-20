"""Money-safety regressions from the full review (2026-10-20). Each test reproduces a scenario that
put real money at risk before the fix."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.enums import AssetClass, Direction, OrderSide, OrderStatus, OrderType
from app.models.schemas import AccountState, OrderRequest, RiskLimits, TradeProposal

_LIMITS = RiskLimits(risk_per_trade=0.03, max_open_positions=5, max_daily_loss=0.1, max_total_exposure=0.09,
                     per_pair_cooldown_minutes=15, loss_cooldown_minutes=15, risk_per_trade_ceiling=0.03)


class FakeTerminal:
    """An MT5 module whose symbol reads fail (an IPC hiccup) while account reads work."""
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self):
        self.trade_mode = 0
        self.login = 111
        self.sent = []

    def symbol_info(self, s):
        return None

    def order_calc_profit(self, *a):
        return None

    def symbol_select(self, *a):
        return True

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(trade_mode=self.trade_mode, equity=1290.0, balance=1290.0, trade_expert=True,
                               login=self.login)

    def order_send(self, req):
        self.sent.append(req)
        return None


def _adapter(fake):
    from app.brokers.mt5_adapter import Mt5BrokerAdapter

    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._mt5 = fake
    a._paper = True
    a._paper_checked = 0.0
    a._login = 111
    a._symbol_cache = {"XAUUSDm": "XAUUSDm"}
    return a


def _gold():
    return TradeProposal(symbol="XAUUSDm", asset_class=AssetClass.METAL, timeframe="1h", direction=Direction.LONG,
                         entry=4000.0, stop_loss=3985.0, take_profit=4045.0, confidence=0.8)


def test_a_real_broker_that_cannot_price_one_lot_is_vetoed_not_sized_as_one_unit():
    """Before: symbol_info/order_calc_profit returned None for one read, contract_size answered 1.0,
    and the manager approved 2.58 lots of gold recorded as $38.70 risk - really ~$3,870."""
    from app.risk.manager import evaluate_proposal
    from app.risk.service import _real_broker, _risk_per_lot

    a = _adapter(FakeTerminal())
    rpl = _risk_per_lot(a, "XAUUSDm", 4000.0, 3985.0)
    assert rpl is None                                    # no guess for a real broker
    dec = evaluate_proposal(_gold(), AccountState(equity=1290.0, cash=1290.0, open_positions=0), _LIMITS,
                            now=datetime.now(timezone.utc), risk_per_lot=rpl,
                            risk_per_lot_required=_real_broker(a))
    assert not dec.approved
    assert "can't price the risk" in dec.reason


def test_the_simulator_still_sizes_from_price_distance():
    from app.brokers.sim import SimPaperBroker
    from app.risk.service import _risk_per_lot

    sim = SimPaperBroker.__new__(SimPaperBroker)
    assert _risk_per_lot(sim, "AAA", 100.0, 99.0) == 1.0  # 1 lot == 1 unit is right for the sim


def test_switching_the_terminal_to_a_real_account_is_seen():
    """Before: is_paper was read once at connect and stayed True after the terminal switched to the
    real account, so every real-money gate let automated orders through."""
    fake = FakeTerminal()
    a = _adapter(fake)
    assert a.is_paper is True
    fake.trade_mode = 2                                   # the user logs the same terminal into REAL
    a._paper_checked = 0.0                                # (past the 5 s re-check window)
    assert a.is_paper is False


def test_an_unreadable_account_counts_as_real():
    fake = FakeTerminal()
    fake.account_info = lambda: None
    assert _adapter(fake).is_paper is False


def test_orders_are_refused_after_the_terminal_switches_account():
    fake = FakeTerminal()
    a = _adapter(fake)
    fake.login = 222                                      # a different account now
    res = a.submit_order(OrderRequest(symbol="XAUUSDm", asset_class=AssetClass.METAL, side=OrderSide.BUY,
                                      qty=0.01, order_type=OrderType.MARKET, stop_loss=3985.0))
    assert res.status == OrderStatus.REJECTED
    assert "#222" in res.error and "#111" in res.error
    assert fake.sent == []                                # nothing reached the broker


# ---------- manual Close: the app's rows follow the BROKER's answer ----------

def _open_row(db, symbol="EURUSDm"):
    from app.models.db import Position
    from app.models.enums import PositionStatus

    pos = Position(symbol=symbol, asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                   status=PositionStatus.OPEN.value, last_price=1.09, unrealized_pnl=-100.0,
                   risk_amount=40.0, stop_loss=1.096)
    db.add(pos)
    db.commit()
    return pos


def _close_with(monkeypatch, result):
    from app.brokers import registry

    class _B:
        is_paper = True
        name = "fake"

        def close_position(self, s, direction=None):
            return result

    monkeypatch.setattr(registry, "get_broker_for", lambda ac, bm=None: _B())


def test_a_refused_close_leaves_the_position_open(db_session, monkeypatch):
    """Before: the row was marked CLOSED and -$100 booked, then 409 returned - the live position fell
    out of the exposure budget."""
    import pytest
    from fastapi import HTTPException

    from app.api.settings_routes import LiveCloseRequest, live_close
    from app.models.enums import PositionStatus
    from app.models.schemas import OrderResult

    pos = _open_row(db_session)
    _close_with(monkeypatch, OrderResult(status=OrderStatus.ERROR,
                                         error="MT5 'Algo Trading' is switched OFF in the terminal"))
    with pytest.raises(HTTPException) as ei:
        live_close(LiveCloseRequest(symbol="EURUSDm", asset_class=AssetClass.FOREX), db_session)
    assert ei.value.status_code == 409 and "Algo Trading" in ei.value.detail
    db_session.refresh(pos)
    assert pos.status == PositionStatus.OPEN.value
    assert pos.realized_pnl is None


def test_a_confirmed_close_closes_the_row(db_session, monkeypatch):
    from app.api.settings_routes import LiveCloseRequest, live_close
    from app.models.enums import PositionStatus
    from app.models.schemas import OrderResult

    pos = _open_row(db_session)
    _close_with(monkeypatch, OrderResult(status=OrderStatus.SUBMITTED))
    out = live_close(LiveCloseRequest(symbol="EURUSDm", asset_class=AssetClass.FOREX), db_session)
    assert out["status"] == "closed"
    db_session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value
    assert pos.realized_pnl == -100.0          # no broker history in the test: the last mark


def test_a_position_the_broker_no_longer_holds_is_closed_in_the_app(db_session, monkeypatch):
    from app.api.settings_routes import LiveCloseRequest, live_close
    from app.models.enums import PositionStatus
    from app.models.schemas import OrderResult

    pos = _open_row(db_session)
    _close_with(monkeypatch, OrderResult(status=OrderStatus.REJECTED, error="no open MT5 position for EURUSDm"))
    out = live_close(LiveCloseRequest(symbol="EURUSDm", asset_class=AssetClass.FOREX), db_session)
    assert out["status"] == "already_closed"
    db_session.refresh(pos)
    assert pos.status == PositionStatus.CLOSED.value


class _TwoTickets(FakeTerminal):
    """Two tickets on one symbol; the FIRST close is refused (e.g. market closed)."""
    TRADE_ACTION_DEAL = 1
    POSITION_TYPE_BUY = 0
    TRADE_RETCODE_DONE = 10009
    SYMBOL_FILLING_FOK = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2

    def __init__(self):
        super().__init__()
        self.n = 0

    def symbol_info(self, s):
        return SimpleNamespace(filling_mode=1, volume_step=0.01, volume_min=0.01, volume_max=100)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=1.1, ask=1.1002, time=0)

    def positions_get(self, symbol=None):
        return [SimpleNamespace(type=0, volume=0.1, ticket=1), SimpleNamespace(type=0, volume=0.1, ticket=2)]

    def order_send(self, req):
        self.n += 1
        return SimpleNamespace(retcode=10018 if self.n == 1 else 10009, comment="Market closed")

    def last_error(self):
        return (0, "ok")


def test_mt5_close_is_an_error_if_any_ticket_failed():
    """Before: only the LAST ticket's result was returned, so a failed first close read as success."""
    a = _adapter(_TwoTickets())
    a._symbol_cache = {"EURUSDm": "EURUSDm"}
    res = a.close_position("EURUSDm")
    assert res.status == OrderStatus.ERROR
    assert res.error


# ---------- the final risk check inside the order lock ----------

def _pending_stock(db, qty=10.0, entry=100.0, stop=95.0, tp=110.0):
    from app.models.db import TradeProposalRecord
    from app.models.enums import ProposalStatus

    rec = TradeProposalRecord(
        symbol="AAPL", asset_class="stock", timeframe="1h", direction="long",
        entry=entry, stop_loss=stop, take_profit=tp, confidence=0.7, rationale="t", reasoning={},
        status=ProposalStatus.PENDING_APPROVAL.value, risk_decision="approved",
        approved_qty=qty, risk_amount=qty * abs(entry - stop))
    db.add(rec)
    db.commit()
    return rec


def test_approving_without_a_size_cannot_skip_the_daily_loss_breaker(db_session):
    """Before: approve(lots=None) went straight to the executor, which did not re-run the Risk
    Manager - with the daily-loss breaker tripped the trade still opened."""
    import pytest
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.api.proposal_routes import approve_proposal
    from app.core.state import get_or_create_daily_state
    from app.models.db import Position

    daily = get_or_create_daily_state(db_session, starting_equity=1000.0)
    daily.trading_paused = True
    db_session.commit()
    rec = _pending_stock(db_session)
    with pytest.raises(HTTPException) as ei:
        approve_proposal(rec.id, None, db_session)
    assert ei.value.status_code == 423
    assert "risk check at execution" in ei.value.detail
    assert db_session.scalars(select(Position)).all() == []


def test_the_order_is_never_bigger_than_approved_and_shrinks_for_a_wider_real_stop(db_session, monkeypatch):
    """The fill-to-stop distance at execution is what the money actually risks."""
    from app.execution import executor as ex

    rec = _pending_stock(db_session, qty=10.0, entry=100.0, stop=95.0)
    rec.status = "approved"
    db_session.commit()

    class _Q:
        price = 101.0                        # 1.0 above plan: stop is 6.0 away, not 5.0

    class _B:
        name = "sim"
        is_paper = True

        def get_quote(self, s):
            return _Q()

        def spread(self, s):
            return 0.0

    captured = {}

    def fake_assess(session, proposal, override_risk_fraction=None, **kw):
        from app.models.enums import RiskDecisionType
        from app.models.schemas import RiskDecision

        captured["entry"] = proposal.entry
        # the manager sizes the same $ risk over the wider distance: 50 / 6 = 8.33 lots
        return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="AAPL",
                            approved_qty=8.33, risk_amount=49.98)

    monkeypatch.setattr("app.risk.service.assess", fake_assess)
    monkeypatch.setattr("app.risk.service.current_equity", lambda s: 1000.0)
    ex._final_risk_gate(db_session, _B(), rec)
    assert captured["entry"] == 101.0          # sized at the live fill, not the stale plan
    assert rec.approved_qty == 8.33 and rec.risk_amount == 49.98


# ---------- news blackout survives a calendar outage ----------

def test_a_calendar_outage_keeps_the_last_good_events(monkeypatch):
    """Before: one failed fetch cached an EMPTY list for an hour - no news blackout at all."""
    import httpx
    from datetime import timedelta

    from app.data.providers import TradingViewCalendarProvider

    p = TradingViewCalendarProvider()
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": [{"importance": 1, "date": soon, "country": "US", "title": "CPI"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    good = p.get_events("EURUSDm")
    assert [e.label for e in good] == ["US: CPI"]

    def down(*a, **k):
        raise httpx.ConnectError("getaddrinfo failed")

    monkeypatch.setattr(httpx, "get", down)
    key = next(iter(p._cache))
    p._cache[key] = (p._cache[key][0] - timedelta(hours=2), p._cache[key][1])   # the hour is up
    assert [e.label for e in p.get_events("EURUSDm")] == ["US: CPI"]           # still blacked out
    # ...and a later success replaces it as usual
    p._failed_at.clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    assert p.get_events("EURUSDm")


# ---------- the macro "hold for news" gate really holds ----------

def test_a_setup_held_for_news_is_not_executable():
    from app.risk.manager import evaluate_proposal

    p = _gold().model_copy(update={"macro_wait": True})
    dec = evaluate_proposal(p, AccountState(equity=1290.0, cash=1290.0, open_positions=0), _LIMITS,
                            now=datetime.now(timezone.utc), risk_per_lot=1500.0)
    assert not dec.approved and "held for high-impact news" in dec.reason


def test_the_pipeline_hold_no_longer_crashes(db_session, monkeypatch):
    """Before: proposal.is_actionable() on a property raised TypeError, swallowed as 'macro context
    unavailable' - so a blocked setup was never held."""
    import app.macro.service as macro_service
    from app.agents.pipeline import analyze_symbol

    monkeypatch.setattr(macro_service, "macro_context",
                        lambda *a, **k: {"blocked": True, "gate_reason": "US CPI in 20 min"})
    out = analyze_symbol(db_session, "EURUSD", AssetClass.FOREX, "1h", use_llm=False, no_execute=True)
    prop = out.proposal
    assert prop.macro is not None and prop.macro.get("blocked") is True   # the macro block ran
    if prop.is_actionable:
        assert prop.macro_wait is True and prop.rationale.startswith("HELD")
        assert not out.risk.approved and "held for high-impact news" in out.risk.reason


# ---------- no decision on partial context ----------

def test_a_missing_higher_timeframe_means_no_trade_not_a_looser_gate():
    """Before: an empty 4h read as 'sideways', which PASSES the next-timeframe gate."""
    from app.agents.pipeline import _missing_context, _no_trade_for_missing_context
    from app.models.schemas import Candle, OHLCVSeries

    bars = [Candle(ts=datetime(2026, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1)] * 200
    full = [OHLCVSeries(symbol="X", timeframe=tf, candles=bars) for tf in ("1h", "4h", "1d")]
    assert _missing_context(full, "1h") is None
    no4h = [full[0], OHLCVSeries(symbol="X", timeframe="4h", candles=[]), full[2]]
    assert "4h (0 bars)" in _missing_context(no4h, "1h")
    dropped = [full[0], full[1]]                               # the 1d fetch raised and was dropped
    assert "1d" in _missing_context(dropped, "1h")
    p = _no_trade_for_missing_context(_gold(), "4h (0 bars)")
    assert p.direction == Direction.NO_TRADE and p.entry is None and "Context data missing" in p.rationale


# ---------- a hedge on the same symbol is left alone ----------

class _Hedge(_TwoTickets):
    """EURUSD long (ticket 1) and short (ticket 2) open together."""

    def positions_get(self, symbol=None):
        return [SimpleNamespace(type=0, volume=0.1, ticket=1), SimpleNamespace(type=1, volume=0.1, ticket=2)]

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, comment="done")


def test_closing_one_side_leaves_the_opposite_hedge_open():
    """Before: close_position(symbol) closed EVERY ticket - the DB shows long+short pairs closed in
    the same microsecond."""
    fake = _Hedge()
    a = _adapter(fake)
    a._symbol_cache = {"EURUSDm": "EURUSDm"}
    res = a.close_position("EURUSDm", direction="long")
    assert res.status == OrderStatus.SUBMITTED
    assert [r["position"] for r in fake.sent] == [1]          # only the long's ticket
    fake.sent.clear()
    a.close_position("EURUSDm")                                  # no direction = all (kill switch)
    assert sorted(r["position"] for r in fake.sent) == [1, 2]


# ---------- the broker's own stop/target win over the app's copy ----------

def test_a_target_moved_in_the_terminal_is_respected(db_session, monkeypatch):
    """Before: the user extended the TP in the Exness app; when mid touched the OLD target the monitor
    market-closed the trade anyway."""
    import app.execution.monitor as M
    from app.models.db import Position
    from app.models.enums import PositionStatus
    from app.models.schemas import PositionView

    pos = Position(symbol="EURUSDm", asset_class="forex", direction="long", qty=0.1, entry_price=1.10,
                   stop_loss=1.098, take_profit=1.106, status=PositionStatus.OPEN.value, risk_amount=20.0)
    db_session.add(pos)
    db_session.commit()
    closed = []

    class _Mt5Like:
        name = "mt5"
        reconciles_positions = True
        is_paper = True

        def get_quote(self, s):
            return SimpleNamespace(price=1.1061)                # at/past the OLD target

        def get_open_positions(self):
            return [PositionView(id=1, symbol="EURUSDm", asset_class="forex", direction="long", qty=0.1,
                                 entry_price=1.10, stop_loss=1.098, take_profit=1.110, status='open')]   # moved to 1.110

        def close_position(self, s, direction=None):
            closed.append(s)

        def position_profit(self, *a):
            return 6.1

    monkeypatch.setattr(M, "get_broker_for", lambda ac, bm: _Mt5Like())
    monkeypatch.setattr(M, "_reconcile_closed_at_broker", lambda *a, **k: False)
    M.monitor_positions(db_session)
    db_session.refresh(pos)
    assert closed == []
    assert pos.status == PositionStatus.OPEN.value and pos.take_profit == 1.110


# ---------- R is measured against the risk AT ENTRY ----------

def test_r_multiple_uses_the_entry_risk_not_the_moved_stop(db_session):
    """Live #581: lost $71.10 on $36.13 risked at entry = -1.97R, booked as -1.01R after a widen."""
    from app.models.db import Position, entry_risk
    from app.models.enums import PositionStatus
    from app.risk.service import _position_r_multiple

    pos = Position(symbol="EURUSDm", asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                   status=PositionStatus.CLOSED.value, realized_pnl=-71.10,
                   risk_amount=70.40, initial_risk_amount=36.13)
    assert entry_risk(pos) == 36.13
    assert round(_position_r_multiple(pos), 2) == -1.97
    old = Position(symbol="X", asset_class="forex", direction="long", qty=1, entry_price=1,
                   realized_pnl=10.0, risk_amount=20.0)        # pre-column row: falls back
    assert entry_risk(old) == 20.0


# ---------- exposure counts trades the app did not open ----------

def test_a_phone_trade_counts_against_the_exposure_budget(db_session):
    """Before: a broker position with no app row added NOTHING to the 9% budget."""
    from app.models.schemas import PositionView
    from app.risk.service import _untracked_broker_risk

    class _B:
        name = "mt5"
        reconciles_positions = True

        def get_open_positions(self):
            return [PositionView(id=1, symbol="XAUUSDm", asset_class="metal", direction="long", qty=0.10,
                                 entry_price=4000.0, stop_loss=3990.0, status="open"),
                    PositionView(id=2, symbol="EURUSDm", asset_class="forex", direction="short", qty=0.10,
                                 entry_price=1.1, stop_loss=None, status="open")]

        def position_profit(self, sym, d, lots, entry, exit_):
            return -100.0                                   # 0.10 lot gold, $10 adverse = -$100

    extra = _untracked_broker_risk(db_session, _B(), [], equity=1000.0)
    # $100 at the gold stop + a full 3% allowance for the stop-less EURUSD
    assert round(extra, 2) == 130.0


def test_a_market_closed_refusal_marks_the_symbol_closed():
    """Index/oil CFDs keep quoting through the daily break, so the tick-age test read them open."""
    import app.brokers.mt5_adapter as m

    class _T(FakeTerminal):
        TRADE_ACTION_DEAL = 1
        TRADE_RETCODE_DONE = 10009
        ORDER_FILLING_FOK = 0

        def symbol_info(self, s):
            return SimpleNamespace(filling_mode=1, volume_step=0.01, volume_min=0.01, volume_max=100,
                                   trade_mode=4)

        def symbol_info_tick(self, s):
            import time as _t
            return SimpleNamespace(bid=100.0, ask=100.1, time=int(_t.time()))   # still quoting

        def order_send(self, req):
            return SimpleNamespace(retcode=10018, comment="Market closed", volume=0, price=0)

    a = _adapter(_T())
    a._symbol_cache = {"UK100m": "UK100m"}
    m._CLOSED_REFUSALS.clear()
    assert a.market_open("UK100m") is True
    a.submit_order(OrderRequest(symbol="UK100m", asset_class=AssetClass.INDEX, side=OrderSide.BUY,
                                qty=0.1, order_type=OrderType.MARKET, stop_loss=99.0))
    assert a.market_open("UK100m") is False
    m._CLOSED_REFUSALS.clear()


def test_a_kill_switch_pressed_during_a_tick_is_seen_by_the_executor(db_session):
    """Before: the tick's session still held kill_switch=False from when it started."""
    import pytest

    from app.core import database as db
    from app.core.state import get_or_create_settings
    from app.execution import executor as ex

    rec = _pending_stock(db_session)
    rec.status = "approved"
    db_session.commit()
    get_or_create_settings(db_session)                    # the tick has read the settings (switch OFF)
    with db.SessionLocal() as other:                       # the user presses KILL elsewhere
        get_or_create_settings(other).kill_switch_engaged = True
        other.commit()
    with pytest.raises(ex.ExecutionBlocked, match="kill-switch"):
        ex.execute_proposal(db_session, rec)


def test_the_daily_review_reports_today_and_tags(db_session):
    from datetime import timedelta

    from app.api.journal_routes import daily_review
    from app.models.db import Position
    from app.models.enums import PositionStatus

    now = datetime.now(timezone.utc)
    for i, pnl in enumerate((-10.0, -10.0, 20.0)):
        db_session.add(Position(symbol="EURUSDm", asset_class="forex", direction="long", qty=0.1, entry_price=1.1,
                                status=PositionStatus.CLOSED.value, realized_pnl=pnl, risk_amount=10.0,
                                initial_risk_amount=10.0, closed_at=now - timedelta(minutes=30 - i),
                                review={"note": "x", "tags": ["plan"]}))
    db_session.commit()
    out = daily_review(db_session)
    assert len(out["trades"]) == 3 and out["total_r"] == 0.0 and out["longest_losing_run"] == 2
    assert out["by_tag"] == [] and out["tags_below_10"] == ["plan"]
