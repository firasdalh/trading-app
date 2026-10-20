"""Execution agent: submits risk-approved proposals via the active broker.

Hard safety rules enforced here (server-side, never bypassable):
- The kill-switch halts ALL new order submission.
- LIVE submission requires the live-confirmation gate (re-confirmed each restart).
- Every order is logged to the DB BEFORE submission (PENDING + submit_payload) and updated
  AFTER with the broker's response. Nothing executes silently.
On a fill we open a Position row (carrying the entry risk for exposure accounting) and mark
the proposal EXECUTED.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import (
    get_or_create_daily_state,
    get_or_create_settings,
    kill_switch_active,
    live_execution_allowed,
)
from app.models.db import Order, Position, TradeProposalRecord
from app.models.enums import (
    AssetClass,
    Direction,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    ProposalStatus,
)
from app.models.schemas import OrderRequest, OrderResult

log = get_logger("execution.executor")


class ExecutionBlocked(Exception):
    """Raised when execution is refused for a safety reason (kill-switch / live gate)."""


# Serialize order submission across the scheduler's concurrent jobs (hybrid / conditional /
# scanner Mode-B/C / advisor re-enter) so the anti-stacking check + fill + Position write run as
# ONE atomic step. Without this, two openers could both pass the anti-stacking check before either
# order fills and stack the same symbol. Opens are infrequent, so serializing them costs nothing.
_EXEC_LOCK = threading.RLock()

# --- stale-plan / price-drift guard ---
# A market order fills at the CURRENT price, not at the plan's entry. If price drifts against the
# plan between the moment the plan is made and the moment it executes (classically: a triggered arm
# approved minutes later, Mode A), the fill can break the plan's reward:risk AND over-size the
# position — approved_qty was sized to the PLANNED stop distance, so a wider actual stop means the
# real $ risk blows past the cap. We only re-check once drift is meaningful (so normal immediate
# fills / spread noise never false-block), then refuse rather than open a broken trade.
# (USDJPYm 2026-07-08: planned entry 162.423 / 8.5-pip stop / ~4R; approved ~12 min late, filled at
#  market 162.621 -> 28-pip stop, ~0.5R, ~3.3x the intended risk, immediately underwater.)
_EXEC_MIN_RR = 1.5          # a market fill below this R:R is a broken trade -> refuse
_DRIFT_TRIGGER = 0.10       # only re-check when adverse drift exceeds 10% of the planned stop distance
_MAX_RISK_DRIFT = 0.25      # refuse if the actual stop distance exceeds 125% of planned (over-risk)


def execute_proposal(session: Session, record: TradeProposalRecord) -> OrderResult:
    """Submit an approved proposal (serialized via _EXEC_LOCK). Raises ExecutionBlocked if a safety
    gate refuses."""
    _news_gate(record)      # before the lock: it may make one (cached, 8 s max) calendar fetch
    with _EXEC_LOCK:
        return _execute_proposal(session, record)


def _user_clicked(record: TradeProposalRecord) -> bool:
    """A trade the user placed or approved by hand (the chart ticket, an approval of a queued one)."""
    src = getattr(record, "source", None) or ""
    return src == "manual" or src.startswith("manual_from_")


def _news_gate(record: TradeProposalRecord) -> None:
    """No AUTOMATED entry inside a high-impact news window, or while the calendar can't be read.

    The analysis pipeline already stood aside around news, but the mechanical openers (RSI-Over,
    the SuperTrend auto-trader) never looked at the calendar, and every path read "calendar
    unreachable" as "no news" (full review 2026-10-20, API-4). One gate here covers every opener.
    A hand-placed trade is the user's call: the chart ticket shows the news warning instead.
    """
    if _user_clicked(record):
        return
    from app.agents.fundamental import run_fundamental
    from app.agents.orchestrator import _now_in_stand_aside

    now = datetime.now(timezone.utc)
    try:
        fund = run_fundamental(record.symbol, now=now, use_llm=False, asset_class=record.asset_class)
    except Exception as exc:  # noqa: BLE001 - unknown news is not "no news"
        raise ExecutionBlocked(f"can't check the news calendar ({exc}) - automated entry not opened") from exc
    if fund.calendar_unavailable:
        raise ExecutionBlocked("the economic calendar can't be read - automated entries stand aside until it loads")
    if _now_in_stand_aside(fund, now):
        raise ExecutionBlocked("inside a high-impact news window - automated entry not opened")


def _execute_proposal(session: Session, record: TradeProposalRecord) -> OrderResult:
    if record.approved_qty is None or record.approved_qty <= 0:
        raise ExecutionBlocked("no risk-approved quantity to execute")
    if record.direction not in (Direction.LONG.value, Direction.SHORT.value):
        raise ExecutionBlocked("proposal is not actionable (no direction)")

    settings = get_or_create_settings(session)

    # --- safety gates ---
    if kill_switch_active(session):
        raise ExecutionBlocked("kill-switch active — order submission halted")

    asset_class = AssetClass(record.asset_class)
    broker = get_broker_for(asset_class, settings.broker_map)
    if not broker.is_paper and not live_execution_allowed(settings):
        raise ExecutionBlocked(
            "REAL-money account connected but live trading is not confirmed for this session — "
            "switch the broker environment to LIVE (typed confirmation; again after every restart)")

    # --- anti-stacking: never pile a second same-direction trade into one symbol ---
    # (Final gate at the moment of opening — catches Mode-A approvals of multiple pending
    # proposals and Mode-B auto-execs that the analyze-time check couldn't see yet.)
    from app.risk.service import broker_has_open_same_direction

    try:
        stacked = broker_has_open_same_direction(session, record.symbol, record.direction)
    except Exception as exc:  # noqa: BLE001 - an unreadable book is not an empty one
        raise ExecutionBlocked(
            f"can't read the broker's open positions ({exc}) — not opening blind") from exc
    if stacked:
        raise ExecutionBlocked(
            f"already have an open {record.direction} position in {record.symbol} — not stacking"
        )

    # --- stale-plan / price-drift guard (see constants above) ---
    _guard_price_drift(broker, record)

    # --- the Risk Manager, again, HERE: inside the lock, at the live fill price ---
    # On a FRESH session: the caller's session (a Hybrid tick, an arm firing) caches the settings,
    # risk config and daily state it read when the tick started, so a kill switch, a daily-loss pause
    # or a lowered limit written meanwhile by another session was not seen (full review 2026-10-20).
    from app.core import database as _db

    with _db.SessionLocal() as fresh:
        if kill_switch_active(fresh):
            raise ExecutionBlocked("kill-switch active — order submission halted")
        _final_risk_gate(fresh, broker, record)

    side = OrderSide.BUY if record.direction == Direction.LONG.value else OrderSide.SELL
    req = OrderRequest(
        symbol=record.symbol,
        asset_class=asset_class,
        side=side,
        order_type=OrderType.MARKET,
        qty=record.approved_qty,
        stop_loss=record.stop_loss,
        take_profit=record.take_profit,
    )

    # --- log BEFORE submission ---
    order = Order(
        proposal_id=record.id,
        symbol=record.symbol,
        asset_class=record.asset_class,
        side=side.value,
        order_type=OrderType.MARKET.value,
        qty=record.approved_qty,
        stop_loss=record.stop_loss,
        take_profit=record.take_profit,
        broker=broker.name,
        broker_env=settings.broker_env,
        status=OrderStatus.PENDING.value,
        submit_payload=req.model_dump(mode="json"),
    )
    session.add(order)
    session.commit()
    log.info("order created (pre-submit)", extra={"order_id": order.id, "symbol": record.symbol,
                                                   "side": side.value, "qty": record.approved_qty})

    # --- submit ---
    result = broker.submit_order(req)

    # --- log AFTER submission ---
    order.broker_order_id = result.broker_order_id
    order.status = result.status.value
    order.filled_qty = result.filled_qty
    order.avg_fill_price = result.avg_fill_price
    order.broker_response = result.raw
    order.error = result.error
    session.add(order)

    if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
        entry_price = result.avg_fill_price or record.entry or 0.0
        position = Position(
            symbol=record.symbol,
            asset_class=record.asset_class,
            direction=record.direction,
            qty=result.filled_qty or record.approved_qty,
            entry_price=entry_price,
            stop_loss=record.stop_loss,
            take_profit=record.take_profit,
            status=PositionStatus.OPEN.value,
            last_price=entry_price,
            risk_amount=record.risk_amount,
            initial_risk_amount=record.risk_amount,   # the R denominator, never modified
            # kept for confidence-vs-outcome calibration - but a manual trade has no engine score
            confidence=None if getattr(record, "source", None) == "manual" else record.confidence,
            review=({"note": (record.reasoning or {}).get("note"), "tags": (record.reasoning or {}).get("tags") or []}
                    if getattr(record, "source", None) == "manual" else None),
            source=getattr(record, "source", None),  # who opened it -> journal breakdown by source
            broker=broker.name,
            broker_env=settings.broker_env,
        )
        session.add(position)
        record.status = ProposalStatus.EXECUTED.value
        daily = get_or_create_daily_state(session)
        daily.trades_count += 1
        log.info("order filled -> position opened", extra={"order_id": order.id,
                 "symbol": record.symbol, "entry": entry_price})
    else:
        log.warning("order not filled", extra={"order_id": order.id, "status": order.status,
                                               "error": order.error})

    session.commit()
    return result


def _fill_side_price(broker, record: TradeProposalRecord) -> float | None:
    """The price a market order would actually fill at: ask for a buy, bid for a sell. Quotes are
    mid; candles (and so plan entries) are bid - a long pays the whole spread on top of its plan."""
    try:
        mid = broker.get_quote(record.symbol).price
    except Exception:  # noqa: BLE001
        return None
    if not mid or mid <= 0:
        return None
    try:
        spread = broker.spread(record.symbol) or 0.0
    except Exception:  # noqa: BLE001
        spread = 0.0
    return mid + spread / 2 if record.direction == Direction.LONG.value else mid - spread / 2


def _final_risk_gate(session: Session, broker, record: TradeProposalRecord) -> None:
    """Re-run the Risk Manager inside _EXEC_LOCK, sized at the live fill price, and only ever SHRINK.

    The earlier check (when the setup was assessed, or when a pending proposal was created) is not
    enough on its own:
      - two openers (the Hybrid's scheduled tick and a 'Run now', an arm firing, an approval) can
        each pass max positions / exposure / the daily pause before either order fills - seen in the
        DB as three Hybrid fills within 40 s (2026-07-29);
      - approving a pending proposal without a lot size skipped every check: a probe with the daily
        loss breaker tripped still opened the trade;
      - the size was derived from the PLANNED entry, but a long fills at the ask: with the 25% drift
        allowance the real loss at the stop could reach ~1.33x the approved risk.
    The fraction of equity the trade was approved at is preserved; the lots are re-derived from the
    actual fill-to-stop distance and capped at the approved lots.
    """
    from app.risk.service import assess, current_equity, proposal_from_record

    equity = current_equity(session)
    if not equity or equity <= 0:
        raise ExecutionBlocked("can't read the account equity - not opening without a fresh risk check")
    fresh = proposal_from_record(record)
    fill = _fill_side_price(broker, record)
    if fill is not None:
        fresh = fresh.model_copy(update={"entry": fill})
    fraction = (record.risk_amount or 0.0) / equity if record.risk_amount else None
    decision = assess(session, fresh, override_risk_fraction=fraction)
    if not decision.approved or not decision.approved_qty or decision.approved_qty <= 0:
        raise ExecutionBlocked(f"risk check at execution: {decision.reason}")
    if decision.approved_qty < (record.approved_qty or 0.0) - 1e-9:
        log.warning("execution resized by the final risk check",
                    extra={"symbol": record.symbol, "approved": record.approved_qty,
                           "now": decision.approved_qty, "risk": decision.risk_amount})
        record.approved_qty = decision.approved_qty
        record.risk_amount = decision.risk_amount


def _guard_price_drift(broker, record: TradeProposalRecord) -> None:
    """Refuse a market fill that has drifted too far from the plan to still be the trade we sized.

    Compares the CURRENT price to the plan's entry. Only acts once adverse drift is meaningful
    (> _DRIFT_TRIGGER of the planned stop distance), so normal immediate fills and spread noise
    never false-block. Then refuses if the reward:risk at the real fill falls below _EXEC_MIN_RR
    or the actual stop distance balloons past the plan (over-risk). Favorable drift always passes.
    A quote hiccup is non-fatal — we fall through to the prior (unchecked) behaviour rather than
    block a trade on a data glitch.
    """
    if not (record.entry and record.stop_loss and record.take_profit):
        return  # nothing to measure against (shouldn't happen for actionable proposals)
    try:
        cur = broker.get_quote(record.symbol).price
    except Exception as exc:  # noqa: BLE001 - best-effort guard; don't block on a quote hiccup
        log.warning("drift guard: quote unavailable, skipping check",
                    extra={"symbol": record.symbol, "error": str(exc)})
        return
    if not cur or cur <= 0:
        return

    is_long = record.direction == Direction.LONG.value
    planned_risk = abs(record.entry - record.stop_loss)
    if planned_risk <= 0:
        return
    adverse = (cur - record.entry) if is_long else (record.entry - cur)
    if adverse <= _DRIFT_TRIGGER * planned_risk:
        return  # at/inside plan (or better) — normal fill, nothing to guard

    risk = (cur - record.stop_loss) if is_long else (record.stop_loss - cur)
    reward = (record.take_profit - cur) if is_long else (cur - record.take_profit)
    fmt = lambda v: f"{v:.5g}"
    if risk <= 0:
        raise ExecutionBlocked(
            f"{record.symbol}: price ({fmt(cur)}) has drifted to/through the stop ({fmt(record.stop_loss)}) "
            f"since the plan (entry {fmt(record.entry)}) — the setup is stale. Re-run analysis for a fresh plan."
        )
    rr = reward / risk
    if reward <= 0 or rr < _EXEC_MIN_RR:
        raise ExecutionBlocked(
            f"{record.symbol}: price moved from the planned entry {fmt(record.entry)} to {fmt(cur)} — "
            f"reward:risk at the real fill is {rr:.1f} (need ≥ {_EXEC_MIN_RR:.1f}). The plan no longer holds; "
            f"re-run analysis for a fresh plan."
        )
    if risk > planned_risk * (1 + _MAX_RISK_DRIFT):
        raise ExecutionBlocked(
            f"{record.symbol}: price moved from the planned entry {fmt(record.entry)} to {fmt(cur)} — the real "
            f"stop distance ({fmt(risk)}) is {risk / planned_risk:.1f}x the planned ({fmt(planned_risk)}), so the "
            f"position would carry that multiple of the intended risk. Refusing; re-run analysis for a fresh plan."
        )
