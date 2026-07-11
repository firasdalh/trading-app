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
from app.models.enums import AssetClass, ProposalStatus
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


class ManualTradeRequest(BaseModel):
    """A user-placed QUICK trade. It still goes through the deterministic Risk Manager + execution
    gates — nothing here bypasses risk."""
    symbol: str = Field(min_length=1)
    asset_class: AssetClass = AssetClass.FOREX
    direction: str                                  # "long" | "short"
    stop_loss: float | None = Field(None, gt=0)     # optional — if omitted, an ATR stop is auto-derived
    take_profit: float | None = Field(None, gt=0)   # optional — if omitted, a default R-multiple target
    entry: float | None = Field(None, gt=0)         # default: current market
    lots: float | None = Field(None, gt=0)          # default: the Risk Manager's 3%-capped size
    timeframe: str = "1h"                            # timeframe the auto ATR stop is measured on
    execute: bool = True                            # True = open now if approved; False = queue for approval


# When the user places a QUICK trade with no stop, the Risk Manager still needs a stop to size off,
# so we auto-derive one from volatility: stop = entry ∓ (mult × ATR14) on the trade's timeframe, and a
# default target at TARGET_R × the stop distance. Both are placed on the chart as draggable bars the
# user adjusts after the fill — the whole point of "quick" is not typing prices up front.
_DEFAULT_STOP_ATR_MULT = 1.5
_DEFAULT_TARGET_R = 2.0
_FALLBACK_STOP_PCT = 0.005   # if ATR is unavailable (thin history), fall back to 0.5% of price


def _auto_stop_distance(session: Session, symbol: str, asset_class: AssetClass,
                        timeframe: str, entry: float) -> float:
    """ATR-based stop distance for a manual ticket with no user stop (see _DEFAULT_STOP_ATR_MULT)."""
    from app.agents.indicators import atr
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.data.ohlcv_cache import get_ohlcv_cached

    dist = 0.0
    try:
        settings = get_or_create_settings(session)
        broker = get_broker_for(asset_class, settings.broker_map)
        series = get_ohlcv_cached(broker, symbol, timeframe or "1h", limit=200)
        a = atr(series.candles, 14)
        if a and a > 0:
            dist = _DEFAULT_STOP_ATR_MULT * a
    except Exception as exc:  # noqa: BLE001 — ATR is best-effort; fall back to a % of price
        log.warning("auto-stop ATR failed; using %% fallback",
                    extra={"symbol": symbol, "error": str(exc)})
    if dist <= 0:
        dist = _FALLBACK_STOP_PCT * entry
    return dist


class ExplainRequest(BaseModel):
    text: str = Field(min_length=1)
    lang: str = "en"  # "en" | "ar"


class ExplainResponse(BaseModel):
    decision: str
    is_trade: bool
    grade: str | None = None
    main_reason: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    conclusion: str
    lang: str


log = get_logger("api.proposals")
router = APIRouter(prefix="/api/proposals", tags=["proposals"])


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest, session: Session = Depends(get_session)) -> AnalyzeResponse:
    return analyze_symbol(session, request.symbol, request.asset_class, request.timeframe)


class ManualPreviewResponse(BaseModel):
    entry: float          # the market entry the ticket would use
    stop_loss: float      # the stop that will be used (user's, or the auto ATR stop)
    take_profit: float    # the target that will be used (user's, or the default R-multiple)
    auto_levels: bool     # True when the stop/target were auto-derived (no user stop given)
    approved: bool
    max_lots: float       # the Risk Manager's 3%-capped size (the "max" the ticket can suggest)
    risk_amount: float    # $ risked at that size
    reason: str


