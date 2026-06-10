"""Position Monitor: marks open positions to market and manages protective exits.

Runs periodically (APScheduler). For each open position it refreshes the last price and
unrealized P&L, then closes the position if the stop-loss or take-profit is hit. On a close
it books realized P&L into the day's risk state and trips the daily-loss auto-pause when the
limit is reached.

Closing positions is risk-REDUCING, so the monitor still runs while the kill-switch is
engaged (the kill-switch only blocks *new* exposure). New entries are blocked in the
executor, not here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import get_or_create_daily_state, get_or_create_risk_config, get_or_create_settings
from app.models.db import AgentRun, Position
from app.models.enums import AssetClass, Direction, OrderSide, OrderStatus, OrderType, PositionStatus
from app.models.schemas import OrderRequest

log = get_logger("execution.monitor")


def _exit_reason(direction: str, price: float, stop: float | None, target: float | None) -> str | None:
    if direction == Direction.LONG.value:
        if stop is not None and price <= stop:
            return "stop"
        if target is not None and price >= target:
            return "target"
    else:  # short
        if stop is not None and price >= stop:
            return "stop"
        if target is not None and price <= target:
            return "target"
    return None


def _contract_size(broker, symbol: str) -> float:
    """Broker contract size for P&L scaling; defensive so a lookup failure never blocks a close."""
    try:
        return broker.contract_size(symbol) or 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _close_position(session: Session, pos: Position, broker, exit_price: float, reason: str) -> None:
    # Close the EXISTING position by ticket — never fire a fresh opposite order. On a hedging
    # MT5 account (Exness default) an opposing market order opens a NEW opposite position
    # instead of closing, which would silently flip a short into a long.
    result = broker.close_position(pos.symbol)
    fill = result.avg_fill_price or exit_price
    sign = 1 if pos.direction == Direction.LONG.value else -1
    contract = _contract_size(broker, pos.symbol)
    realized = round(sign * pos.qty * contract * (fill - pos.entry_price), 2)

    pos.status = PositionStatus.CLOSED.value
    pos.closed_at = datetime.now(timezone.utc)
    pos.last_price = fill
    pos.realized_pnl = realized
    pos.unrealized_pnl = 0.0

    daily = get_or_create_daily_state(session)
    daily.realized_pnl = round(daily.realized_pnl + realized, 2)

    # Daily-loss auto-pause.
    risk = get_or_create_risk_config(session)
    equity = daily.starting_equity or 0.0
    if equity and -daily.realized_pnl >= equity * risk.max_daily_loss and not daily.trading_paused:
        daily.trading_paused = True
        daily.pause_reason = "daily loss limit reached — auto-paused for the day"
        log.warning("daily loss limit hit — trading paused", extra={"realized": daily.realized_pnl})

    session.add_all([pos, daily, AgentRun(
        agent="monitor", symbol=pos.symbol, event="position_closed",
        detail={"reason": reason, "exit": fill, "realized_pnl": realized,
                "broker_status": result.status.value},
    )])
    log.info("position closed", extra={"symbol": pos.symbol, "reason": reason,
                                       "exit": fill, "realized_pnl": realized})


def close_one(session: Session, position_id: int) -> dict:
    """Manually close a single open position (from the UI). Books realized P&L like a
    monitored exit. Closing reduces risk, so it is allowed even under the kill-switch."""
    pos = session.get(Position, position_id)
    if pos is None:
        return {"closed": False, "error": "position not found"}
    if pos.status != PositionStatus.OPEN.value:
        return {"closed": False, "error": f"position is {pos.status}, not open"}

    settings = get_or_create_settings(session)
    broker = get_broker_for(AssetClass(pos.asset_class), settings.broker_map)
    try:
        price = broker.get_quote(pos.symbol).price
    except Exception as exc:  # noqa: BLE001
        price = pos.last_price or pos.entry_price
        log.warning("manual close quote failed; using last price", extra={"symbol": pos.symbol, "error": str(exc)})

    _close_position(session, pos, broker, price, "manual")
    session.commit()
    return {"closed": True, "symbol": pos.symbol, "realized_pnl": pos.realized_pnl}


def _reconcile_closed_at_broker(session: Session, settings, open_positions: list[Position]) -> int:
    """Mark app positions CLOSED when the broker no longer holds them — i.e. they were closed in
    the MT5 terminal, by a broker-side SL/TP, or by any path that didn't update the DB. Without
    this, such 'phantom' rows linger as OPEN forever and wrongly count against the anti-stacking
    rule and the position cap, and show as open in the UI when they aren't.

    Outage-safe: a position is reconciled only when ITS OWN broker successfully reports an open
    book that lacks it. (`live_broker_positions` swallows per-broker errors and can return an empty
    list on an outage, so we fetch per broker here and skip any broker whose fetch failed — never
    closing a position just because the broker was unreachable.)

    Does NOT book realized P&L into the daily state: realized P&L and the journal already come from
    the broker's own deal history (broker truth), so re-booking here would double-count. This only
    removes the phantom from the OPEN set.
    """
    from app.risk.service import _norm_symbol

    bm = settings.broker_map or {}
    broker_books: dict[str, tuple[bool, set[tuple[str, str]]]] = {}  # broker name -> (fetch_ok, keys)

    def book_for(asset_class: str) -> tuple[bool, set[tuple[str, str]]]:
        name = bm.get(asset_class, "sim")
        if name not in broker_books:
            # Everything here is wrapped: a bad asset_class string (legacy/migrated row) or a
            # broker that can't be built/reached must never close DB rows — and must never abort
            # the whole monitoring pass (which would silently stop SL/TP management).
            try:
                broker = get_broker_for(AssetClass(asset_class), bm)
                # Only a durable-account broker (MT5/Exness) is authoritative for "still open". The
                # sim broker forgets positions on restart, so its empty book must never close DB rows.
                if not getattr(broker, "reconciles_positions", False):
                    broker_books[name] = (False, set())
                else:
                    positions = broker.get_open_positions()
                    broker_books[name] = (True, {(_norm_symbol(p.symbol), p.direction) for p in positions})
            except Exception as exc:  # noqa: BLE001 - bad asset_class / unbuildable / unreachable => skip
                log.warning("reconcile: broker open-book unavailable",
                            extra={"broker": name, "asset_class": asset_class, "error": str(exc)})
                broker_books[name] = (False, set())
        return broker_books[name]

    reconciled = 0
    for pos in open_positions:
        ok, keys = book_for(pos.asset_class)
        if not ok:
            continue
        if (_norm_symbol(pos.symbol), pos.direction) not in keys:
            pos.status = PositionStatus.CLOSED.value
            pos.closed_at = datetime.now(timezone.utc)
            session.add(pos)
            session.add(AgentRun(
                agent="monitor", symbol=pos.symbol, event="position_reconciled",
                detail={"reason": "closed at broker (reconciled)", "direction": pos.direction},
            ))
            log.warning("position reconciled (closed at broker)",
                        extra={"symbol": pos.symbol, "direction": pos.direction, "id": pos.id})
            reconciled += 1
    return reconciled


def monitor_positions(session: Session) -> dict:
    """One monitoring pass over all open positions. Returns a small summary dict."""
    settings = get_or_create_settings(session)
    open_positions = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()

    # Reconcile first: drop positions the broker no longer holds (closed in the terminal /
    # broker-side) so we neither price nor manage phantoms, and they stop blocking new trades.
    if _reconcile_closed_at_broker(session, settings, open_positions):
        session.commit()
        open_positions = session.scalars(
            select(Position).where(Position.status == PositionStatus.OPEN.value)
        ).all()

    checked, closed = 0, 0
    for pos in open_positions:
        checked += 1
        broker = get_broker_for(AssetClass(pos.asset_class), settings.broker_map)
        try:
            price = broker.get_quote(pos.symbol).price
        except Exception as exc:  # noqa: BLE001
            log.warning("monitor quote failed", extra={"symbol": pos.symbol, "error": str(exc)})
            continue

        sign = 1 if pos.direction == Direction.LONG.value else -1
        contract = _contract_size(broker, pos.symbol)
        pos.last_price = price
        pos.unrealized_pnl = round(sign * pos.qty * contract * (price - pos.entry_price), 2)

        reason = _exit_reason(pos.direction, price, pos.stop_loss, pos.take_profit)
        if reason:
            _close_position(session, pos, broker, price, reason)
            closed += 1
        else:
            session.add(pos)

    session.commit()

    # Account-truth daily-loss guard (realized today + floating vs the RISK.md limit).
    try:
        from app.risk.service import evaluate_daily_pause
        evaluate_daily_pause(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("daily pause evaluation failed", extra={"error": str(exc)})

    return {"checked": checked, "closed": closed}
