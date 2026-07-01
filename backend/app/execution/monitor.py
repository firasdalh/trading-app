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


def _pnl(broker, pos, price: float) -> float:
    """Account-currency P&L of ``pos`` valued at ``price``. Prefers the broker's currency-correct
    figure (MT5 ``order_calc_profit`` converts a JPY/HKD quote into the USD account); falls back to
    naive ``±lots × contract × price_diff`` (correct only when the quote ccy IS the account ccy,
    e.g. the sim/USD path). Never raises."""
    try:
        p = broker.position_profit(pos.symbol, pos.direction, pos.qty, pos.entry_price, price)
        if p is not None:
            return round(float(p), 2)
    except Exception:  # noqa: BLE001 - fall back to naive scaling
        pass
    sign = 1 if pos.direction == Direction.LONG.value else -1
    return round(sign * pos.qty * _contract_size(broker, pos.symbol) * (price - pos.entry_price), 2)


def _close_position(session: Session, pos: Position, broker, exit_price: float, reason: str) -> None:
    # Close the EXISTING position by ticket — never fire a fresh opposite order. On a hedging
    # MT5 account (Exness default) an opposing market order opens a NEW opposite position
    # instead of closing, which would silently flip a short into a long.
    result = broker.close_position(pos.symbol)
    fill = result.avg_fill_price or exit_price
    realized = _pnl(broker, pos, fill)  # broker-truth (currency-correct) when available

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


def _trail_one_supertrend(session: Session, settings, pos: Position, broker, price: float) -> None:
    """SuperTrend-band strategy: trail this position's stop to the current SuperTrend (2.3) line —
    TIGHTEN-ONLY (never loosens), so 'the stop follows SuperTrend'. Risk-reducing; still respects the
    live-execution gate (paper is always allowed). No-op unless the line is a tighter valid stop."""
    from app.agents.indicators import supertrend
    from app.core.state import live_execution_allowed
    from app.data.ohlcv_cache import get_ohlcv_cached
    from app.models.db import WatchItem

    if not broker.is_paper and not live_execution_allowed(settings):
        return
    tf = session.scalar(select(WatchItem.timeframe).where(WatchItem.symbol == pos.symbol)) or "1h"
    try:
        candles = get_ohlcv_cached(broker, pos.symbol, tf, limit=200).candles
    except Exception as exc:  # noqa: BLE001 - a data hiccup must never break the monitor pass
        log.warning("supertrend trail: ohlcv failed", extra={"symbol": pos.symbol, "error": str(exc)})
        return
    st = supertrend(candles)
    if not st:
        return
    line = round(st["line"], 6)
    cur = pos.stop_loss
    is_long = pos.direction == Direction.LONG.value
    # Only trail when the line is on the correct side of price AND tighter than the current stop.
    tighter = (line < price and (cur is None or line > cur)) if is_long \
        else (line > price and (cur is None or line < cur))
    if not tighter:
        return
    try:
        res = broker.set_sl_tp(pos.symbol, line, pos.take_profit)
        if res.status.value not in ("error", "rejected"):
            pos.stop_loss = line
            session.add(pos)
            log.info("supertrend trail", extra={"symbol": pos.symbol, "stop": line, "from": cur})
    except Exception as exc:  # noqa: BLE001
        log.warning("supertrend trail failed", extra={"symbol": pos.symbol, "error": str(exc)})


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

        pos.last_price = price
        pos.unrealized_pnl = _pnl(broker, pos, price)  # broker-truth (currency-correct) when available

        # SuperTrend-band strategy: trail the stop to the SuperTrend line (tighten-only) before the
        # exit check, so 'the stop follows SuperTrend'.
        if settings.st_band_mode:
            _trail_one_supertrend(session, settings, pos, broker, price)

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