def _build_manual_proposal(req: "ManualTradeRequest", session: Session):
    """Validate a manual ticket, default the entry to the current market, and build the TradeProposal.
    Shared by the place + preview endpoints. Raises HTTPException on invalid direction/side/quote."""
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.models.enums import Direction
    from app.models.schemas import TradeProposal

    direction = (req.direction or "").lower()
    if direction not in ("long", "short"):
        raise HTTPException(status_code=422, detail="direction must be 'long' or 'short'")
    d = Direction.LONG if direction == "long" else Direction.SHORT

    entry = req.entry
    if entry is None:   # default the entry to the current market quote
        settings = get_or_create_settings(session)
        broker = get_broker_for(req.asset_class, settings.broker_map)
        try:
            entry = broker.get_quote(req.symbol).price
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not read a quote for {req.symbol}: {exc}") from exc
    if not entry or entry <= 0:
        raise HTTPException(status_code=422, detail="no valid entry price")

    # Stop: use the user's if given (side-checked), else auto-derive one from ATR so the Risk Manager
    # can still size at the 3% cap. The auto stop is always on the correct side by construction.
    auto_stop = req.stop_loss is None
    if auto_stop:
        dist = _auto_stop_distance(session, req.symbol, req.asset_class, req.timeframe, entry)
        stop_loss = entry - dist if d == Direction.LONG else entry + dist
    else:
        stop_loss = req.stop_loss
        if d == Direction.LONG and not stop_loss < entry:
            raise HTTPException(status_code=422, detail="for a LONG the stop must be BELOW the entry")
        if d == Direction.SHORT and not stop_loss > entry:
            raise HTTPException(status_code=422, detail="for a SHORT the stop must be ABOVE the entry")

    # Target: use the user's if given (side-checked), else a default R-multiple so a TP bar appears on
    # the chart for the user to drag. Both auto levels are just starting points, adjustable after fill.
    if req.take_profit is not None:
        take_profit = req.take_profit
        if d == Direction.LONG and not take_profit > entry:
            raise HTTPException(status_code=422, detail="for a LONG the target must be ABOVE the entry")
        if d == Direction.SHORT and not take_profit < entry:
            raise HTTPException(status_code=422, detail="for a SHORT the target must be BELOW the entry")
    else:
        risk = abs(entry - stop_loss)
        take_profit = (entry + _DEFAULT_TARGET_R * risk if d == Direction.LONG
                       else entry - _DEFAULT_TARGET_R * risk)

    rationale = ("Manual quick trade (risk-managed); auto ATR stop + default target — adjust on the chart."
                 if auto_stop else "Manual quick trade (risk-managed).")
    return TradeProposal(
        symbol=req.symbol, asset_class=req.asset_class, timeframe="manual", direction=d,
        entry=round(entry, 6), stop_loss=round(stop_loss, 6), take_profit=round(take_profit, 6),
        confidence=1.0, rationale=rationale, strategy="manual",
    )


@router.post("/manual/preview", response_model=ManualPreviewResponse)
def manual_trade_preview(req: ManualTradeRequest, session: Session = Depends(get_session)) -> ManualPreviewResponse:
    """Risk-size a manual ticket WITHOUT placing it — returns the max lots at the 3% cap + the $ risk,
    so the ticket can suggest 'max N lots'. Non-persisting, no execution."""
    from app.risk.service import assess

    proposal = _build_manual_proposal(req, session)
    decision = assess(session, proposal)
    return ManualPreviewResponse(
        entry=proposal.entry, stop_loss=proposal.stop_loss, take_profit=proposal.take_profit or 0.0,
        auto_levels=req.stop_loss is None, approved=decision.approved,
        max_lots=round(decision.approved_qty or 0.0, 2),
        risk_amount=round(decision.risk_amount or 0.0, 2), reason=decision.reason,
    )


