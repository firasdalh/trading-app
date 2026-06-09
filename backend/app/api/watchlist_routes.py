"""Watchlist + autonomous-scanner routes (M+).

The scanner periodically analyzes these pairs and (in Modes B/C) auto-executes risk-approved
setups; in Mode A it queues proposals for approval. Auto-close at stop/target is handled by
the Monitor.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.scanner import get_or_create_scan_config, run_scan
from app.core.database import get_session
from app.core.logging import get_logger
from app.models.db import WatchItem
from app.models.enums import AssetClass

log = get_logger("api.watchlist")
router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchItemRequest(BaseModel):
    symbol: str
    asset_class: AssetClass = AssetClass.FOREX
    timeframe: str = "1h"


class WatchItemView(BaseModel):
    id: int
    symbol: str
    asset_class: str
    timeframe: str
    enabled: bool


class ScanConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(None, ge=20, le=3600)


class WatchlistResponse(BaseModel):
    items: list[WatchItemView]
    scan_enabled: bool
    interval_seconds: int
    last_scan_at: str | None = None


def _iso_utc(dt) -> str | None:
    """Stamp naive (SQLite) timestamps as UTC so the browser doesn't read them as local time."""
    if dt is None:
        return None
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _response(session: Session) -> WatchlistResponse:
    cfg = get_or_create_scan_config(session)
    items = session.scalars(select(WatchItem).order_by(WatchItem.id)).all()
    return WatchlistResponse(
        items=[WatchItemView(id=i.id, symbol=i.symbol, asset_class=i.asset_class,
                             timeframe=i.timeframe, enabled=i.enabled) for i in items],
        scan_enabled=cfg.enabled,
        interval_seconds=cfg.interval_seconds,
        last_scan_at=_iso_utc(cfg.last_scan_at),
    )


@router.get("", response_model=WatchlistResponse)
def get_watchlist(session: Session = Depends(get_session)) -> WatchlistResponse:
    return _response(session)


@router.post("", response_model=WatchlistResponse)
def add_item(req: WatchItemRequest, session: Session = Depends(get_session)) -> WatchlistResponse:
    from sqlalchemy import func

    symbol = req.symbol.strip()  # preserve broker casing (e.g. XAUUSDm)
    exists = session.scalar(
        select(WatchItem).where(func.lower(WatchItem.symbol) == symbol.lower(),
                                WatchItem.asset_class == req.asset_class.value,
                                WatchItem.timeframe == req.timeframe)
    )
    if not exists:
        session.add(WatchItem(symbol=symbol, asset_class=req.asset_class.value,
                              timeframe=req.timeframe, enabled=True))
        session.commit()
        log.info("watch item added", extra={"symbol": symbol})
    return _response(session)


@router.delete("/{item_id}", response_model=WatchlistResponse)
def remove_item(item_id: int, session: Session = Depends(get_session)) -> WatchlistResponse:
    item = session.get(WatchItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="watch item not found")
    session.delete(item)
    session.commit()
    return _response(session)


@router.post("/scan-config", response_model=WatchlistResponse)
def set_scan_config(req: ScanConfigRequest, session: Session = Depends(get_session)) -> WatchlistResponse:
    cfg = get_or_create_scan_config(session)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.interval_seconds is not None:
        cfg.interval_seconds = req.interval_seconds
    session.commit()
    log.warning("scan config updated", extra={"enabled": cfg.enabled, "interval": cfg.interval_seconds})
    return _response(session)


@router.post("/scan-now")
def scan_now(session: Session = Depends(get_session)) -> dict:
    """Run one scan immediately (regardless of the interval)."""
    return run_scan(session)


class OpportunityView(BaseModel):
    symbol: str
    asset_class: str
    timeframe: str
    direction: str
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    rr: float | None = None
    confidence: float
    watch: bool
    rationale: str
    risk_approved: bool
    risk_decision: str | None = None
    risk_reason: str | None = None
    already_open: bool = False


@router.get("/opportunities", response_model=list[OpportunityView])
def opportunities(session: Session = Depends(get_session)) -> list[OpportunityView]:
    """Scan every enabled watchlist pair and return the setups ranked best-first — actionable &
    risk-approved on top, then forming ('watch'), then the rest. Read-only: nothing is opened."""
    from app.agents.pipeline import preview_symbol
    from app.risk.service import live_broker_positions

    open_syms = {p.symbol.upper() for p in live_broker_positions(session)}
    items = session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()

    out: list[OpportunityView] = []
    for it in items:
        try:
            # Full LLM analysis (same engine as the "Run analysis" button) so the scan is a
            # proper read — fundamentals + the AI reviewer contribute, not deterministic-only.
            prop, dec = preview_symbol(session, it.symbol, AssetClass(it.asset_class),
                                       it.timeframe, use_llm=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("opportunity preview failed", extra={"symbol": it.symbol, "error": str(exc)})
            continue
        rr = None
        if prop.entry and prop.stop_loss and prop.take_profit:
            risk = abs(prop.entry - prop.stop_loss)
            rr = round(abs(prop.take_profit - prop.entry) / risk, 2) if risk else None
        out.append(OpportunityView(
            symbol=it.symbol, asset_class=it.asset_class, timeframe=it.timeframe,
            direction=prop.direction.value, entry=prop.entry, stop_loss=prop.stop_loss,
            take_profit=prop.take_profit, rr=rr, confidence=prop.confidence, watch=prop.watch,
            rationale=prop.rationale, risk_approved=dec.approved, risk_decision=dec.decision.value,
            risk_reason=dec.reason, already_open=it.symbol.upper() in open_syms,
        ))

    def rank(o: OpportunityView):
        actionable = o.direction in ("long", "short")
        if actionable and o.risk_approved and not o.already_open:
            tier = 0  # open it now
        elif actionable:
            tier = 1  # a real setup with levels, but blocked (risk/cooldown/paused) or already open
        elif o.watch:
            tier = 2  # forming — not ready yet
        else:
            tier = 3  # no trade
        return (tier, -o.confidence, -(o.rr or 0))

    out.sort(key=rank)
    return out
