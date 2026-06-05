"""Proposal routes (Milestone 4, Mode A).

- POST /api/proposals/analyze  -> run the agent pipeline for a symbol, return proposal+risk.
- GET  /api/proposals          -> list recent proposals.
- GET  /api/proposals/{id}     -> one proposal with full reasoning.
- POST /api/proposals/{id}/approve | /reject -> Mode A user decision (no execution yet; M6).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pipeline import analyze_symbol
from app.core.database import get_session
from app.core.logging import get_logger
from app.models.db import TradeProposalRecord
from app.models.enums import ProposalStatus
from app.models.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    ProposalView,
    SizePreviewResponse,
    TradeEconomics,
)


class SizeRequest(BaseModel):
    # Desired position size in LOTS. None = use the AI's risk-based default size.
    lots: float | None = Field(None, gt=0)

log = get_logger("api.proposals")
router = APIRouter(prefix="/api/proposals", tags=["proposals"])


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest, session: Session = Depends(get_session)) -> AnalyzeResponse:
    return analyze_symbol(session, request.symbol, request.asset_class, request.timeframe)


@router.get("", response_model=list[ProposalView])
def list_proposals(
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(None),
    symbol: str | None = Query(None),
    timeframe: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[ProposalView]:
    from sqlalchemy import func

    stmt = select(TradeProposalRecord).order_by(TradeProposalRecord.id.desc())
    if status:
        stmt = stmt.where(TradeProposalRecord.status == status)
    if symbol:
        stmt = stmt.where(func.lower(TradeProposalRecord.symbol) == symbol.lower())
    if timeframe:
        stmt = stmt.where(TradeProposalRecord.timeframe == timeframe)
    rows = session.scalars(stmt.limit(limit)).all()
    return [ProposalView.model_validate(r) for r in rows]


@router.get("/{proposal_id}", response_model=ProposalView)
def get_proposal(proposal_id: int, session: Session = Depends(get_session)) -> ProposalView:
    row = session.get(TradeProposalRecord, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return ProposalView.model_validate(row)


@router.post("/{proposal_id}/size-preview", response_model=SizePreviewResponse)
def preview_size(
    proposal_id: int,
    req: SizeRequest | None = None,
    session: Session = Depends(get_session),
) -> SizePreviewResponse:
    """Risk verdict + cost (margin) and leverage for this proposal at a chosen lot size.

    Read-only. ``lots=None`` shows the AI's default size. Any size is clamped to the 2%
    per-trade ceiling, so the returned economics never exceed the hard cap.
    """
    row = session.get(TradeProposalRecord, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    from app.risk.service import size_preview

    out = size_preview(session, row, desired_lots=req.lots if req else None)
    return SizePreviewResponse(
        risk=out["risk"],
        economics=TradeEconomics(**out["economics"]),
        capped=out["capped"],
        max_lots=out["max_lots"],
    )


@router.post("/{proposal_id}/approve", response_model=ProposalView)
def approve_proposal(
    proposal_id: int,
    req: SizeRequest | None = None,
    session: Session = Depends(get_session),
) -> ProposalView:
    """Mode A approval — the user confirms, and we submit the order via the active broker.

    If ``lots`` is supplied, the trade is re-sized to that (clamped to the 2% per-trade ceiling
    by the Risk Manager) before execution. Execution gates (kill-switch, live-confirmation) are
    enforced in the executor; if a gate refuses, we surface 423 and leave it approved-unexecuted.
    """
    from app.execution.executor import ExecutionBlocked, execute_proposal

    row = session.get(TradeProposalRecord, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if row.status != ProposalStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=409, detail=f"cannot approve a proposal in status '{row.status}'")

    # User chose a custom size: re-run the Risk Manager at that size and persist the result.
    if req and req.lots is not None:
        from app.risk.service import size_preview

        out = size_preview(session, row, desired_lots=req.lots)
        decision = out["risk"]
        if not decision.approved or decision.approved_qty <= 0:
            raise HTTPException(status_code=409, detail=f"risk manager refused this size: {decision.reason}")
        row.approved_qty = decision.approved_qty
        row.risk_amount = decision.risk_amount
        row.risk_decision = decision.decision.value
        row.risk_reason = decision.reason
        log.warning("proposal resized by user before approval",
                    extra={"proposal_id": proposal_id, "lots": req.lots, "approved_qty": decision.approved_qty})

    row.status = ProposalStatus.APPROVED.value
    session.commit()
    try:
        execute_proposal(session, row)  # sets EXECUTED + opens a position on fill
    except ExecutionBlocked as exc:
        log.warning("approve blocked by safety gate", extra={"proposal_id": proposal_id, "reason": str(exc)})
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    session.refresh(row)
    log.warning("proposal approved + executed", extra={"proposal_id": proposal_id, "status": row.status})
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
