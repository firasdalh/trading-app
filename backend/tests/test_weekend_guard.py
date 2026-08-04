"""Weekend-gap protection — the one risk a stop-loss cannot cover.

A stop is an instruction to exit AT a price, so it only works while the market trades. Across a
closed weekend price doesn't pass through the stop, it jumps over it: UKOILm #301 was sized to lose
1R ($49) and realised -8.9R (-$434) on the Monday reopen.

Two independent guards:
  BLOCK   (on by default)  — refuse NEW entries in the hours before the Friday close
  FLATTEN (off by default) — close what's still open; opt-in because it ACTS on live positions

Crypto is exempt from both — it trades through the weekend, so there is no gap.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import AccountState, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal
from app.risk.weekend import hours_to_weekend_close, in_weekend_window, weekend_block_reason

# 2026-08-07 is a Friday; the reference close is 21:00 UTC.
FRI_1000 = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)   # 11h before
FRI_1900 = datetime(2026, 8, 7, 19, 0, tzinfo=timezone.utc)   # 2h before
FRI_2030 = datetime(2026, 8, 7, 20, 30, tzinfo=timezone.utc)  # 30m before
FRI_2200 = datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)   # after the close
SAT = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
SUN = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
MON = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)        # Monday, ~4.4 days out


# --- the clock ------------------------------------------------------------------------------

def test_hours_to_close_on_friday():
    assert hours_to_weekend_close(FRI_1900) == 2.0
    assert hours_to_weekend_close(FRI_1000) == 11.0


def test_hours_to_close_earlier_in_the_week():
    h = hours_to_weekend_close(MON)
    assert h is not None and 100 < h < 110        # Monday noon -> Friday 21:00


def test_no_window_once_the_market_is_shut():
    for t in (FRI_2200, SAT, SUN):
        assert hours_to_weekend_close(t) is None


def test_naive_datetimes_are_treated_as_utc():
    assert hours_to_weekend_close(datetime(2026, 8, 7, 19, 0)) == 2.0


# --- the window -----------------------------------------------------------------------------

def test_inside_and_outside_the_window():
    assert in_weekend_window(FRI_1900, 3.0, "energy") is True     # 2h left, 3h window
    assert in_weekend_window(FRI_1000, 3.0, "energy") is False    # 11h left


def test_crypto_is_always_exempt():
    """24/7 — there is no gap to protect against."""
    assert in_weekend_window(FRI_2030, 3.0, "crypto") is False
    assert in_weekend_window(FRI_2030, 3.0, "CRYPTO") is False


def test_zero_hours_disables_the_window():
    assert in_weekend_window(FRI_2030, 0.0, "energy") is False


def test_block_reason_states_the_time_left():
    r = weekend_block_reason(FRI_1900, 3.0, "energy")
    assert r is not None and "2.0h" in r and "weekend" in r
    assert weekend_block_reason(FRI_1000, 3.0, "energy") is None


# --- the Risk Manager veto -------------------------------------------------------------------

def _acct():
    return AccountState(equity=100_000.0, cash=100_000.0, open_positions=0,
                        total_risk_amount=0.0, daily_realized_pnl=0.0, trading_paused=False)


def _limits(**kw):
    base = dict(risk_per_trade=0.01, max_open_positions=3, max_daily_loss=0.03,
                max_total_exposure=0.06, per_pair_cooldown_minutes=30, risk_per_trade_ceiling=0.02)
    base.update(kw)
    return RiskLimits(**base)


def _prop(ac=AssetClass.ENERGY):
    return TradeProposal(symbol="UKOILm", asset_class=ac, direction=Direction.LONG,
                         entry=93.078, stop_loss=92.424, take_profit=94.26, confidence=0.7)


def test_entry_vetoed_inside_the_window():
    d = evaluate_proposal(_prop(), _acct(), _limits(), now=FRI_2030, qty_step=0.01)
    assert d.approved is False
    assert d.decision == RiskDecisionType.VETOED
    assert "weekend guard" in d.reason
    assert d.checks.get("weekend_ok") is False


def test_entry_allowed_outside_the_window():
    d = evaluate_proposal(_prop(), _acct(), _limits(), now=FRI_1000, qty_step=0.01)
    assert d.approved is True
    assert d.checks.get("weekend_ok") is True


def test_crypto_entry_allowed_inside_the_window():
    d = evaluate_proposal(_prop(AssetClass.CRYPTO), _acct(), _limits(), now=FRI_2030, qty_step=0.01)
    assert d.approved is True


def test_guard_can_be_switched_off():
    d = evaluate_proposal(_prop(), _acct(), _limits(weekend_block_enabled=False),
                          now=FRI_2030, qty_step=0.01)
    assert d.approved is True


def test_guard_is_on_by_default():
    assert RiskLimits().weekend_block_enabled is True
    assert RiskLimits().weekend_block_hours == 3.0


def test_the_real_ukoil_trade_would_have_been_blocked():
    """UKOILm #301 opened Friday 2026-07-24 20:45 UTC and lost 8.9R over the weekend."""
    opened = datetime(2026, 7, 24, 20, 45, tzinfo=timezone.utc)
    d = evaluate_proposal(_prop(), _acct(), _limits(), now=opened, qty_step=0.01)
    assert d.approved is False and "weekend guard" in d.reason


# --- flatten (opt-in) -------------------------------------------------------------------------

def test_flatten_is_off_by_default(db_session):
    from app.execution.monitor import flatten_before_weekend

    assert flatten_before_weekend(db_session) == 0


def test_flatten_closes_only_non_crypto_in_the_window(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.core.state import get_or_create_risk_config
    from app.execution import monitor as M
    from app.models.db import Position
    from app.models.enums import PositionStatus

    cfg = get_or_create_risk_config(db_session)
    cfg.weekend_flatten_enabled = True
    cfg.weekend_flatten_hours = 1.0
    db_session.commit()

    for sym, ac in (("UKOILm", "energy"), ("BTCUSDm", "crypto")):
        db_session.add(Position(symbol=sym, asset_class=ac, direction="long", qty=0.1,
                                entry_price=100.0, stop_loss=99.0, take_profit=105.0,
                                status=PositionStatus.OPEN.value, last_price=100.0))
    db_session.commit()

    closed: list[str] = []

    def _close(sym):
        closed.append(sym)
        # Shaped like a real OrderResult — _close_position records the broker status on the exit.
        return SimpleNamespace(avg_fill_price=100.0,
                               status=SimpleNamespace(value="filled"),
                               filled_qty=0.1, raw={}, error=None, broker_order_id="T1")

    broker = SimpleNamespace(get_quote=lambda s: SimpleNamespace(price=100.0),
                             close_position=_close)
    monkeypatch.setattr(M, "get_broker_for", lambda ac, bm: broker)
    monkeypatch.setattr(M, "datetime", SimpleNamespace(now=lambda tz=None: FRI_2030))

    n = M.flatten_before_weekend(db_session)
    assert n == 1
    assert closed == ["UKOILm"]                # crypto left alone
