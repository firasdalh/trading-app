"""The Strategies board: build, check and run custom strategies (app/agents/strategy.py).

- GET  /api/strategies/catalog       -> the conditions the board offers, with their parameters.
- GET  /api/strategies               -> every saved strategy, with what it is doing now.
- POST /api/strategies               -> save a new one (always saved stopped).
- PUT  /api/strategies/{id}          -> change one (a running one picks it up on its next pass).
- DELETE /api/strategies/{id}        -> delete a stopped one.
- POST /api/strategies/{id}/run      -> start / stop it.
- POST /api/strategies/check         -> what a draft's rules read on a pair right now (reads only).
- GET  /api/strategies/{id}/activity -> what it opened, closed and was refused.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import strategy as engine
from app.core.database import get_session
from app.core.logging import get_logger
from app.models.db import AgentRun, Position, StrategyConfig
from app.models.enums import AssetClass, PositionStatus

log = get_logger("api.strategies")
router = APIRouter(prefix="/api/strategies", tags=["strategies"])


class ConditionIn(BaseModel):
    type: str
    tf: str
    params: dict[str, float] = Field(default_factory=dict)


class PairIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass = AssetClass.FOREX


class StrategyIn(BaseModel):
    name: str = Field("My strategy", min_length=1, max_length=64)
    pairs: list[PairIn] = Field(default_factory=list, max_length=10)
    timeframe: str = "1m"
    sides: Literal["long", "short", "both"] = "both"
    entry_long: list[list[ConditionIn]] = Field(default_factory=list)
    entry_short: list[list[ConditionIn]] = Field(default_factory=list)
    mirror_short: bool = True
    exit_rules: list[list[ConditionIn]] = Field(default_factory=list)
    # Per trade, of equity. The Risk Manager re-clamps it to the per-trade ceiling regardless.
    risk_pct: float = Field(0.005, gt=0, le=0.03)
    stop_atr: float = Field(1.5, ge=0.3, le=10)
    target_r: float | None = Field(2.0, ge=0.3, le=20)
    max_trades_day: int = Field(10, ge=1, le=200)
    manage_manual: bool = False
    # Its cooldowns, in minutes. The floors are what the user confirmed for scalp-style trading (RISK.md
    # override 2026-11-01): no per-pair wait, and at least 5 minutes after a losing trade.
    cooldown_minutes: int = Field(0, ge=0, le=240)
    loss_cooldown_minutes: int = Field(5, ge=5, le=360)

    @model_validator(mode="after")
    def _rules_are_known(self) -> "StrategyIn":
        if self.timeframe not in engine.TF_SECONDS:
            raise ValueError(f"unknown timeframe '{self.timeframe}'")
        for name, allowed in (("entry_long", ("entry", "both")), ("entry_short", ("entry", "both")),
                              ("exit_rules", ("exit", "both"))):
            groups = [g for g in getattr(self, name) if g]          # an empty group is dropped
            if len(groups) > engine._MAX_GROUPS:
                raise ValueError(f"{name}: at most {engine._MAX_GROUPS} groups")
            for g in groups:
                if len(g) > engine._MAX_CONDITIONS:
                    raise ValueError(f"{name}: at most {engine._MAX_CONDITIONS} conditions in a group")
                for c in g:
                    spec = engine.CATALOG.get(c.type)
                    if spec is None:
                        raise ValueError(f"unknown condition '{c.type}'")
                    if spec.use not in allowed:
                        raise ValueError(f"'{c.type}' can't be used in {name}")
                    if c.tf not in engine.TF_SECONDS:
                        raise ValueError(f"unknown timeframe '{c.tf}'")
                    if spec.tfs and c.tf not in spec.tfs:
                        raise ValueError(f"'{c.type}' reads {' / '.join(spec.tfs)} only, not {c.tf}")
                    c.params = engine.clean_params(spec, c.params)
            setattr(self, name, groups)
        return self

    def groups(self, name: str) -> list:
        return [[c.model_dump() for c in g] for g in getattr(self, name)]

    def rules(self) -> engine.Rules:
        return engine.Rules(timeframe=self.timeframe, sides=self.sides, entry_long=self.groups("entry_long"),
                            entry_short=self.groups("entry_short"), mirror_short=self.mirror_short,
                            exit_rules=self.groups("exit_rules"), stop_atr=self.stop_atr)


class StrategyView(BaseModel):
    id: int
    name: str
    enabled: bool
    pairs: list[dict]
    timeframe: str
    sides: str
    entry_long: list
    entry_short: list
    mirror_short: bool
    exit_rules: list
    risk_pct: float
    stop_atr: float
    target_r: float | None
    max_trades_day: int
    manage_manual: bool
    cooldown_minutes: int = 0
    loss_cooldown_minutes: int = 5
    state: dict | None = None
    last_run_at: str | None = None
    last_result: str | None = None
    open_trades: int = 0
    trades_today: int = 0


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _view(session: Session, row: StrategyConfig) -> StrategyView:
    src = engine.strategy_source(row)
    open_n = len(session.scalars(select(Position).where(Position.source == src,
                                                        Position.status == PositionStatus.OPEN.value)).all())
    return StrategyView(
        id=row.id, name=row.name, enabled=row.enabled, pairs=list(row.pairs or []), timeframe=row.timeframe,
        sides=row.sides, entry_long=list(row.entry_long or []), entry_short=list(row.entry_short or []),
        mirror_short=row.mirror_short, exit_rules=list(row.exit_rules or []), risk_pct=row.risk_pct,
        stop_atr=row.stop_atr, target_r=row.target_r, max_trades_day=row.max_trades_day,
        manage_manual=row.manage_manual, cooldown_minutes=row.cooldown_minutes if row.cooldown_minutes is not None else 0,
        loss_cooldown_minutes=row.loss_cooldown_minutes if row.loss_cooldown_minutes is not None else 5,
        state=row.state, last_run_at=_iso(row.last_run_at),
        last_result=row.last_result, open_trades=open_n,
        trades_today=engine._trades_today(session, row, datetime.now(timezone.utc)),
    )


def _apply(row: StrategyConfig, req: StrategyIn) -> None:
    row.name = req.name.strip()
    row.pairs = [{"symbol": p.symbol, "asset_class": p.asset_class.value} for p in req.pairs]
    row.timeframe, row.sides, row.mirror_short = req.timeframe, req.sides, req.mirror_short
    row.entry_long, row.entry_short, row.exit_rules = (req.groups("entry_long"), req.groups("entry_short"),
                                                       req.groups("exit_rules"))
    row.risk_pct, row.stop_atr, row.target_r = req.risk_pct, req.stop_atr, req.target_r
    row.max_trades_day, row.manage_manual = req.max_trades_day, req.manage_manual
    row.cooldown_minutes, row.loss_cooldown_minutes = req.cooldown_minutes, req.loss_cooldown_minutes


def _get(session: Session, sid: int) -> StrategyConfig:
    row = session.get(StrategyConfig, sid)
    if row is None:
        raise HTTPException(status_code=404, detail="no such strategy")
    return row


@router.get("/catalog")
def strategy_catalog() -> dict:
    return {"timeframes": engine.TIMEFRAMES, "conditions": engine.catalog(),
            "max_groups": engine._MAX_GROUPS, "max_conditions": engine._MAX_CONDITIONS}


class CheckIn(BaseModel):
    config: StrategyIn
    symbol: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass = AssetClass.FOREX


@router.post("/check")
def strategy_check(req: CheckIn, session: Session = Depends(get_session)) -> dict:
    """What a draft's rules read on one pair right now. Reads candles and quotes; never trades."""
    return engine.check_now(session, req.config.rules(), req.symbol, req.asset_class.value)


