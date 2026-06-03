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


def execute_proposal(session: Session, record: TradeProposalRecord) -> OrderResult:
    """Submit an approved proposal. Raises ExecutionBlocked if a safety gate refuses."""
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
        raise ExecutionBlocked("live execution not confirmed for this session")

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
