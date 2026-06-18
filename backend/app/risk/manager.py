"""The deterministic Risk Manager — pure Python, NEVER an LLM.

This module is the hard boundary between an AI idea and a real order. It:
  1. validates the proposal is a sane, actionable setup,
  2. sizes the position from the risk-per-trade rule (size is DERIVED, never set directly),
  3. enforces every hard limit from RISK.md (per-trade risk + ceiling, max open positions,
     max daily loss / auto-pause, max total exposure, per-pair cooldown),
  4. returns an APPROVED (possibly RESIZED) order or a VETO with a reason.

Its verdict is final. It can only ever *shrink* a position, never grow one. There is no
"force" path and there must never be one (see RISK.md).

Everything here is a pure function of its inputs — no DB, no network, no clock except the
``now`` passed in — so it is fully unit-testable and reproducible.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.core.logging import get_logger
from app.models.enums import Direction, OrderSide, RiskDecisionType
from app.models.schemas import AccountState, RiskDecision, RiskLimits, TradeProposal

log = get_logger("risk")

# Quantities below this are treated as zero (floating-point hygiene).
_QTY_EPS = 1e-12


def _veto(symbol: str, reason: str, checks: dict[str, bool]) -> RiskDecision:
    return RiskDecision(
        decision=RiskDecisionType.VETOED,
        approved=False,
        reason=reason,
        symbol=symbol,
        approved_qty=0.0,
        risk_amount=0.0,
        risk_pct_of_equity=0.0,
        checks=checks,
    )


def _floor_to_step(qty: float, step: float | None) -> float:
    """Floor a quantity to a tradable increment so we never round risk *up*."""
    if step is None or step <= 0:
        # Default: 8 decimal places (covers fractional crypto) without rounding up.
        return math.floor(qty * 1e8) / 1e8
    return math.floor(qty / step) * step


def size_position(
    *,
    equity: float,
    risk_fraction: float,
    entry: float,
    stop_loss: float,
    qty_step: float | None = None,
    risk_per_lot: float | None = None,
) -> tuple[float, float]:
    """Return (lots, risk_amount) for a target risk fraction of equity.

    lots = (equity * risk_fraction) / risk_per_lot, floored to a tradable step.
    ``risk_per_lot`` is the ACCOUNT-CURRENCY loss of one lot over the stop (currency-correct, from
    the broker). When omitted it falls back to ``|entry - stop|`` — correct only when the quote
    currency IS the account currency (the sim/USD path); the service always supplies the real value
    for live brokers. risk_amount is recomputed from the *floored* lots so it reflects reality.
    """
    per_lot = risk_per_lot if (risk_per_lot and risk_per_lot > 0) else abs(entry - stop_loss)
    if per_lot <= 0:
        return 0.0, 0.0
    target_risk = equity * risk_fraction
    raw_qty = target_risk / per_lot
    qty = _floor_to_step(raw_qty, qty_step)
    if qty <= _QTY_EPS:
        return 0.0, 0.0
    return qty, qty * per_lot


def evaluate_proposal(
    proposal: TradeProposal,
    account: AccountState,
    limits: RiskLimits,
    *,
    now: datetime,
    last_pair_close_at: datetime | None = None,
    qty_step: float | None = None,
    min_qty: float = 0.0,
    override_risk_fraction: float | None = None,
    has_open_same_direction: bool = False,
    correlated_exposure: str | None = None,
    not_tradeable_reason: str | None = None,
    risk_per_lot: float | None = None,
) -> RiskDecision:
    """Deterministically evaluate a proposal. Returns an APPROVED/RESIZED/VETOED decision.

    ``override_risk_fraction`` lets the user size a single trade up or down (Mode A manual size).
    It is ALWAYS re-clamped to the hard per-trade ceiling here, so a manual size can never exceed
    RISK.md's 2% cap — the user can size up to the cap, never past it.
    """
    symbol = proposal.symbol
    checks: dict[str, bool] = {}

    # --- choose the risk fraction, then clamp to the hard ceiling, defensively ---
    desired = override_risk_fraction if override_risk_fraction is not None else limits.risk_per_trade
    risk_fraction = min(desired, limits.risk_per_trade_ceiling)
    if risk_fraction <= 0:
        return _veto(symbol, "risk_per_trade is non-positive", checks)

    # --- 0. orchestrator declined ---
    if proposal.direction == Direction.NO_TRADE:
        checks["actionable"] = False
        return _veto(symbol, "orchestrator returned NO_TRADE", checks)

    # --- 1. actionable + sane prices ---
    if not proposal.is_actionable or proposal.entry is None or proposal.stop_loss is None:
        checks["actionable"] = False
        return _veto(symbol, "proposal missing entry/stop or not actionable", checks)
    checks["actionable"] = True

    entry, stop = proposal.entry, proposal.stop_loss
    if entry <= 0 or stop <= 0:
        return _veto(symbol, "entry/stop must be positive", checks)

    # Stop must be on the correct side of entry for the direction.
    side = OrderSide.BUY if proposal.direction == Direction.LONG else OrderSide.SELL
    valid_stop = (proposal.direction == Direction.LONG and stop < entry) or (
        proposal.direction == Direction.SHORT and stop > entry
    )
    checks["stop_side_valid"] = valid_stop
    if not valid_stop:
        return _veto(symbol, "stop-loss is on the wrong side of entry", checks)

    # Take-profit, if given, must also be on the correct side.
    if proposal.take_profit is not None:
        tp_ok = (proposal.direction == Direction.LONG and proposal.take_profit > entry) or (
            proposal.direction == Direction.SHORT and proposal.take_profit < entry
        )
        checks["take_profit_valid"] = tp_ok
        if not tp_ok:
            return _veto(symbol, "take-profit is on the wrong side of entry", checks)

    # --- 1c. broker tradeability: refuse a setup the broker won't let us OPEN (instrument
    # disabled / close-only, or the wrong direction for a long-only/short-only symbol). Otherwise
    # we'd "approve" a trade that bounces at order time (e.g. Exness has India 50 disabled). ---
    checks["tradeable"] = not bool(not_tradeable_reason)
    if not_tradeable_reason:
        return _veto(symbol, f"not tradeable — {not_tradeable_reason}", checks)

    # --- 2. account sanity ---
    if account.equity <= 0:
        return _veto(symbol, "account equity is non-positive", checks)

    # --- 3. daily loss / pause gate ---
    # The breaker can be turned OFF (demo-account testing). When off, we skip the daily-loss
    # veto entirely so a prior pause doesn't block new test trades.
    if limits.daily_loss_breaker_enabled:
        daily_loss_limit = account.equity * limits.max_daily_loss
        daily_breached = account.trading_paused or (-account.daily_realized_pnl >= daily_loss_limit)
        checks["daily_loss_ok"] = not daily_breached
        if daily_breached:
            return _veto(symbol, "daily loss limit reached — trading paused for the day", checks)
    else:
        checks["daily_loss_ok"] = True

    # --- 4. max open positions ---
    room_for_positions = account.open_positions < limits.max_open_positions
    checks["positions_ok"] = room_for_positions
    if not room_for_positions:
        return _veto(
            symbol,
            f"max open positions reached ({account.open_positions}/{limits.max_open_positions})",
            checks,
        )

    # --- 4b. no stacking: refuse a second same-direction trade in a symbol already open ---
    # (Three BTC shorts opened in 11 min turned one wrong call into a tripled loss.)
    checks["no_stacking"] = not has_open_same_direction
    if has_open_same_direction:
        return _veto(
            symbol,
            f"already have an open {proposal.direction.value} position in {symbol} — not stacking",
            checks,
        )

    # --- 4c. correlation: don't pile a 3rd correlated bet onto one risk factor ---
    # (e.g. short EURUSD + short GBPUSD + short AUDUSD = "long USD" x3 — one big bet.)
    checks["not_correlated"] = correlated_exposure is None
    if correlated_exposure:
        return _veto(symbol, correlated_exposure, checks)

    # --- 5. per-pair cooldown ---
    if last_pair_close_at is not None:
        cooldown_until = last_pair_close_at + timedelta(minutes=limits.per_pair_cooldown_minutes)
        cooldown_ok = now >= cooldown_until
        checks["cooldown_ok"] = cooldown_ok
        if not cooldown_ok:
            mins = max(0, math.ceil((cooldown_until - now).total_seconds() / 60))
            return _veto(symbol, f"per-pair cooldown active (~{mins} min left)", checks)
    else:
        checks["cooldown_ok"] = True

    # --- 6. size from risk-per-trade ---
    qty, risk_amount = size_position(
        equity=account.equity, risk_fraction=risk_fraction,
        entry=entry, stop_loss=stop, qty_step=qty_step, risk_per_lot=risk_per_lot,
    )
    if qty <= _QTY_EPS:
        return _veto(symbol, "computed position size is zero (stop too wide for risk budget)", checks)

    # --- 7. total-exposure budget (may force a resize) ---
    exposure_budget = account.equity * limits.max_total_exposure
    remaining = exposure_budget - account.total_risk_amount
    checks["exposure_ok"] = remaining > 0
    if remaining <= 0:
        return _veto(symbol, "no exposure budget left across open positions", checks)

    decision_type = RiskDecisionType.APPROVED
    if risk_amount > remaining:
        # Shrink to fit the remaining exposure budget (per-lot risk in account currency).
        per_lot = risk_per_lot if (risk_per_lot and risk_per_lot > 0) else abs(entry - stop)
        qty = _floor_to_step(remaining / per_lot, qty_step) if per_lot > 0 else 0.0
        risk_amount = qty * per_lot
        decision_type = RiskDecisionType.RESIZED
        if qty <= _QTY_EPS:
            return _veto(symbol, "exposure budget too small to size any position", checks)

    # --- 8. minimum tradable size ---
    if qty < min_qty:
        checks["min_qty_ok"] = False
        return _veto(symbol, f"position size {qty} below minimum {min_qty}", checks)
    checks["min_qty_ok"] = True

    risk_pct = risk_amount / account.equity
    reason = (
        "approved at full risk-per-trade size"
        if decision_type == RiskDecisionType.APPROVED
        else "approved but RESIZED to fit remaining exposure budget"
    )
    log.info(
        "risk decision",
        extra={"symbol": symbol, "decision": decision_type.value, "qty": qty,
               "risk_amount": round(risk_amount, 2), "risk_pct": round(risk_pct, 4)},
    )
    return RiskDecision(
        decision=decision_type,
        approved=True,
        reason=reason,
        symbol=symbol,
        side=side,
        approved_qty=qty,
        risk_amount=round(risk_amount, 2),
        risk_pct_of_equity=round(risk_pct, 6),
        checks=checks,
    )
