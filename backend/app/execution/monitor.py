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
from app.models.enums import AssetClass, Direction, PositionStatus

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


def _aware(dt):
    """SQLite drops tzinfo; treat naive timestamps as UTC so we can compare against broker (aware)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fill_realized_pnl_from_broker(session: Session, positions: list[Position]) -> int:
    """Recover realized P&L + exit price for CLOSED app rows that never booked it (closed broker-side
    / in the terminal, so the monitor only reconciled them to CLOSED without a $ result). Pulled from
    the broker's OWN deal history (broker truth), matched to each row by normalized symbol + direction
    + entry price (±0.5%) + nearest close time, one broker trade per row.

    Journal-only: this fills the row's ``realized_pnl``/``last_price`` for the stats + by-source
    breakdown. It does NOT book into the daily-loss state (that already comes from broker truth), so
    it can never double-count. Returns the number of rows filled.
    """
    from app.risk.service import _norm_symbol, broker_closed_trades

    targets = [p for p in positions
               if p.realized_pnl is None and p.status == PositionStatus.CLOSED.value]
    if not targets:
        return 0
    broker_trades = broker_closed_trades(session)  # list[PositionView] | None (respects journal reset)
    if not broker_trades:
        return 0

    used: set[int] = set()
    filled = 0
    for p in sorted(targets, key=lambda x: _aware(x.closed_at) or datetime.min.replace(tzinfo=timezone.utc)):
        best_i, best_key = None, None
        for i, bt in enumerate(broker_trades):
            if i in used or bt.direction != p.direction:
                continue
            if _norm_symbol(bt.symbol) != _norm_symbol(p.symbol):
                continue
            pe = abs(p.entry_price or 0.0)
            rel = abs((bt.entry_price or 0.0) - (p.entry_price or 0.0)) / max(pe, 1e-9)
            if rel > 0.005:   # >0.5% entry gap -> not the same trade
                continue
            bc, pc = _aware(bt.closed_at), _aware(p.closed_at)
            dt = abs((bc - pc).total_seconds()) if (bc and pc) else 1e12
            key = (rel, dt)
            if best_key is None or key < best_key:
                best_key, best_i = key, i
        if best_i is not None:
            bt = broker_trades[best_i]
            p.realized_pnl = bt.realized_pnl
            if bt.last_price:
                p.last_price = bt.last_price
            p.unrealized_pnl = 0.0
            used.add(best_i)
            session.add(p)
            filled += 1
    return filled


def flatten_before_weekend(session: Session) -> int:
    """Close open NON-CRYPTO positions in the final hours before the Friday close. Returns how many.

    The stop cannot protect a position across a closed market — price gaps over it and the trade
    exits at whatever the reopen prints (UKOILm #301: sized for -1R, realised -8.9R). The only real
    protection is to be flat. Crypto keeps trading through the weekend, so it is never touched.

    OPT-IN (off by default): unlike every other guard here this one ACTS on live positions rather
    than merely refusing to open, so it stays the user's explicit choice. Best-effort per position —
    one failure never stops the rest, since a half-flat book is still better than a full one."""
    from app.risk.weekend import in_weekend_window

    cfg = get_or_create_risk_config(session)
    if not getattr(cfg, "weekend_flatten_enabled", False):
        return 0
    hours = getattr(cfg, "weekend_flatten_hours", 1.0)
    now = datetime.now(timezone.utc)

    settings = get_or_create_settings(session)
    positions = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()
    closed = 0
    for pos in positions:
        if not in_weekend_window(now, hours, pos.asset_class):
            continue
        broker = get_broker_for(AssetClass(pos.asset_class), settings.broker_map)
        try:
            price = broker.get_quote(pos.symbol).price
            _close_position(session, pos, broker, price, "weekend-flat")
            closed += 1
            log.warning("closed before the weekend gap",
                        extra={"symbol": pos.symbol, "position_id": pos.id})
        except Exception as exc:  # noqa: BLE001 - flatten what we can; never abort the sweep
            log.warning("weekend flatten failed", extra={"symbol": pos.symbol, "error": str(exc)})
    if closed:
        session.commit()
    return closed


def monitor_positions(session: Session) -> dict:
    """One monitoring pass over all open positions. Returns a small summary dict."""
    settings = get_or_create_settings(session)
    open_positions = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()

    # Reconcile first: drop positions the broker no longer holds (closed in the terminal /
    # broker-side) so we neither price nor manage phantoms, and they stop blocking new trades.
    pre_ids = {p.id for p in open_positions}
    if _reconcile_closed_at_broker(session, settings, open_positions):
        session.commit()
        # Those just reconciled to CLOSED closed broker-side, so they carry no $ result — recover it
        # from broker deal history now so they show up in the stats + by-source breakdown (journal-only).
        just_closed = session.scalars(
            select(Position).where(Position.id.in_(pre_ids),
                                   Position.status == PositionStatus.CLOSED.value)
        ).all()
        try:
            if fill_realized_pnl_from_broker(session, just_closed):
                session.commit()
        except Exception as exc:  # noqa: BLE001 — P&L recovery is best-effort, never break monitoring
            log.warning("reconciled P&L backfill failed", extra={"error": str(exc)})
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

        reason = _exit_reason(pos.direction, price, pos.stop_loss, pos.take_profit)
        if reason:
            _close_position(session, pos, broker, price, reason)
            closed += 1
        else:
            session.add(pos)

    session.commit()

    # Weekend-gap protection: flatten what's left before the close (opt-in; no-op when off).
    flattened = 0
    try:
        flattened = flatten_before_weekend(session)
    except Exception as exc:  # noqa: BLE001 - never let this break the monitoring loop
        log.warning("weekend flatten pass failed", extra={"error": str(exc)})

    # Account-truth daily-loss guard (realized today + floating vs the RISK.md limit).
    try:
        from app.risk.service import evaluate_daily_pause
        evaluate_daily_pause(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("daily pause evaluation failed", extra={"error": str(exc)})

    return {"checked": checked, "closed": closed + flattened, "weekend_flattened": flattened}
