"""Proposal routes (Milestone 4, Mode A).

- POST /api/proposals/analyze  -> run the agent pipeline for a symbol, return proposal+risk.
- GET  /api/proposals          -> list recent proposals.
- GET  /api/proposals/{id}     -> one proposal with full reasoning.
- POST /api/proposals/{id}/approve | /reject -> Mode A user decision (no execution yet; M6).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pipeline import analyze_symbol
from app.core.database import get_session
from app.core.logging import get_logger
from app.models.db import TradeProposalRecord
from app.models.enums import ProposalStatus
from app.models.schemas import AnalyzeRequest, AnalyzeResponse, ProposalView

log = get_logger("api.proposals")
router = APIRouter(prefix="/api/proposals", tags=["proposals"])


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest, session: Session = Depends(get_session)) -> AnalyzeResponse:
    return analyze_symbol(session, request.symbol, request.asset_class, request.timeframe)


@router.get("", response_model=list[ProposalView])
def list_proposals(
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[ProposalView]:
    stmt = select(TradeProposalRecord).order_by(TradeProposalRecord.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(TradeProposalRecord.status == status)
    rows = session.scalars(stmt).all()
    return [ProposalView.model_validate(r) for r in rows]


@router.get("/{proposal_id}", response_model=ProposalView)
def get_proposal(proposal_id: int, session: Session = Depends(get_session)) -> ProposalView:
    row = session.get(TradeProposalRecord, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return ProposalView.model_validate(row)


@router.post("/{proposal_id}/approve", response_model=ProposalView)
def approve_proposal(proposal_id: int, session: Session = Depends(get_session)) -> ProposalView:
    """Mode A approval. Marks APPROVED; execution is wired in Milestone 6."""
    row = session.get(TradeProposalRecord, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if row.status != ProposalStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=409, detail=f"cannot approve a proposal in status '{row.status}'")
    row.status = ProposalStatus.APPROVED.value
    session.commit()
    log.warning("proposal approved (execution arrives in M6)", extra={"proposal_id": proposal_id})
    return ProposalView.model_validate(row)


@router.post("/{proposal_id}/reject", response_model=ProposalView)
def reject_proposal(proposal_id: int, session: Session = Depends(get_session)) -> ProposalView:
    row = session.get(TradeProposalRecord, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if row.status not in (ProposalStatus.PENDING_APPROVAL.value, ProposalStatus.PENDING_RISK.value):
        raise HTTPException(status_code=409, detail=f"cannot reject a proposal in status '{row.status}'")
    row.status = ProposalStatus.REJECTED.value
    session.commit()
    log.info("proposal rejected", extra={"proposal_id": proposal_id})
    return ProposalView.model_validate(row)
