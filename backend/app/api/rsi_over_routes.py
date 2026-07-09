"""RSI-Over strategy: one endpoint that scans the watchlist for the first RSI-extreme + EMA10-
confirmed reversal and stages it (Mode A: queues for approval; Modes B/C: auto-opens)."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.rsi_over import get_or_create_rsi_over_config, run_rsi_over_scan
from app.core.database import get_session

router = APIRouter(prefix="/api/rsi-over", tags=["rsi-over"])


class RsiOverScanRequest(BaseModel):
    timeframe: str | None = None  # None -> 1h
    confirm: bool = True          # require the EMA10 close-through (strong confirmation)
    macd: bool = False            # also accept a MACD cross/divergence (early pullback entry)


@router.post("/scan")
def scan(req: RsiOverScanRequest, session: Session = Depends(get_session)) -> dict:
    """Sweep all available pairs on ``timeframe`` and stage the first tradeable RSI-Over setup.
    ``confirm`` = EMA10 close-through; ``macd`` = also accept a MACD cross/divergence (early). With
    neither, the RSI extreme alone fires. Returns {ran, found, reason, scanned, signals, candidates}."""
    return run_rsi_over_scan(session, req.timeframe, confirm=req.confirm, macd=req.macd)


# --- auto-watch (a timer that re-runs the sweep and stages the first pair that confirms) ---

class RsiOverConfigView(BaseModel):
    enabled: bool
    interval_seconds: int
    timeframe: str
    confirm: bool
    macd: bool
    last_run_at: datetime | None
    last_result: str | None
    # Snapshot of the last sweep so the panel restores it on refresh.
    last_scan_at: datetime | None
    last_scanned: int
    last_candidates: dict | None


class RsiOverConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    timeframe: str | None = None
    confirm: bool | None = None
    macd: bool | None = None


def _config_view(cfg) -> RsiOverConfigView:
    try:
        cands = json.loads(cfg.last_candidates) if cfg.last_candidates else None
    except (ValueError, TypeError):
        cands = None
    return RsiOverConfigView(enabled=cfg.enabled, interval_seconds=cfg.interval_seconds,
                             timeframe=cfg.timeframe, confirm=cfg.confirm, macd=cfg.macd,
                             last_run_at=cfg.last_run_at, last_result=cfg.last_result,
                             last_scan_at=cfg.last_scan_at, last_scanned=cfg.last_scanned or 0,
                             last_candidates=cands)


@router.get("/config", response_model=RsiOverConfigView)
def get_config(session: Session = Depends(get_session)) -> RsiOverConfigView:
    return _config_view(get_or_create_rsi_over_config(session))


@router.post("/config", response_model=RsiOverConfigView)
def set_config(req: RsiOverConfigRequest, session: Session = Depends(get_session)) -> RsiOverConfigView:
    """Turn the auto-watch on/off and set its interval / timeframe / confirmation."""
    cfg = get_or_create_rsi_over_config(session)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.interval_seconds is not None:
        cfg.interval_seconds = max(60, req.interval_seconds)  # floor at 1 min
    if req.timeframe is not None:
        cfg.timeframe = req.timeframe
    if req.confirm is not None:
        cfg.confirm = req.confirm
    if req.macd is not None:
        cfg.macd = req.macd
    session.add(cfg)
    session.commit()
    return _config_view(cfg)
