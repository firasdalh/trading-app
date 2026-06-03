"""Backtest route (Milestone 8).

Fetches historical OHLCV for the requested symbol/timeframe via the active data source and
replays it through the deterministic pipeline. Read-only — never touches a broker.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.backtest.engine import run_backtest
from app.brokers.registry import get_broker_for
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.schemas import BacktestRequest, BacktestResult
from app.risk.service import build_limits

log = get_logger("api.backtest")
router = APIRouter(prefix="/api", tags=["backtest"])


@router.post("/backtest", response_model=BacktestResult)
def backtest(req: BacktestRequest, session: Session = Depends(get_session)) -> BacktestResult:
    settings = get_or_create_settings(session)
    broker = get_broker_for(req.asset_class, settings.broker_map)
    try:
        series = broker.get_ohlcv(req.symbol, req.timeframe, limit=req.bars)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not fetch data: {exc}") from exc
    if len(series.candles) < 50:
        raise HTTPException(status_code=400, detail="not enough data to backtest (need >= 50 bars)")

    limits = build_limits(session)
    return run_backtest(req.symbol, req.asset_class, series, limits, req.starting_equity)
