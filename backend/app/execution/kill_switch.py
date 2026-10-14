"""Kill-switch flatten: close every open position across configured brokers.

The kill-switch (UI button / env flag) halts new orders everywhere. Flattening is the
optional "and close what's open" action. It works even mid-cycle and books realized P&L /
daily-loss accounting just like a normal monitored exit.

It closes through ``broker.close_position`` (by ticket) — NEVER by sending an opposite market
order: on a hedging MT5 account (Exness default) an opposite order OPENS a new position instead of
closing, so the old flatten would have doubled the book while marking it closed. It then sweeps the
broker's own book, so trades opened directly in the terminal / on the phone are closed too.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.db import AgentRun, Position
from app.models.enums import AssetClass, OrderStatus, PositionStatus

log = get_logger("execution.kill_switch")


def flatten_all(session: Session) -> dict:
    """Close all open positions immediately. Returns ``{"closed": n, "failed": [...]}`` — anything
    in ``failed`` is STILL OPEN at the broker and needs attention."""
    from app.execution.monitor import _close_position

    settings = get_or_create_settings(session)
    bm = settings.broker_map or {}
    closed = 0
    failed: list[dict] = []

    # 1) App-tracked positions: close by ticket and book their P&L (same path as a monitored exit).
    open_rows = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()
    for pos in open_rows:
        broker = get_broker_for(AssetClass(pos.asset_class), bm)
        try:
            price = broker.get_quote(pos.symbol).price
        except Exception:  # noqa: BLE001 - only the P&L fallback needs it, never the close itself
            price = pos.last_price or pos.entry_price
        if _close_position(session, pos, broker, price, "kill-switch flatten"):
            closed += 1
        else:
            failed.append({"symbol": pos.symbol, "error": "broker close failed — still open"})
    session.commit()

    # 2) Broker truth: whatever is still open (opened in the terminal / on the phone, or a row the
    #    app lost track of). One sweep per distinct broker.
    seen: set[str] = set()
    for ac in AssetClass:
        name = bm.get(ac.value, "sim")
        if name in seen:
            continue
        seen.add(name)
        broker = get_broker_for(ac, bm)
        try:
            book = broker.get_open_positions()
        except Exception as exc:  # noqa: BLE001 - report it: we can't claim the book is flat
            failed.append({"broker": name, "error": f"open positions unavailable: {exc}"})
            continue
        for symbol in sorted({p.symbol for p in book}):
            try:
                result = broker.close_position(symbol)
            except Exception as exc:  # noqa: BLE001 - flatten what we can; report the rest
                failed.append({"symbol": symbol, "error": str(exc)})
                continue
            if result.status == OrderStatus.ERROR:
                failed.append({"symbol": symbol, "error": result.error or "close failed"})
            elif result.status != OrderStatus.REJECTED:   # REJECTED = nothing left to close there
                closed += sum(1 for p in book if p.symbol == symbol)

    session.add(AgentRun(agent="kill_switch", event="flatten_all",
                         detail={"closed": closed, "failed": failed}))
    session.commit()
    log.warning("flatten_all complete", extra={"closed": closed, "failed": failed})
    return {"closed": closed, "failed": failed}
