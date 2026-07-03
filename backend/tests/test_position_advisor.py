"""Tests for the open-position management advisor (protect winners / cut losers around news)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.agents.position_advisor as advisor
from app.data.providers import CalendarEvent
from app.models.schemas import PositionAdvice, PositionView

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_advisor_state():
    advisor._reset_auto_state()
    yield
    advisor._reset_auto_state()


def _pos(symbol="XAUUSDm", direction="short", pnl=10.0, stop=4473.0, tp=4397.0) -> PositionView:
    return PositionView(
        id=1, symbol=symbol, asset_class="metal", direction=direction, qty=1.0,
        entry_price=4449.0, stop_loss=stop, take_profit=tp, status="open",
        last_price=4439.0, unrealized_pnl=pnl,
    )


class _Cal:
    def __init__(self, events):
        self._events = events

    def get_events(self, symbol, lookahead_hours=24, include_medium=False):
        return self._events


def _patch(monkeypatch, positions, events, thesis=None):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: positions)
    monkeypatch.setattr(advisor, "get_calendar_provider", lambda: _Cal(events))
    # Isolate the event/protection logic from the (broker-dependent) thesis re-check unless a
    # test explicitly wants a thesis.
    monkeypatch.setattr(advisor, "_position_context", lambda session, p: None)
    monkeypatch.setattr(advisor, "_position_thesis", lambda session, p, ctx=None: thesis)


def _event(mins_from_now=45, importance="high"):
    return CalendarEvent(label="US: ISM Services PMI", when=NOW + timedelta(minutes=mins_from_now),
                         importance=importance, country="US")


def test_winning_into_event_says_lock_in(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=10.0)], [_event(45)])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "warn"
    assert "winning" in a.headline.lower()
    assert "lock" in a.detail.lower()
    assert a.event_label == "US: ISM Services PMI"


def test_losing_into_event_says_cut(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=-15.0)], [_event(30)])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "warn"
    assert "losing" in a.headline.lower()
    assert "clos" in a.detail.lower() or "reduc" in a.detail.lower()


def test_no_stop_into_event_is_danger(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=5.0, stop=None)], [_event(20)])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "danger"
    assert "no stop" in a.headline.lower()


def test_no_event_winner_holds(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [])
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "info"
    assert "profit" in a.headline.lower()


def test_far_off_event_is_not_imminent(monkeypatch):
    # An event 6h out should not trigger the news branch.
    _patch(monkeypatch, [_pos(pnl=8.0)], [_event(360)])
    [a] = advisor.advise_positions(session=None)
    assert a.event_label is None and a.severity == "info"


def test_medium_event_is_soft_heads_up_not_a_warning(monkeypatch):
    # A MEDIUM-impact event (e.g. a Fed speech) shows as a soft heads-up, NOT the hard news warning.
    med = _event(mins_from_now=40, importance="medium")
    _patch(monkeypatch, [_pos(pnl=8.0)], [med])
    [a] = advisor.advise_positions(session=None)
    assert a.events_soon and "ISM" in a.events_soon   # surfaced softly
    assert a.event_label is None and a.severity == "info"  # not gated/escalated


# ---- thesis re-check folding ----

def test_invalidated_thesis_escalates_to_danger(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [],
           thesis={"label": "invalidated", "note": "Plan check: trend flipped."})
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "danger" and a.thesis == "invalidated"
    assert "trend flipped" in a.headline.lower()
    assert "plan check" in a.detail.lower()


def test_weakening_thesis_escalates_info_to_warn(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [],
           thesis={"label": "weakening", "note": "Plan check: momentum rolling over."})
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "warn" and a.thesis == "weakening"


def test_intact_thesis_stays_info_and_appends_note(monkeypatch):
    _patch(monkeypatch, [_pos(pnl=8.0)], [],
           thesis={"label": "intact", "note": "Plan check: thesis intact."})
    [a] = advisor.advise_positions(session=None)
    assert a.severity == "info" and a.thesis == "intact"
    assert "thesis intact" in a.detail.lower()


def test_event_keeps_headline_even_when_thesis_invalidated(monkeypatch):
    # News is the nearer concern: it keeps the headline, but the thesis still bumps severity.
    _patch(monkeypatch, [_pos(pnl=10.0)], [_event(30)],
           thesis={"label": "invalidated", "note": "Plan check: trend flipped."})
    [a] = advisor.advise_positions(session=None)
    assert "winning" in a.headline.lower() and a.severity == "danger"


# ---- auto-watch config + tick ----

def test_run_advisor_stamps_last_run(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [])
    out = advisor.run_advisor(db_session)
    assert "last_run_at" in out and out["advice"] == []
    cfg = advisor.get_or_create_advisor_config(db_session)
    assert cfg.last_run_at is not None


def test_advisor_tick_respects_disabled(db_session):
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.enabled = False
    db_session.commit()
    assert advisor.advisor_tick(db_session)["ran"] is False


def test_advisor_tick_runs_when_enabled(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [])
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.enabled = True
    cfg.interval_seconds = 60
    db_session.commit()
    assert advisor.advisor_tick(db_session)["ran"] is True
    # Interval hasn't elapsed -> should not run again immediately.
    second = advisor.advisor_tick(db_session)
    assert second["ran"] is False and second["reason"] == "interval not elapsed"


# ---- auto-execute ----

def _adv(symbol="XAUUSDm", thesis="intact", event=None, sev="info"):
    return PositionAdvice(symbol=symbol, direction="short", unrealized_pnl=10.0, has_stop=True,
                          severity=sev, headline="h", detail="d", thesis=thesis, event_label=event)


class _Result:
    def __init__(self, value="filled", error=None):
        self.status = type("S", (), {"value": value})()
        self.error = error


class _Broker:
    def __init__(self, is_paper=True):
        self.is_paper = is_paper
        self.closed = []
        self.sltp = []
        self.partials = []

    def close_position(self, symbol):
        self.closed.append(symbol)
        return _Result("filled")

    def close_partial(self, symbol, fraction):
        self.partials.append((symbol, fraction))
        return _Result("filled")

    def set_sl_tp(self, symbol, sl, tp):
        self.sltp.append((symbol, sl, tp))
        return _Result("filled")


def test_auto_decision_closes_invalidated():
    p = _pos(direction="short")
    assert advisor._auto_decision(_adv(thesis="invalidated"), p, {}, None)["action"] == "close"


def test_auto_decision_breakeven_winning_into_news():
    # short winning, stop above entry (worse side) -> lock to breakeven.
    p = _pos(direction="short", pnl=10.0, stop=4460.0)  # entry 4449 -> stop above = worse
    decision = advisor._auto_decision(_adv(thesis="intact", event="US: ISM"), p, {}, None)
    assert decision is not None and decision["kind"] == "breakeven"


def test_auto_decision_protects_naked_position():
    p = _pos(direction="short", stop=None)
    decision = advisor._auto_decision(_adv(thesis="intact"), p, {"atr": 10.0, "last": 4449.0}, None)
    assert decision is not None and decision["kind"] == "protect"
    assert decision["stop"] > 4449.0  # protective stop above price for a short


def test_auto_decision_trails_beyond_target_r():
    # short, big profit (last well below entry) -> trail; plan_risk 10, profit ~50 = 5R.
    # already_scaled=True so we test the trail that manages the remainder after a partial.
    p = _pos(direction="short", stop=4460.0)
    decision = advisor._auto_decision(_adv(thesis="intact"), p,
                                      {"atr": 5.0, "last": 4399.0}, 10.0, already_scaled=True)
    assert decision is not None and decision["kind"] == "trail"
    assert decision["stop"] < 4460.0  # tighter than the current stop


def test_auto_decision_none_when_intact_no_event():
    assert advisor._auto_decision(_adv(thesis="intact"), _pos(), {}, None) is None


def test_auto_decision_tightens_on_weakening_with_meaningful_momentum():
    # short ~+0.5R, weakening with MEANINGFUL counter-momentum (MACD +1.5 >= 0.25*ATR) -> tighten.
    p = _pos(direction="short", stop=4470.0)
    decision = advisor._auto_decision(_adv(thesis="weakening"), p,
                                      {"atr": 5.0, "last": 4444.0, "macd_hist": 1.5}, 10.0)
    assert decision is not None and decision["kind"] == "tighten"
    assert decision["stop"] < 4470.0  # only ever tighter than the current stop


def test_auto_decision_no_tighten_on_tiny_momentum():
    # +0.5R and weakening, but a near-zero MACD (0.3 < 0.25*ATR=1.25) is noise -> do NOT scratch it.
    p = _pos(direction="short", stop=4470.0)
    assert advisor._auto_decision(_adv(thesis="weakening"), p,
                                  {"atr": 5.0, "last": 4444.0, "macd_hist": 0.3}, 10.0) is None


def test_auto_decision_no_tighten_below_profit_floor():
    # Only +0.3R (< 0.5R floor) even with meaningful momentum -> too early to tighten.
    p = _pos(direction="short", stop=4470.0)
    assert advisor._auto_decision(_adv(thesis="weakening"), p,
                                  {"atr": 5.0, "last": 4446.0, "macd_hist": 1.5}, 10.0) is None


def test_auto_decision_tightens_on_weakening_with_choch():
    # +0.5R and weakening via a change-of-character (structure break) -> tighten even without momentum.
    p = _pos(direction="short", stop=4470.0)
    decision = advisor._auto_decision(_adv(thesis="weakening"), p,
                                      {"atr": 5.0, "last": 4444.0, "choch": True}, 10.0)
    assert decision is not None and decision["kind"] == "tighten"


# --- multi-timeframe invalidation gating (entry-TF flip needs higher-TF confirmation) ---

def test_thesis_lone_tf_flip_is_weakening_not_invalidated():
    p = _pos(direction="long")
    # entry-TF flipped DOWN against the long, but the higher TF is still UP -> pullback, weakening.
    ctx = {"tf": "1h", "trend": "down", "macro": "up", "macro_tf": "1d", "macd_hist": 0.0, "atr": 5.0}
    assert advisor._thesis_from_context(p, ctx)["label"] == "weakening"


def test_thesis_flip_confirmed_on_higher_tf_is_invalidated():
    p = _pos(direction="long")
    ctx = {"tf": "1h", "trend": "down", "macro": "down", "macro_tf": "1d", "macd_hist": 0.0, "atr": 5.0}
    assert advisor._thesis_from_context(p, ctx)["label"] == "invalidated"


# --- structure / regime-aware exit management (senior-trader exits) ---

def test_choch_against_position_warns_weakening():
    # Long with an intact up-trend, but a change-of-character (broke the last higher-low).
    p = _pos(direction="long")
    ctx = {"tf": "1h", "trend": "up", "macro": "up", "macro_tf": "1d", "macd_hist": 1.0,
           "atr": 2.0, "structure": "up", "choch": True}
    res = advisor._thesis_from_context(p, ctx)
    assert res["label"] == "weakening" and "early warning" in res["note"].lower()


def test_trail_behind_structure_in_trending_regime():
    # tp=None: the target was already removed (the 'let it run' state), so this isolates the
    # plain structural trail that manages the runner from then on.
    p = _pos(direction="long", stop=4455.0, tp=None)  # entry 4449, stop already past breakeven
    ctx = {"atr": 2.0, "last": 4470.0, "regime": "trending", "swing_low": 4460.0}
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0, already_scaled=True)  # +2.1R
    assert d is not None and d["kind"] == "trail" and "structure" in d["reason"]
    assert abs(d["stop"] - 4459.6) < 0.01  # swing 4460 - 0.2*ATR(2)


def test_trail_atr_in_volatile_regime():
    p = _pos(direction="long", stop=4455.0)
    ctx = {"atr": 2.0, "last": 4470.0, "regime": "volatile", "swing_low": 4460.0}
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0, already_scaled=True)
    assert d is not None and d["kind"] == "trail" and "ATR" in d["reason"]
    assert abs(d["stop"] - 4468.0) < 0.01  # 4470 - 1*ATR(2) (tighter than structure in a chop)


def test_volatile_regime_banks_breakeven_earlier():
    # +0.6R: trending waits (needs +1R) but volatile banks at +0.5R.
    p = _pos(direction="long", stop=4445.0)  # stop below entry (worse side)
    base = {"atr": 2.0, "last": 4455.0}  # profit 6 / plan_risk 10 = 0.6R
    trend = advisor._auto_decision(_adv(thesis="intact"), p, {**base, "regime": "trending"}, 10.0)
    volat = advisor._auto_decision(_adv(thesis="intact"), p, {**base, "regime": "volatile"}, 10.0)
    assert trend is None
    assert volat is not None and volat["kind"] == "breakeven"


def test_protect_behind_structure_not_entry():
    # Long, +1R, stop still below entry, and a swing low is available -> protect behind the last
    # swing (structure), NOT at the entry price. Here the swing sits just above entry, so the
    # structure stop even locks a small profit instead of pinning to break-even.
    p = _pos(direction="long", stop=4445.0)  # entry 4449
    ctx = {"atr": 2.0, "last": 4459.0, "regime": "moderate", "swing_low": 4452.0}  # +1.0R
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0)
    assert d is not None and d["kind"] == "structure" and "structure" in d["reason"]
    assert abs(d["stop"] - (4452.0 - 0.2 * 2.0)) < 0.01  # swing 4452 - 0.2*ATR(2) = 4451.6


def test_protect_falls_back_to_breakeven_without_swing():
    # Same +1R, but no swing in context -> fall back to a plain breakeven at entry (never leave a
    # winner unprotected).
    p = _pos(direction="long", stop=4445.0)  # entry 4449
    ctx = {"atr": 2.0, "last": 4459.0, "regime": "moderate"}  # +1.0R, no swing_low
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0)
    assert d is not None and d["kind"] == "breakeven" and abs(d["stop"] - 4449.0) < 0.01


# --- partial profit-taking (scale out) ---

def test_auto_decision_scales_out_at_milestone():
    p = _pos(direction="long", stop=4445.0)  # entry 4449
    ctx = {"atr": 2.0, "last": 4470.0, "regime": "trending"}  # +2.1R
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0)
    assert d is not None and d["action"] == "take_partial" and d["fraction"] == 0.5


def test_auto_decision_no_double_scale():
    p = _pos(direction="long", stop=4445.0)
    ctx = {"atr": 2.0, "last": 4470.0, "regime": "trending"}
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0, already_scaled=True)
    assert d is None or d["action"] != "take_partial"  # already scaled -> manage the rest instead


def test_sim_close_partial_reduces_position():
    from app.brokers.sim import SimPaperBroker
    from app.models.enums import AssetClass, OrderSide, OrderType
    from app.models.schemas import OrderRequest

    b = SimPaperBroker()
    b.submit_order(OrderRequest(symbol="EURUSD", asset_class=AssetClass.FOREX, side=OrderSide.BUY,
                                order_type=OrderType.MARKET, qty=1.0))
    res = b.close_partial("EURUSD", 0.5)
    assert res.status.value not in ("error", "rejected")
    pos = b.get_open_positions()
    assert len(pos) == 1 and abs(pos[0].qty - 0.5) < 1e-6


def test_auto_execute_takes_partial_and_moves_to_breakeven(db_session, monkeypatch):
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="long", stop=4445.0)])
    monkeypatch.setattr(advisor, "_plan_risk", lambda *a, **k: 10.0)
    _patch_exec(monkeypatch, broker)
    ctx = {"XAUUSDm": {"atr": 2.0, "last": 4470.0, "regime": "trending"}}  # +2.1R
    actions = advisor._auto_execute(db_session, [_adv(symbol="XAUUSDm", thesis="intact")], ctx)
    assert broker.partials == [("XAUUSDm", 0.5)]
    assert any(x["kind"] == "partial" and x["ok"] for x in actions)
    # de-risk: the runner's stop is moved to breakeven (entry 4449).
    assert any(s[0] == "XAUUSDm" and abs(s[1] - 4449.0) < 0.01 for s in broker.sltp)


def test_auto_execute_partial_does_not_loosen_profitable_stop(db_session, monkeypatch):
    """If the runner's stop is already locked in profit, the post-partial breakeven move must NOT
    loosen it back to entry."""
    broker = _Broker(is_paper=True)
    # long, stop 4460 ABOVE entry 4449 -> already in profit; breakeven would loosen it.
    monkeypatch.setattr(advisor, "live_broker_positions",
                        lambda session: [_pos(direction="long", stop=4460.0)])
    monkeypatch.setattr(advisor, "_plan_risk", lambda *a, **k: 10.0)
    _patch_exec(monkeypatch, broker)
    ctx = {"XAUUSDm": {"atr": 2.0, "last": 4470.0, "regime": "trending"}}  # +2.1R -> take_partial
    advisor._auto_execute(db_session, [_adv(symbol="XAUUSDm", thesis="intact")], ctx)
    assert broker.partials == [("XAUUSDm", 0.5)]                       # partial still taken
    assert not any(abs(s[1] - 4449.0) < 0.01 for s in broker.sltp)     # but stop NOT loosened to entry


def test_auto_execute_partial_skips_when_too_small(db_session, monkeypatch):
    """A min-lot position can't be split 50%; the advisor must skip gracefully (not spam a failed
    take_partial every tick) and mark it so it manages the whole position instead."""
    class _SmallBroker(_Broker):
        def close_partial(self, symbol, fraction):
            self.partials.append((symbol, fraction))
            return _Result("rejected", error="position too small to partial-close (min lot)")

    broker = _SmallBroker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions",
                        lambda session: [_pos(direction="long", stop=4445.0)])
    monkeypatch.setattr(advisor, "_plan_risk", lambda *a, **k: 10.0)
    _patch_exec(monkeypatch, broker)
    ctx = {"XAUUSDm": {"atr": 2.0, "last": 4470.0, "regime": "trending"}}  # +2.1R -> take_partial
    actions = advisor._auto_execute(db_session, [_adv(symbol="XAUUSDm", thesis="intact")], ctx)
    assert broker.partials == [("XAUUSDm", 0.5)]          # attempted once
    assert "XAUUSDm" in advisor._PARTIAL_DONE             # marked -> won't retry the partial
    assert any(x["action"] == "partial_skipped" and x["ok"] for x in actions)  # benign, not a ✗


def test_auto_execute_defers_stop_when_market_closed(db_session, monkeypatch):
    """A stop-modify rejected for a benign/temporary reason (market closed) is recorded as a
    'deferred' action, not a red ✗ — the original broker-side stop still protects the trade."""
    class _ClosedBroker(_Broker):
        def set_sl_tp(self, symbol, sl, tp):
            self.sltp.append((symbol, sl, tp))
            return _Result("rejected", error="market closed")

    broker = _ClosedBroker(is_paper=True)
    # short, +1R, stop above entry (worse side) -> the advisor decides "move to breakeven".
    monkeypatch.setattr(advisor, "live_broker_positions",
                        lambda session: [_pos(direction="short", pnl=10.0, stop=4460.0)])
    monkeypatch.setattr(advisor, "_plan_risk", lambda *a, **k: 10.0)
    _patch_exec(monkeypatch, broker)
    ctx = {"XAUUSDm": {"atr": 5.0, "last": 4439.0, "regime": "moderate"}}
    actions = advisor._auto_execute(db_session, [_adv(thesis="intact")], ctx)
    deferred = [a for a in actions if a["action"] == "stop_deferred"]
    assert deferred, f"expected a deferred action, got {[a['action'] for a in actions]}"
    assert "market closed" in deferred[0]["reason"]


def test_auto_decision_lets_winner_run_in_strong_trend():
    """At ~2R in a strong, intact trend the advisor lets the winner RUN (drop the target, trail)
    instead of capping at the planned target."""
    p = _pos(direction="short", stop=4460.0)  # entry 4449, take_profit 4397 still set
    ctx = {"atr": 5.0, "last": 4429.0, "regime": "trending"}  # profit 20 / plan 10 = +2.0R
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0, already_scaled=True)
    assert d is not None and d["action"] == "run_target"
    assert d["stop"] < 4460.0  # trail is tighter than the current stop (never loosens)


def test_auto_decision_no_run_when_trend_not_strong():
    """Same +2R but a ranging/volatile regime -> do NOT remove the target; just trail/bank."""
    p = _pos(direction="short", stop=4460.0)
    ctx = {"atr": 5.0, "last": 4429.0, "regime": "ranging"}
    d = advisor._auto_decision(_adv(thesis="intact"), p, ctx, 10.0, already_scaled=True)
    assert d is None or d["action"] != "run_target"


def test_auto_execute_run_target_clears_take_profit(db_session, monkeypatch):
    """Executing 'run_target' clears the broker TP (0.0) AND the app-tracked DB TP, so neither
    caps the trade — the trailing stop becomes the exit."""
    from sqlalchemy import select
    from app.models.db import Position
    from app.models.enums import PositionStatus

    db_session.add(Position(symbol="XAUUSDm", asset_class="metal", direction="short", qty=0.01,
                            entry_price=4449.0, stop_loss=4460.0, take_profit=4397.0,
                            status=PositionStatus.OPEN.value, last_price=4429.0))
    db_session.commit()

    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions",
                        lambda session: [_pos(direction="short", stop=4460.0)])
    monkeypatch.setattr(advisor, "_plan_risk", lambda *a, **k: 10.0)
    monkeypatch.setattr(advisor, "_already_scaled", lambda *a, **k: True)  # past the partial step
    _patch_exec(monkeypatch, broker)
    ctx = {"XAUUSDm": {"atr": 5.0, "last": 4429.0, "regime": "trending"}}
    actions = advisor._auto_execute(db_session, [_adv(thesis="intact")], ctx)

    runs = [a for a in actions if a["action"] == "run_target"]
    assert runs and runs[0]["ok"]
    assert any(s[0] == "XAUUSDm" and s[2] == 0.0 for s in broker.sltp)  # broker TP cleared
    row = db_session.scalars(select(Position).where(Position.symbol == "XAUUSDm")).first()
    assert row.take_profit is None  # DB TP cleared (Monitor won't close at the old target)


def test_already_scaled_derived_from_remaining_size(db_session):
    """A position whose live size is materially below the planned size is treated as already
    scaled — correct across restarts when the in-memory _PARTIAL_DONE set is empty."""
    from app.models.db import TradeProposalRecord
    from app.models.enums import ProposalStatus

    db_session.add(TradeProposalRecord(
        symbol="XAUUSDm", asset_class="metal", timeframe="1h", direction="short",
        entry=4449.0, stop_loss=4470.0, take_profit=4400.0, confidence=0.7, rationale="t",
        reasoning={}, status=ProposalStatus.EXECUTED.value, risk_decision="approved",
        approved_qty=1.0, risk_amount=21.0,
    ))
    db_session.commit()
    assert advisor._already_scaled(db_session, "XAUUSDm", 1.0, "short") is False  # full size
    assert advisor._already_scaled(db_session, "XAUUSDm", 0.5, "short") is True   # half -> scaled


def test_already_scaled_falls_back_to_partial_done_set(db_session):
    """With no plan on record, fall back to the in-memory set (same-process fast path)."""
    assert advisor._already_scaled(db_session, "EURUSDm", 1.0, "long") is False
    advisor._PARTIAL_DONE.add("EURUSDm")
    assert advisor._already_scaled(db_session, "EURUSDm", 1.0, "long") is True


def _patch_exec(monkeypatch, broker, *, kill=False, live_ok=True):
    import app.brokers.registry as reg
    import app.core.state as state
    monkeypatch.setattr(state, "kill_switch_active", lambda session: kill)
    monkeypatch.setattr(state, "live_execution_allowed", lambda settings: live_ok)
    monkeypatch.setattr(state, "get_or_create_settings",
                        lambda session: type("S", (), {"broker_map": {}})())
    monkeypatch.setattr(reg, "get_broker_for", lambda ac, bm: broker)


def test_auto_execute_closes_on_paper(db_session, monkeypatch):
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "_CLOSE_CONFIRM", 1)  # close on first invalidation for this test
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker)
    actions = advisor._auto_execute(db_session, [_adv(thesis="invalidated", sev="danger")])
    assert broker.closed == ["XAUUSDm"]
    assert actions[0]["action"] == "close" and actions[0]["ok"] is True


def test_close_requires_hysteresis(db_session, monkeypatch):
    # Default _CLOSE_CONFIRM=2: first invalidation is pending, second confirms the close.
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker)
    first = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert broker.closed == [] and first[0]["action"] == "close_pending"
    second = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert broker.closed == ["XAUUSDm"] and second[0]["ok"] is True


def test_auto_execute_halts_on_kill_switch(db_session, monkeypatch):
    broker = _Broker(is_paper=True)
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker, kill=True)
    actions = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert actions == [] and broker.closed == []


def test_auto_execute_blocks_unconfirmed_live(db_session, monkeypatch):
    broker = _Broker(is_paper=False)
    monkeypatch.setattr(advisor, "_CLOSE_CONFIRM", 1)  # pass hysteresis so we reach the live gate
    monkeypatch.setattr(advisor, "live_broker_positions", lambda session: [_pos(direction="short")])
    _patch_exec(monkeypatch, broker, live_ok=False)
    actions = advisor._auto_execute(db_session, [_adv(thesis="invalidated")])
    assert broker.closed == [] and actions[0]["action"] == "blocked_live_unconfirmed"


def test_run_advisor_skips_execution_when_toggle_off(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "_advise_with_context", lambda session: ([_adv(thesis="invalidated")], {}))
    called = {"n": 0}
    monkeypatch.setattr(advisor, "_auto_execute", lambda s, a, c=None: called.__setitem__("n", called["n"] + 1) or [])
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = False
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert out["actions"] == [] and called["n"] == 0


def test_advisor_activity_flattens_actions(db_session):
    from app.api.settings_routes import advisor_activity
    from app.models.db import AgentRun

    db_session.add(AgentRun(agent="advisor", event="check", detail={"actions": [
        {"symbol": "XAUUSDm", "action": "set_stop", "kind": "breakeven", "ok": True, "reason": "+1R"},
        {"symbol": "BTCUSDm", "action": "close", "kind": "close", "ok": True, "reason": "invalidated"},
    ]}))
    db_session.commit()
    items = advisor_activity(limit=30, session=db_session)
    assert len(items) == 2
    assert items[0].symbol == "XAUUSDm" and items[0].kind == "breakeven" and items[0].ok is True
    assert {i.symbol for i in items} == {"XAUUSDm", "BTCUSDm"}


def test_reenter_opens_when_engine_and_risk_approve(db_session, monkeypatch):
    import app.agents.pipeline as pipeline
    import app.execution.executor as executor
    from app.models.db import TradeProposalRecord
    from app.models.enums import AssetClass, Direction, ProposalStatus, RiskDecisionType
    from app.models.schemas import AnalyzeResponse, RiskDecision, TradeProposal

    rec = TradeProposalRecord(symbol="EURUSDm", asset_class="forex", timeframe="1h", direction="long",
                              entry=1.16, stop_loss=1.155, take_profit=1.17, confidence=0.6,
                              rationale="x", status=ProposalStatus.PENDING_APPROVAL.value)
    db_session.add(rec)
    db_session.commit()

    prop = TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction=Direction.LONG,
                         entry=1.16, stop_loss=1.155, take_profit=1.17, confidence=0.6)
    risk = RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="EURUSDm")
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        lambda s, sym, ac, tf, use_llm=False:
                        AnalyzeResponse(proposal_id=rec.id, status="pending_approval", proposal=prop, risk=risk))
    monkeypatch.setattr(executor, "execute_proposal",
                        lambda session, record: setattr(record, "status", ProposalStatus.EXECUTED.value))

    out = advisor._maybe_reenter(db_session, "EURUSDm", "forex")
    assert out["action"] == "reenter" and out["ok"] is True and "opened long" in out["reason"].lower()


def test_reenter_skips_when_no_fresh_setup(db_session, monkeypatch):
    import app.agents.pipeline as pipeline
    from app.models.enums import AssetClass, Direction, RiskDecisionType
    from app.models.schemas import AnalyzeResponse, RiskDecision, TradeProposal

    prop = TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction=Direction.NO_TRADE)
    risk = RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="no", symbol="EURUSDm")
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        lambda *a, **k: AnalyzeResponse(proposal_id=0, status="x", proposal=prop, risk=risk))
    out = advisor._maybe_reenter(db_session, "EURUSDm", "forex")
    assert out["action"] == "reenter_skip" and out["ok"] is False


def test_run_advisor_reenters_after_close_when_enabled(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "_advise_with_context", lambda s: ([], {}))
    monkeypatch.setattr(advisor, "_auto_execute",
                        lambda s, a, c=None: [{"symbol": "EURUSDm", "action": "close", "ok": True, "asset_class": "forex"}])
    monkeypatch.setattr(advisor, "_maybe_reenter",
                        lambda s, sym, ac: {"symbol": sym, "action": "reenter", "kind": "open", "ok": True, "reason": "opened long"})
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = True
    cfg.auto_reenter = True
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert any(x["action"] == "reenter" for x in out["actions"])


def test_run_advisor_no_reenter_when_toggle_off(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "_advise_with_context", lambda s: ([], {}))
    monkeypatch.setattr(advisor, "_auto_execute",
                        lambda s, a, c=None: [{"symbol": "EURUSDm", "action": "close", "ok": True, "asset_class": "forex"}])
    monkeypatch.setattr(advisor, "_maybe_reenter",
                        lambda s, sym, ac: (_ for _ in ()).throw(AssertionError("should not re-enter")))
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = True
    cfg.auto_reenter = False
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert not any(x["action"] == "reenter" for x in out["actions"])


def test_run_advisor_executes_when_toggle_on(db_session, monkeypatch):
    monkeypatch.setattr(advisor, "_advise_with_context", lambda session: ([_adv(thesis="invalidated")], {}))
    monkeypatch.setattr(advisor, "_auto_execute",
                        lambda s, a, c=None: [{"symbol": "XAUUSDm", "action": "close", "ok": True}])
    cfg = advisor.get_or_create_advisor_config(db_session)
    cfg.auto_execute = True
    db_session.commit()
    out = advisor.run_advisor(db_session)
    assert out["actions"][0]["action"] == "close"
