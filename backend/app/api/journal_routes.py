"""Journal / reflection routes (Milestone 9).

- GET  /api/journal/trades            -> recent CLOSED positions (the trade log).
- POST /api/journal/reflect           -> run the read-only Reflection agent now.
- GET  /api/journal/reflection/latest -> the most recent stored reflection (or null).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.reflection import latest_reflection, run_reflection
from app.core.database import get_session
from app.models.db import Position
from app.models.enums import PositionStatus
from app.models.schemas import PositionView, ReflectionReport

router = APIRouter(prefix="/api/journal", tags=["journal"])


@router.get("/trades", response_model=list[PositionView])
def closed_trades(
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[PositionView]:
    """Closed-trade log. Prefers the broker's own deal history (MT5) so entry/exit/P&L/date
    match the Exness journal exactly; falls back to app-tracked closed positions otherwise."""
    from app.risk.service import broker_closed_trades

    broker_trades = broker_closed_trades(session)
    if broker_trades is not None:
        return broker_trades[:limit]

    rows = session.scalars(
        select(Position)
        .where(Position.status == PositionStatus.CLOSED.value)
        .order_by(Position.closed_at.desc())
        .limit(limit)
    ).all()
    return [PositionView.model_validate(r) for r in rows]


@router.post("/reflect", response_model=ReflectionReport)
def reflect(session: Session = Depends(get_session)) -> ReflectionReport:
    return run_reflection(session)


@router.get("/reflection/latest", response_model=ReflectionReport | None)
def reflection_latest(session: Session = Depends(get_session)) -> ReflectionReport | None:
    return latest_reflection(session)
