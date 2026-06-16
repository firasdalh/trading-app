"""Journal / reflection routes (Milestone 9).

- GET  /api/journal/trades            -> recent CLOSED positions (the trade log).
- POST /api/journal/reflect           -> run the read-only Reflection agent now.
- GET  /api/journal/reflection/latest -> the most recent stored reflection (or null).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.reflection import latest_reflection, run_reflection
from app.core.database import get_session
from app.models.db import Position
from app.models.enums import PositionStatus
from app.models.schemas import PositionView, ReflectionReport

router = APIRouter(prefix="/api/journal", tags=["journal"])


class CalibrationBucket(BaseModel):
    bucket: str               # confidence range, e.g. "70-80%"
    trades: int
    wins: int
    win_rate: float | None    # None when the bucket has no trades yet
    avg_r: float | None       # mean realized R (realized_pnl / risk_amount)


@router.get("/calibration", response_model=list[CalibrationBucket])
def calibration(session: Session = Depends(get_session)) -> list[CalibrationBucket]:
    """Per-confidence-bucket win rate + avg R over CLOSED app-tracked trades — i.e. does the
    engine's confidence actually predict outcomes? (Does "70%" win ~70%?) Use it to recalibrate
    the score and the Hybrid auto-open threshold once enough trades have accumulated."""
    rows = session.scalars(
        select(Position).where(
            Position.status == PositionStatus.CLOSED.value,
            Position.confidence.is_not(None),
            Position.realized_pnl.is_not(None),
        )
    ).all()
    edges = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    out: list[CalibrationBucket] = []
    for lo, hi in edges:
        b = [r for r in rows if lo <= (r.confidence or 0.0) < hi]
        n = len(b)
        wins = sum(1 for r in b if (r.realized_pnl or 0.0) > 0)
        rs = [(r.realized_pnl or 0.0) / r.risk_amount for r in b if r.risk_amount]
        out.append(CalibrationBucket(
            bucket=f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%",
            trades=n, wins=wins,
            win_rate=round(wins / n, 3) if n else None,
            avg_r=round(sum(rs) / len(rs), 2) if rs else None,
        ))
    return out


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