@router.post("/manual", response_model=AnalyzeResponse)
def manual_trade(req: ManualTradeRequest, session: Session = Depends(get_session)) -> AnalyzeResponse:
    """Manual QUICK trade — the user places a trade directly, but it ALWAYS runs through the
    deterministic Risk Manager (sizing + 3% cap, exposure, correlation, per-pair cooldown, daily-loss
    breaker, anti-stacking) and the execution gates (kill-switch, live-confirmation). Nothing here
    bypasses risk. ``execute=True`` opens it now if approved; ``execute=False`` queues it for approval."""
    from app.execution.executor import ExecutionBlocked, execute_proposal
    from app.risk.service import assess, size_preview

    proposal = _build_manual_proposal(req, session)   # validates direction/side + defaults entry to market
    d = proposal.direction
    decision = assess(session, proposal)   # deterministic Risk Manager — sizes + gates

    record = TradeProposalRecord(
        symbol=req.symbol, asset_class=req.asset_class.value, timeframe="manual", direction=d.value,
        entry=proposal.entry, stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
        confidence=1.0, rationale=proposal.rationale, review_decision=None, reasoning={"manual": True},
        source="manual",
        risk_decision=decision.decision.value, risk_reason=decision.reason,
        approved_qty=decision.approved_qty, risk_amount=decision.risk_amount,
        status=(ProposalStatus.PENDING_APPROVAL.value if decision.approved else ProposalStatus.RISK_VETOED.value),
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    # Custom lot -> re-size through the Risk Manager (still clamped to the 3% cap).
    if decision.approved and req.lots is not None:
        out = size_preview(session, record, desired_lots=req.lots)
        dec2 = out["risk"]
        if not dec2.approved or dec2.approved_qty <= 0:
            raise HTTPException(status_code=409, detail=f"risk manager refused this size: {dec2.reason}")
        record.approved_qty, record.risk_amount = dec2.approved_qty, dec2.risk_amount
        record.risk_decision, record.risk_reason = dec2.decision.value, dec2.reason
        decision = dec2
        session.commit()

    if not decision.approved:
        log.info("manual trade vetoed by risk", extra={"symbol": req.symbol, "reason": decision.reason})
        return AnalyzeResponse(proposal_id=record.id, status=record.status, proposal=proposal, risk=decision)

    # Approved. Open now (execute=True) or leave it queued for the user's approval click.
    if req.execute:
        record.status = ProposalStatus.APPROVED.value
        session.commit()
        try:
            result = execute_proposal(session, record)  # enforces kill-switch + live-confirmation
        except ExecutionBlocked as exc:
            record.status = ProposalStatus.PENDING_APPROVAL.value
            session.commit()
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        session.refresh(record)
        if record.status != ProposalStatus.EXECUTED.value:
            record.status = ProposalStatus.PENDING_APPROVAL.value
            session.commit()
            raise HTTPException(status_code=422,
                                detail=f"order not filled — {result.error or 'broker rejected the order'}")
        log.warning("manual trade opened", extra={"symbol": req.symbol, "direction": d.value,
                                                  "qty": record.approved_qty})

    return AnalyzeResponse(proposal_id=record.id, status=record.status, proposal=proposal, risk=decision)


@router.post("/explain", response_model=ExplainResponse)
def explain(req: ExplainRequest) -> ExplainResponse:
    """Reformat a raw AI-review rationale into a structured (Decision / main reason / pros / cons /
    conclusion) explanation, in English or Arabic. Stateless — serves both the analysis report and
    the scan list. Faithful: it only restructures/translates, never adds new analysis."""
    from app.agents.explain import explain_review

    lang = "ar" if req.lang == "ar" else "en"
    out = explain_review(req.text, lang)
    if out is None:
        raise HTTPException(status_code=503,
                            detail="explanation unavailable — no AI model configured")
    return ExplainResponse(**out.model_dump(), lang=lang)


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

    Read-only. ``lots=None`` shows the AI's default size. Any size is clamped to the 3%
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

    If ``lots`` is supplied, the trade is re-sized to that (clamped to the 3% per-trade ceiling
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
        result = execute_proposal(session, row)  # sets EXECUTED + opens a position on fill
    except ExecutionBlocked as exc:
        # Revert to pending (not stuck in APPROVED) so the state is honest — nothing opened.
        row.status = ProposalStatus.PENDING_APPROVAL.value
        session.commit()
        log.warning("approve blocked by safety gate", extra={"proposal_id": proposal_id, "reason": str(exc)})
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    session.refresh(row)
    if row.status != ProposalStatus.EXECUTED.value:
        # The order was SUBMITTED but the broker didn't fill it — e.g. "AutoTrading disabled by
        # client" (the terminal's Algo Trading button is off), market closed, or a requote. Revert
        # to pending so the user can fix the cause and approve again, and surface WHY — instead of
        # a silent, permanent "approved — awaiting execution" with no explanation.
        row.status = ProposalStatus.PENDING_APPROVAL.value
        session.commit()
        log.warning("approve: order not filled", extra={"proposal_id": proposal_id, "error": result.error})
        raise HTTPException(status_code=422,
                            detail=f"order not filled — {result.error or 'broker rejected the order'}")
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
