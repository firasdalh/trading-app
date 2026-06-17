"""Conditional ('armed' / pending) setup routes.

- GET    /api/conditionals       -> armed setups first, then recent history (the Armed panel).
- POST   /api/conditionals       -> arm a setup from an engine suggestion (manual).
- DELETE /api/conditionals/{id}  -> cancel an armed setup.

Arming never opens a trade. On the trigger the Monitor re-runs the full analysis and only then
opens it (Modes B/C + Hybrid) or queues it for approval (Mode A) — all risk gates still apply.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.agents.conditional import arm_conditional, cancel_conditional, list_conditionals
from app.core.database import get_session
from app.core.state import get_or_create_settings
from app.models.db import ConditionalSetup
from app.models.enums import ConditionalStatus, ExecutionMode
from app.models.schemas import SizePreviewResponse, TradeEconomics

router = APIRouter(prefix="/api/conditionals", tags=["conditionals"])


class LotRequest(BaseModel):
    lots: float | None = Field(None, gt=0)


class ArmRequest(BaseModel):
    symbol: str
    asset_class: str
    timeframe: str = "1h"
    direction: str                      # "long" | "short"
    order_type: str                     # ConditionalOrderType value
    trigger_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float = 0.0
    rr: float | None = None
    reason: str = ""
    valid_hours: int = Field(12, ge=1, le=168)
    require_close_confirm: bool = True


class ConditionalSetupView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    symbol: str
    asset_class: str
    timeframe: str
    direction: str
    order_type: str
    trigger_price: float
    stop_loss: float | None
    take_profit: float | None
    confidence: float
    rr: float | None
    rationale: str
    status: str
    source: str
    auto_execute: bool
    require_close_confirm: bool
    valid_until: datetime | None
    triggered_at: datetime | None
    result_proposal_id: int | None
    last_note: str | None
    desired_lots: float | None


@router.get("", response_model=list[ConditionalSetupView])
def list_all(session: Session = Depends(get_session)) -> list[ConditionalSetupView]:
    return [ConditionalSetupView.model_validate(s) for s in list_conditionals(session)]


@router.post("", response_model=ConditionalSetupView)
def arm(req: ArmRequest, session: Session = Depends(get_session)) -> ConditionalSetupView:
    # In Mode A a manually-armed setup is queued for approval at the trigger; in Modes B/C it
    # auto-executes (after the re-check), matching how the rest of the app treats the mode.
    settings = get_or_create_settings(session)
    auto = settings.execution_mode != ExecutionMode.A_PROPOSE_APPROVE.value
    s = arm_conditional(
        session, symbol=req.symbol, asset_class=req.asset_class, timeframe=req.timeframe,
        direction=req.direction, order_type=req.order_type, trigger_price=req.trigger_price,
        stop_loss=req.stop_loss, take_profit=req.take_profit, confidence=req.confidence,
        rr=req.rr, rationale=req.reason, source="manual", auto_execute=auto,
        valid_hours=req.valid_hours, require_close_confirm=req.require_close_confirm,
    )
    if s is None:
        raise HTTPException(status_code=409,
                            detail="already armed for this symbol/direction, or the symbol is open")
    return ConditionalSetupView.model_validate(s)


def _setup_shim(s: ConditionalSetup):
    """A minimal proposal-like object from the armed setup's levels (trigger = entry), so the
    standard Risk Manager size/economics logic can price it without a stored proposal."""
    from types import SimpleNamespace

    return SimpleNamespace(
        symbol=s.symbol, asset_class=s.asset_class, timeframe=s.timeframe, direction=s.direction,
        entry=s.trigger_price, stop_loss=s.stop_loss, take_profit=s.take_profit,
        confidence=s.confidence,
    )


@router.post("/{setup_id}/size-preview", response_model=SizePreviewResponse)
def size_preview(setup_id: int, req: LotRequest | None = None,
                 session: Session = Depends(get_session)) -> SizePreviewResponse:
    """Risk verdict + $ economics (risk / cost) for an armed setup at a chosen lot — computed from
    the trigger as the entry. Read-only; any size is clamped to the 2% per-trade cap."""
    from app.risk.service import size_preview as _size_preview

    s = session.get(ConditionalSetup, setup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="not found")
    out = _size_preview(session, _setup_shim(s), desired_lots=req.lots if req else None)
    return SizePreviewResponse(
        risk=out["risk"], economics=TradeEconomics(**out["economics"]),
        capped=out["capped"], max_lots=out["max_lots"],
    )


@router.patch("/{setup_id}", response_model=ConditionalSetupView)
def set_lots(setup_id: int, req: LotRequest, session: Session = Depends(get_session)) -> ConditionalSetupView:
    """Set the lot this setup will open at when it fires (None = the AI's default size). Re-clamped
    to the 2% cap at fire time."""
    s = session.get(ConditionalSetup, setup_id)
    if s is None:
        raise HTTPException(status_code=404, detail="not found")
    if s.status != ConditionalStatus.ARMED.value:
        raise HTTPException(status_code=409, detail=f"cannot resize a setup in status '{s.status}'")
    s.desired_lots = req.lots
    session.commit()
    session.refresh(s)
    return ConditionalSetupView.model_validate(s)


@router.delete("/{setup_id}")
def cancel(setup_id: int, session: Session = Depends(get_session)) -> dict:
    if not cancel_conditional(session, setup_id):
        raise HTTPException(status_code=404, detail="not armed / not found")
    return {"cancelled": True}
