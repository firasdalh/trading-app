"""Unit tests for the deterministic Risk Manager.

This is the most dangerous module in the system, so coverage is intentionally exhaustive:
sizing math, the risk-per-trade ceiling, and every veto/resize path from RISK.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.enums import AssetClass, Direction, OrderSide, RiskDecisionType
from app.models.schemas import AccountState, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal, size_position

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def make_account(**kw) -> AccountState:
    base = dict(equity=100_000.0, cash=100_000.0, open_positions=0,
                total_risk_amount=0.0, daily_realized_pnl=0.0, trading_paused=False)
    base.update(kw)
    return AccountState(**base)


def make_limits(**kw) -> RiskLimits:
    base = dict(risk_per_trade=0.01, max_open_positions=3, max_daily_loss=0.03,
                max_total_exposure=0.06, per_pair_cooldown_minutes=30, risk_per_trade_ceiling=0.02)
    base.update(kw)
    return RiskLimits(**base)


def make_proposal(direction=Direction.LONG, entry=100.0, stop=95.0, tp=110.0, **kw) -> TradeProposal:
    return TradeProposal(
        symbol=kw.get("symbol", "AAPL"),
        asset_class=kw.get("asset_class", AssetClass.STOCK),
        direction=direction, entry=entry, stop_loss=stop, take_profit=tp,
        confidence=kw.get("confidence", 0.7),
    )


# ---------------------------------------------------------------- sizing ----


def test_size_position_basic():
    qty, risk = size_position(equity=100_000, risk_fraction=0.01, entry=100, stop_loss=95)
    assert qty == 200.0          # 1000 risk / 5 stop distance
    assert risk == 1000.0


def test_size_position_floors_to_step():
    qty, risk = size_position(equity=100_000, risk_fraction=0.01, entry=100, stop_loss=97, qty_step=1)
    # 1000 / 3 = 333.33 -> floored to 333 whole units
    assert qty == 333.0
    assert risk == pytest.approx(999.0)


def test_size_position_zero_stop_distance():
    qty, risk = size_position(equity=100_000, risk_fraction=0.01, entry=100, stop_loss=100)
    assert qty == 0.0 and risk == 0.0


# ------------------------------------------------------------- approvals ----


def test_happy_path_long_approved():
    d = evaluate_proposal(make_proposal(), make_account(), make_limits(), now=NOW, qty_step=1)
    assert d.approved is True
    assert d.decision == RiskDecisionType.APPROVED
    assert d.side == OrderSide.BUY
    assert d.approved_qty == 200.0
    assert d.risk_amount == 1000.0
    assert d.risk_pct_of_equity == pytest.approx(0.01)
    assert all(d.checks.values())


def test_short_sets_sell_side():
    p = make_proposal(direction=Direction.SHORT, entry=100, stop=105, tp=90)
    d = evaluate_proposal(p, make_account(), make_limits(), now=NOW, qty_step=1)
    assert d.approved and d.side == OrderSide.SELL


def test_risk_per_trade_ceiling_is_enforced():
    # Stored risk_per_trade absurdly high; ceiling must clamp it to 2%.
    limits = make_limits(risk_per_trade=0.50, risk_per_trade_ceiling=0.02)
    d = evaluate_proposal(make_proposal(), make_account(), limits, now=NOW, qty_step=1)
    assert d.risk_amount == 2000.0   # 2% of 100k, NOT 50%
    assert d.approved_qty == 400.0


def test_size_position_sizes_by_account_ccy_risk_per_lot():
    from app.risk.manager import size_position
    # $1000 equity, 3% budget = $30. With a currency-correct $20 risk-per-lot -> 1.5 lots, $30 risk.
    lots, risk = size_position(equity=1000.0, risk_fraction=0.03, entry=100.0, stop_loss=99.0,
                               risk_per_lot=20.0)
    assert lots == 1.5 and risk == 30.0


def test_size_position_falls_back_to_price_distance():
    from app.risk.manager import size_position
    # No risk_per_lot -> legacy |entry-stop| basis (correct for the USD-quoted / sim path).
    lots, risk = size_position(equity=1000.0, risk_fraction=0.03, entry=100.0, stop_loss=99.0)
    assert lots == 30.0 and risk == 30.0


# ----------------------------------------------------------------- vetoes ----


def test_no_trade_is_vetoed():
    p = make_proposal(direction=Direction.NO_TRADE, entry=None, stop=None, tp=None)
    d = evaluate_proposal(p, make_account(), make_limits(), now=NOW)
    assert not d.approved and d.decision == RiskDecisionType.VETOED
    assert "NO_TRADE" in d.reason


def test_missing_stop_vetoed():
    p = make_proposal()
    p.stop_loss = None
    d = evaluate_proposal(p, make_account(), make_limits(), now=NOW)
    assert not d.approved


def test_wrong_side_stop_long_vetoed():
    p = make_proposal(direction=Direction.LONG, entry=100, stop=105)  # stop above entry on a long
    d = evaluate_proposal(p, make_account(), make_limits(), now=NOW)
    assert not d.approved and "wrong side" in d.reason


def test_wrong_side_take_profit_vetoed():
    p = make_proposal(direction=Direction.LONG, entry=100, stop=95, tp=90)  # tp below entry on a long
    d = evaluate_proposal(p, make_account(), make_limits(), now=NOW)
    assert not d.approved and "take-profit" in d.reason


def test_daily_loss_breach_vetoed():
    acct = make_account(daily_realized_pnl=-3100.0)  # > 3% of 100k
    d = evaluate_proposal(make_proposal(), acct, make_limits(), now=NOW)
    assert not d.approved and "daily loss" in d.reason


def test_trading_paused_vetoed():
    acct = make_account(trading_paused=True)
    d = evaluate_proposal(make_proposal(), acct, make_limits(), now=NOW)
    assert not d.approved


def test_max_open_positions_vetoed():
    acct = make_account(open_positions=3)
    d = evaluate_proposal(make_proposal(), acct, make_limits(), now=NOW)
    assert not d.approved and "max open positions" in d.reason


def test_cooldown_active_vetoed():
    last_close = NOW - timedelta(minutes=10)  # only 10 of 30 min elapsed
    d = evaluate_proposal(make_proposal(), make_account(), make_limits(),
                          now=NOW, last_pair_close_at=last_close)
    assert not d.approved and "cooldown" in d.reason


def test_cooldown_elapsed_approved():
    last_close = NOW - timedelta(minutes=31)
    d = evaluate_proposal(make_proposal(), make_account(), make_limits(),
                          now=NOW, last_pair_close_at=last_close, qty_step=1)
    assert d.approved


def test_equity_nonpositive_vetoed():
    acct = make_account(equity=0.0)
    d = evaluate_proposal(make_proposal(), acct, make_limits(), now=NOW)
    assert not d.approved


# ----------------------------------------------------------- exposure ----


def test_exposure_forces_resize():
    # Budget 6% of 100k = 6000; 5500 already at risk -> only 500 remaining.
    acct = make_account(open_positions=1, total_risk_amount=5500.0)
    d = evaluate_proposal(make_proposal(), acct, make_limits(), now=NOW, qty_step=1)
    assert d.approved
    assert d.decision == RiskDecisionType.RESIZED
    assert d.risk_amount <= 500.0
    assert d.approved_qty == 100.0   # 500 budget / 5 stop distance


def test_exposure_exhausted_vetoed():
    acct = make_account(open_positions=2, total_risk_amount=6000.0)  # budget fully used
    d = evaluate_proposal(make_proposal(), acct, make_limits(), now=NOW)
    assert not d.approved and "exposure" in d.reason


def test_stop_too_wide_zero_size_vetoed():
    # Stop so far that even full risk budget buys < 1 whole unit, with whole-unit step.
    p = make_proposal(entry=100.0, stop=0.01, tp=200.0)
    acct = make_account(equity=100.0)  # 1% = $1 risk, ~$1/99.99 distance < 1 unit
    d = evaluate_proposal(p, acct, make_limits(), now=NOW, qty_step=1)
    assert not d.approved


def test_min_qty_vetoed():
    d = evaluate_proposal(make_proposal(), make_account(), make_limits(),
                          now=NOW, qty_step=1, min_qty=1000.0)
    assert not d.approved and "below minimum" in d.reason


def test_vetoes_when_broker_not_tradeable():
    """A setup the broker won't let us OPEN (instrument disabled / close-only / wrong side) is
    vetoed up front, not 'approved' then bounced at order time (e.g. Exness disables India 50)."""
    dec = evaluate_proposal(make_proposal(), make_account(), make_limits(), now=NOW,
                            not_tradeable_reason="broker has this instrument disabled")
    assert not dec.approved and dec.decision == RiskDecisionType.VETOED
    assert "not tradeable" in dec.reason.lower()
