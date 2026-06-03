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
