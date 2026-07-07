"""Shadow scorecard API — the AI-vs-deterministic head-to-head proof.

- GET  /api/shadow/scorecard : aggregated win rate + expectancy (AI vs deterministic), overall + by regime.
- POST /api/shadow/evaluate  : grade any pending decisions whose horizon has enough forward candles.
- GET  /api/shadow/decisions : the recent decision log (for inspection).

INFO/measurement only — never touches a live order.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.shadow import evaluate_shadows, scorecard
from app.core.database import get_session
from app.models.db import ShadowDecision

router = APIRouter(prefix="/api/shadow", tags=["shadow"])


@router.get("/scorecard")
def get_scorecard(session: Session = Depends(get_session)) -> dict:
    # Grade any pending decisions whose horizon now has forward candles, then aggregate. Best-effort:
    # a grading hiccup must never blank the scorecard.
    try:
        evaluate_shadows(session)
    except Exception:  # noqa: BLE001
        pass
    return scorecard(session)


@router.post("/evaluate")
def post_evaluate(session: Session = Depends(get_session)) -> dict:
    """Grade pending decisions now, then return the refreshed scorecard."""
    graded = evaluate_shadows(session)
    return {"graded": graded, "scorecard": scorecard(session)}


@router.get("/decisions")
def get_decisions(
    limit: int = Query(100, ge=1, le=500),
    evaluated: bool | None = Query(None, description="filter by graded/pending"),
    session: Session = Depends(get_session),
) -> list[dict]:
    q = select(ShadowDecision).order_by(ShadowDecision.created_at.desc()).limit(limit)
    if evaluated is not None:
        q = select(ShadowDecision).where(ShadowDecision.evaluated.is_(evaluated)).order_by(
            ShadowDecision.created_at.desc()).limit(limit)
    rows = session.scalars(q).all()
    return [
        {
            "id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
            "symbol": r.symbol, "timeframe": r.timeframe, "regime": r.regime, "price_at": r.price_at,
            "ai_action": r.ai_action, "ai_direction": r.ai_direction, "ai_scenario": r.ai_scenario,
            "ai_entry": r.ai_entry, "ai_stop": r.ai_stop, "ai_target": r.ai_target, "ai_conf": r.ai_conf,
            "det_direction": r.det_direction, "det_conf": r.det_conf,
            "evaluated": r.evaluated, "ai_outcome": r.ai_outcome, "ai_r": r.ai_r,
            "det_outcome": r.det_outcome, "det_r": r.det_r, "missed_move": r.missed_move,
        }
        for r in rows
    ]
