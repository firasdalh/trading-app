"""Per-pair AI auto-trader: toggle pairs on/off, tune the params, and run a pass on demand.
Off/empty by default. Paper-only; every risk gate applies (see app/agents/auto_trade.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.auto_trade import get_or_create_auto_trade_config, run_auto_trade, set_pair
from app.core.database import get_session

router = APIRouter(prefix="/api/auto-trade", tags=["auto-trade"])


class AutoTradeView(BaseModel):
    enabled: bool
    interval_seconds: int
    min_confidence: float
    min_rr: float
    min_profit_usd: float
    cooldown_minutes: int
    strategy: str = "scenario"   # "scenario" (AI) | "supertrend" (mechanical)
    timeframe: str = "1h"        # ONE timeframe applied to every auto-traded pair
    pairs: list[dict]
    last_run_at: str | None = None
    last_result: str | None = None
    last_results: list[dict] = []   # per-pair outcome + reason from the last tick


def _view(cfg) -> AutoTradeView:
    return AutoTradeView(
        enabled=cfg.enabled, interval_seconds=cfg.interval_seconds,
        min_confidence=cfg.min_confidence, min_rr=getattr(cfg, "min_rr", 1.2),
        min_profit_usd=getattr(cfg, "min_profit_usd", 20.0),
        cooldown_minutes=cfg.cooldown_minutes,
        strategy=getattr(cfg, "strategy", None) or "scenario",
        timeframe=getattr(cfg, "timeframe", None) or "1h",
        pairs=list(cfg.pairs or []),
        last_run_at=cfg.last_run_at.isoformat() if cfg.last_run_at else None,
        last_result=cfg.last_result,
        last_results=list(getattr(cfg, "last_results", None) or []),
    )


@router.get("")
def get_config(session: Session = Depends(get_session)) -> AutoTradeView:
    return _view(get_or_create_auto_trade_config(session))


class PairRequest(BaseModel):
    symbol: str = Field(min_length=1)
    asset_class: str = "forex"
    timeframe: str = "1h"
    on: bool


@router.post("/pair")
def toggle_pair(req: PairRequest, session: Session = Depends(get_session)) -> AutoTradeView:
    """Turn auto-trade on/off for one pair."""
    return _view(set_pair(session, req.symbol, req.asset_class, req.on, req.timeframe))


class ConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(None, ge=60, le=7200)
    min_confidence: float | None = Field(None, ge=0.3, le=0.95)
    min_rr: float | None = Field(None, ge=1.0, le=3.0)
    min_profit_usd: float | None = Field(None, ge=0.0, le=100000.0)
    cooldown_minutes: int | None = Field(None, ge=0, le=240)
    strategy: str | None = Field(None, pattern="^(scenario|supertrend|reversal)$")
    timeframe: str | None = Field(None, pattern="^(5m|15m|30m|1h|4h|1d)$")


@router.post("/config")
def set_config(req: ConfigRequest, session: Session = Depends(get_session)) -> AutoTradeView:
    cfg = get_or_create_auto_trade_config(session)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.interval_seconds is not None:
        cfg.interval_seconds = req.interval_seconds
    if req.min_confidence is not None:
        cfg.min_confidence = req.min_confidence
    if req.min_rr is not None:
        cfg.min_rr = req.min_rr
    if req.min_profit_usd is not None:
        cfg.min_profit_usd = req.min_profit_usd
    if req.cooldown_minutes is not None:
        cfg.cooldown_minutes = req.cooldown_minutes
    if req.strategy is not None:
        cfg.strategy = req.strategy
    if req.timeframe is not None:
        cfg.timeframe = req.timeframe
    session.commit()
    return _view(cfg)


@router.post("/run")
def run_now(session: Session = Depends(get_session)) -> dict:
    """Run one auto-trade pass immediately (respects every gate; paper-only)."""
    cfg = get_or_create_auto_trade_config(session)
    if not (cfg.pairs or []):
        raise HTTPException(status_code=409, detail="no pairs enabled for auto-trade")
    return run_auto_trade(session)