@router.get("", response_model=list[StrategyView])
def list_strategies(session: Session = Depends(get_session)) -> list[StrategyView]:
    rows = session.scalars(select(StrategyConfig).order_by(StrategyConfig.id)).all()
    return [_view(session, r) for r in rows]


@router.post("", response_model=StrategyView)
def create_strategy(req: StrategyIn, session: Session = Depends(get_session)) -> StrategyView:
    row = StrategyConfig(enabled=False)      # saved stopped: starting it is its own, deliberate click
    _apply(row, req)
    session.add(row)
    session.commit()
    session.refresh(row)
    log.warning("strategy saved", extra={"strategy": row.id, "strategy_name": row.name})
    return _view(session, row)


@router.put("/{sid}", response_model=StrategyView)
def update_strategy(sid: int, req: StrategyIn, session: Session = Depends(get_session)) -> StrategyView:
    row = _get(session, sid)
    _apply(row, req)
    session.add(AgentRun(agent="strategy", event="edited", detail={"strategy_id": row.id, "name": row.name,
                                                                    "running": row.enabled}))
    session.commit()
    session.refresh(row)
    return _view(session, row)


@router.delete("/{sid}")
def delete_strategy(sid: int, session: Session = Depends(get_session)) -> dict:
    row = _get(session, sid)
    if row.enabled:
        raise HTTPException(status_code=409, detail="stop the strategy before deleting it")
    session.delete(row)
    session.commit()
    return {"deleted": sid}


class RunIn(BaseModel):
    on: bool


@router.post("/{sid}/run", response_model=StrategyView)
def run_strategy(sid: int, req: RunIn, session: Session = Depends(get_session)) -> StrategyView:
    row = _get(session, sid)
    if req.on:
        if not row.pairs:
            raise HTTPException(status_code=422, detail="add a pair for it to trade first")
        has_entry = (row.sides in ("both", "long") and row.entry_long) or \
                    (row.sides in ("both", "short") and (row.entry_long if row.mirror_short else row.entry_short))
        if not has_entry:
            raise HTTPException(status_code=422, detail="add a BUY or SELL rule first")
    row.enabled = req.on
    session.add(AgentRun(agent="strategy", event="started" if req.on else "stopped",
                         detail={"strategy_id": row.id, "name": row.name}))
    session.commit()
    session.refresh(row)
    log.warning("strategy started" if req.on else "strategy stopped", extra={"strategy": row.id})
    return _view(session, row)


@router.get("/{sid}/activity")
def strategy_activity(sid: int, limit: int = Query(40, ge=1, le=200),
                      session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(AgentRun).where(AgentRun.agent == "strategy")
                           .order_by(AgentRun.id.desc()).limit(1000)).all()
    out = []
    for r in rows:
        if (r.detail or {}).get("strategy_id") != sid:
            continue
        out.append({"id": r.id, "at": _iso(r.created_at), "symbol": r.symbol, "event": r.event,
                    "detail": r.detail})
        if len(out) >= limit:
            break
    return out
