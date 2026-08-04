"""Two safety guards on the armed/conditional system.

1. MARKET HOURS — a closed session still returns the last tick, so every price test would run on a
   stale quote and an arm could "trigger" on hours-old data, then try to open into the reopen gap.
   Every other engine already skips a closed market; this one didn't. It WAITS (never cancels) —
   the setup is still valid, it just can't be judged until the session reopens.

2. MAX DRIFT — size is derived from |trigger - stop|, but a breakout arm waits for closes BEYOND the
   trigger and then fills at MARKET. That overshoot is added to the real risk while the size stays as
   budgeted (trigger 100 / stop 99 filling at 100.4 = a 1.4-point loss on a position sized for 1.0).
   Past 0.25R of overshoot the arm refuses: don't chase your own breakout.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.conditional import (
    _MAX_DRIFT_R,
    _drift_too_far,
    active_armed,
    arm_conditional,
    check_conditional_setups,
)
from app.models.db import ConditionalSetup
from app.models.enums import ConditionalStatus

NOW = datetime.now(timezone.utc)


def _setup(**kw) -> ConditionalSetup:
    base = dict(symbol="DE30m", asset_class="index", timeframe="1h", direction="long",
                order_type="buy_stop", trigger_price=100.0, stop_loss=99.0, take_profit=105.0,
                confidence=0.7, status=ConditionalStatus.ARMED.value, source="manual")
    base.update(kw)
    return ConditionalSetup(**base)


# --- 2. drift guard (pure function, no DB) -------------------------------------------------------

def test_no_drift_at_the_trigger():
    assert _drift_too_far(_setup(), 100.0) is None


def test_small_drift_is_tolerated():
    """Planned R = 1.0; 0.2 past the trigger = 0.20R, inside the 0.25R allowance."""
    assert _drift_too_far(_setup(), 100.2) is None


def test_large_drift_is_refused():
    d = _drift_too_far(_setup(), 100.5)          # 0.50R past -> real risk 1.5x planned
    assert d is not None
    assert "not chasing" in d and "50%" in d


def test_drift_boundary_is_inclusive():
    assert _drift_too_far(_setup(), 100.0 + _MAX_DRIFT_R) is None          # exactly at the cap
    assert _drift_too_far(_setup(), 100.0 + _MAX_DRIFT_R + 0.01) is not None


def test_short_breakout_drift_uses_the_other_side():
    s = _setup(direction="short", order_type="sell_stop", trigger_price=100.0, stop_loss=101.0,
               take_profit=95.0)
    assert _drift_too_far(s, 99.9) is None        # 0.1R past -> fine
    assert _drift_too_far(s, 99.4) is not None    # 0.6R past -> refuse


def test_price_better_than_the_trigger_is_never_refused():
    """Below the trigger on a buy_stop is a BETTER entry — drift only ever measures overshoot."""
    assert _drift_too_far(_setup(), 99.8) is None


def test_limit_orders_are_exempt():
    """A limit fills on the favourable side of its trigger, so its drift only reduces risk."""
    s = _setup(order_type="buy_limit", direction="long", trigger_price=100.0, stop_loss=99.0)
    assert _drift_too_far(s, 90.0) is None


def test_drift_needs_a_stop_to_measure_against():
    assert _drift_too_far(_setup(stop_loss=None), 100.5) is None


# --- 1. market hours ------------------------------------------------------------------------------

class _Broker:
    """Minimal broker stand-in; ``open_`` drives the market-hours answer."""

    def __init__(self, open_: bool, price: float = 100.0):
        self.open_, self.price, self.quoted = open_, price, False

    def market_open(self, symbol):
        return self.open_

    def get_quote(self, symbol):
        self.quoted = True
        from app.models.schemas import Quote
        return Quote(symbol=symbol, price=self.price, ts=NOW)

    def can_open(self, symbol, direction):
        return True, None


def _armed_row(db_session, **kw):
    s = arm_conditional(
        db_session, symbol="DE30m", asset_class="index", timeframe="1h", direction="long",
        order_type="buy_stop", trigger_price=100.0, stop_loss=99.0, take_profit=105.0,
        confidence=0.7, rr=2.0, source="manual", **kw)
    assert s is not None
    return s


def _patch(monkeypatch, broker):
    from app.agents import conditional as C

    monkeypatch.setattr(C, "get_broker_for", lambda ac, bm: broker)
    monkeypatch.setattr(C, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(C, "kill_switch_active", lambda s: False)


def test_closed_market_is_not_evaluated(db_session, monkeypatch):
    """Trigger is 'hit' by the stale price, but the market is shut -> don't even quote it."""
    _armed_row(db_session)
    broker = _Broker(open_=False, price=101.0)
    _patch(monkeypatch, broker)

    out = check_conditional_setups(db_session)

    assert out["triggered"] == 0
    assert broker.quoted is False                      # skipped before the price was read
    assert active_armed(db_session)[0].status == ConditionalStatus.ARMED.value  # still waiting


def test_closed_market_does_not_cancel_the_setup(db_session, monkeypatch):
    """A shut session must never kill a valid arm — it just waits for the reopen."""
    _armed_row(db_session)
    _patch(monkeypatch, _Broker(open_=False))
    check_conditional_setups(db_session)
    assert len(active_armed(db_session)) == 1


def test_open_market_is_evaluated(db_session, monkeypatch):
    """The guard is narrow: an OPEN market still reads the price as before."""
    _armed_row(db_session)
    broker = _Broker(open_=True, price=99.0)           # below trigger -> no fire, but quoted
    _patch(monkeypatch, broker)
    check_conditional_setups(db_session)
    assert broker.quoted is True


def test_market_hours_failure_falls_back_to_evaluating(db_session, monkeypatch):
    """If we can't tell whether the market is open, keep the old behaviour rather than freezing."""
    class _Broken(_Broker):
        def market_open(self, symbol):
            raise RuntimeError("no session info")

    _armed_row(db_session)
    broker = _Broken(open_=True, price=99.0)
    _patch(monkeypatch, broker)
    check_conditional_setups(db_session)
    assert broker.quoted is True
