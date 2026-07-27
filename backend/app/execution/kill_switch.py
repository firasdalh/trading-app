"""Kill-switch flatten: close every open position across configured brokers.

The kill-switch (UI button / env flag) halts new orders everywhere. Flattening is the
optional "and close what's open" action. It works even mid-cycle and books realized P&L /
daily-loss accounting just like a normal monitored exit.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import get_or_create_daily_state, get_or_create_settings
from app.models.db import AgentRun, Position
from app.models.enums import AssetClass, Direction, OrderSide, OrderType, PositionStatus
from app.models.schemas import OrderRequest

log = get_logger("execution.kill_switch")


def flatten_all(session: Session) -> dict:
    """Close all open positions immediately. Returns a summary."""
    settings = get_or_create_settings(session)
    open_positions = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()

    closed = 0
    for pos in open_positions:
        broker = get_broker_for(AssetClass(pos.asset_class), settings.broker_map)
        side = OrderSide.SELL if pos.direction == Direction.LONG.value else OrderSide.BUY
        req = OrderRequest(symbol=pos.symbol, asset_class=AssetClass(pos.asset_class),
                           side=side, order_type=OrderType.MARKET, qty=pos.qty)
        try:
            result = broker.submit_order(req)
        except Exception as exc:  # noqa: BLE001
            log.warning("flatten submit failed", extra={"symbol": pos.symbol, "error": str(exc)})
            continue

        fill = result.avg_fill_price or pos.last_price or pos.entry_price
        sign = 1 if pos.direction == Direction.LONG.value else -1
        realized = round(sign * pos.qty * (fill - pos.entry_price), 2)
        pos.status = PositionStatus.CLOSED.value
        pos.closed_at = datetime.now(timezone.utc)
        pos.realized_pnl = realized
        pos.unrealized_pnl = 0.0
        pos.last_price = fill

        daily = get_or_create_daily_state(session)
        daily.realized_pnl = round(daily.realized_pnl + realized, 2)
        session.add_all([pos, daily])
        closed += 1

    session.add(AgentRun(agent="kill_switch", event="flatten_all",
                         detail={"closed": closed}))
    session.commit()
    log.warning("flatten_all complete", extra={"closed": closed})
    return {"closed": closed}
