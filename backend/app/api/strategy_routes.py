"""The Strategies board: build, check and run custom strategies (app/agents/strategy.py).

- GET  /api/strategies/catalog       -> the conditions the board offers, with their parameters.
- GET  /api/strategies               -> every saved strategy, with what it is doing now.
- POST /api/strategies               -> save a new one (always saved stopped).
- PUT  /api/strategies/{id}          -> change one (a running one picks it up on its next pass).
- DELETE /api/strategies/{id}        -> delete a stopped one.
- POST /api/strategies/{id}/run      -> start / stop it.
- POST /api/strategies/check         -> what a draft's rules read on a pair right now (reads only).
- GET  /api/strategies/{id}/activity -> what it opened, closed and was refused.
- GET  /api/strategies/activity      -> the same for every strategy, newest first (the desk's feed).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


RuleSet = Literal["entry_long", "entry_short", "exit_rules"]
RULE_SETS: tuple[RuleSet, ...] = ("entry_long", "entry_short", "exit_rules")


def off_groups_of(raw: dict | None) -> dict[str, list[int]]:
    """A strategy's switched-off groups with every rule set present, in order - as the board compares drafts."""
    return {name: sorted(set((raw or {}).get(name) or ())) for name in RULE_SETS}


class ConditionIn(BaseModel):
    type: str
    tf: str
    params: dict[str, float] = Field(default_factory=dict)


class PairIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass = AssetClass.FOREX


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class HoursIn(BaseModel):
    """Its trading hours: it opens trades from ``start`` up to ``end`` (24-hour, past midnight when end
    comes first) on the clock of ``tz`` - the board sends the browser's zone; none = this machine's."""
    start: str = Field(pattern=_HHMM)
    end: str = Field(pattern=_HHMM)
    tz: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _a_real_window(self) -> "HoursIn":
        if self.start == self.end:
            raise ValueError("trading hours: the start and the end are the same - pick 'all day' instead")
        if self.tz:
            try:
                ZoneInfo(self.tz)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"trading hours: unknown time zone '{self.tz}'") from None
        return self


class StopIn(BaseModel):
    """A stop that follows a SuperTrend line (engine.st_stop): on the ``tf`` SuperTrend (``period``,
    ``mult``), ``buffer`` x that timeframe's ATR beyond the line - room for a false break."""
    mode: Literal["supertrend"] = "supertrend"
    tf: str = "1m"
    period: int = Field(10, ge=3, le=50)
    mult: float = Field(2.7, ge=0.5, le=10)
    buffer: float = Field(0.5, ge=0, le=5)

    @model_validator(mode="after")
    def _a_known_timeframe(self) -> "StopIn":
        if self.tf not in engine.TF_SECONDS:
            raise ValueError(f"stop: unknown timeframe '{self.tf}'")
        return self


class StrategyIn(BaseModel):
    name: str = Field("My strategy", min_length=1, max_length=64)
    pairs: list[PairIn] = Field(default_factory=list, max_length=10)
    timeframe: str = "1m"
    sides: Literal["long", "short", "both"] = "both"
    entry_long: list[list[ConditionIn]] = Field(default_factory=list)
    entry_short: list[list[ConditionIn]] = Field(default_factory=list)
    mirror_short: bool = True
    exit_rules: list[list[ConditionIn]] = Field(default_factory=list)
    # Per trade, of equity: up to scalp mode's 6% ceiling (RISK.md override 2026-11-02), which the Risk
    # Manager re-clamps it to regardless.
    risk_pct: float = Field(0.005, gt=0, le=0.06)
    stop_atr: float = Field(1.5, ge=0.3, le=10)
    target_r: float | None = Field(2.0, ge=0.3, le=20)
    max_trades_day: int = Field(10, ge=1, le=200)
    manage_manual: bool = False
    # Its cooldowns, in minutes. The floors are what the user confirmed for scalp-style trading (RISK.md
    # override 2026-11-01): no per-pair wait, and at least 5 minutes after a losing trade.
    cooldown_minutes: int = Field(0, ge=0, le=240)
    loss_cooldown_minutes: int = Field(5, ge=5, le=360)
    hours: HoursIn | None = None          # None = it may open a trade at any hour
    stop: StopIn | None = None            # None = stop_atr x ATR
    # $ its closed trades make in a day before it stops opening until the next (engine.today_result).
    # It only ever stops trades, so there is no floor to keep; None = no target.
    daily_target: float | None = Field(None, gt=0, le=1_000_000)
    # The groups switched off, by place in each rule set: read for the board, never acted on.
    off_groups: dict[RuleSet, list[int]] = Field(default_factory=dict)
    # It trades a REAL account too, not only a demo (RISK.md 2026-11-04). Off by default.
    live: bool = False

    @model_validator(mode="after")
    def _rules_are_known(self) -> "StrategyIn":
        if self.timeframe not in engine.TF_SECONDS:
            raise ValueError(f"unknown timeframe '{self.timeframe}'")
        off = {name: set(self.off_groups.get(name, ())) for name in RULE_SETS}
        for name, allowed in (("entry_long", ("entry", "both")), ("entry_short", ("entry", "both")),
                              ("exit_rules", ("exit", "both"))):
            # An empty group is dropped, and a group switched off stays off at its new place.
            kept = [i for i, g in enumerate(getattr(self, name)) if g]
            groups = [getattr(self, name)[i] for i in kept]
            self.off_groups[name] = [j for j, i in enumerate(kept) if i in off[name]]
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
                            exit_rules=self.groups("exit_rules"), stop_atr=self.stop_atr,
                            hours=self.hours.model_dump() if self.hours else None,
                            stop=self.stop.model_dump() if self.stop else None, off=self.off_groups)


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
    hours: dict | None = None
    stop: dict | None = None              # a SuperTrend stop (StopIn); None = stop_atr x ATR
    hours_now: dict | None = None         # engine.hours_now: may it open a trade now, and until when
    # symbol -> when its re-entry wait after the last close ends (engine.cooldown_until), pairs still waiting only
    cooldown_until: dict[str, str] = {}
    daily_target: float | None = None
    off_groups: dict[str, list[int]] = {}   # every rule set's switched-off groups (off_groups_of)
    live: bool = False                    # it trades a real account too
    pnl_today: float = 0.0                # what the trades it closed today made (engine.today_result)
    target_hit: dict | None = None        # {"at", "pnl"}: it reached its daily target today - no new trades
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
    now = datetime.now(timezone.utc)
    waits = {p.get("symbol"): engine.cooldown_until(session, row, p.get("symbol") or "") for p in row.pairs or []}
    day = engine.today_result(session, row, now)
    return StrategyView(
        id=row.id, name=row.name, enabled=row.enabled, pairs=list(row.pairs or []), timeframe=row.timeframe,
        sides=row.sides, entry_long=list(row.entry_long or []), entry_short=list(row.entry_short or []),
        mirror_short=row.mirror_short, exit_rules=list(row.exit_rules or []), risk_pct=row.risk_pct,
        stop_atr=row.stop_atr, target_r=row.target_r, max_trades_day=row.max_trades_day,
        manage_manual=row.manage_manual, cooldown_minutes=row.cooldown_minutes if row.cooldown_minutes is not None else 0,
        loss_cooldown_minutes=row.loss_cooldown_minutes if row.loss_cooldown_minutes is not None else 5,
        hours=row.hours or None, hours_now=engine.hours_now(row.hours, now), stop=row.stop or None,
        cooldown_until={sym: _iso(t) for sym, t in waits.items() if sym and t and t > now},
        daily_target=row.daily_target, off_groups=off_groups_of(row.off_groups), live=bool(row.live),
        pnl_today=day["pnl"],
        target_hit={"at": _iso(day["hit_at"]), "pnl": day["pnl"]} if day["hit_at"] else None,
        state=row.state, last_run_at=_iso(row.last_run_at),
        last_result=row.last_result, open_trades=open_n,
        trades_today=engine._trades_today(session, row, now),
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
    row.stop = req.stop.model_dump() if req.stop else None
    row.hours = req.hours.model_dump() if req.hours else None
    row.daily_target = req.daily_target
    row.off_groups = off_groups_of(req.off_groups)
    row.live = req.live


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


# The engine logs what IT does. A trade that ends at its broker-side stop or target, or that is closed by
# hand, never passes through it - 63 of the 95 closes on 2026-11-02 - so those closes are read from the
# trades themselves and merged into the feed, with how each one ended.
_SAME_CLOSE_S = 90      # a close the engine logged and a trade's close time this near = the same close


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _exit_kind(p: Position) -> str:
    """How a strategy's trade ended when the strategy did not close it: "stop" (the price reached or
    passed its stop), "target", or "other" (by hand, the kill-switch, another tool)."""
    x, sl, tp = p.last_price, p.stop_loss, p.take_profit
    if x is None:
        return "other"
    s = 1 if p.direction == "long" else -1
    tol = max(0.1 * abs(p.entry_price - sl) if sl else 0.0, 0.0002 * abs(p.entry_price))
    if tp and s * (x - tp) >= -tol:
        return "target"
    if sl and s * (x - sl) <= tol:
        return "stop"
    return "other"


def _feed(session: Session, sid: int | None, limit: int) -> list[dict]:
    """What one strategy (``sid``), or every strategy, did - newest first: the engine's own events (opened,
    closed by its rules, refused...) plus the closes of its trades that did not go through it."""
    configs = session.scalars(select(StrategyConfig)).all()
    names = {c.id: c.name for c in configs}
    classes = {p.get("symbol"): p.get("asset_class") for c in configs for p in (c.pairs or [])}
    rows = session.scalars(select(AgentRun).where(AgentRun.agent == "strategy")
                           .order_by(AgentRun.id.desc()).limit(1000)).all()
    items = [(_utc(r.created_at), {"id": r.id, "at": _iso(r.created_at), "symbol": r.symbol,
                                   "asset_class": classes.get(r.symbol), "event": r.event, "detail": r.detail})
             for r in rows if sid is None or (r.detail or {}).get("strategy_id") == sid][:limit]
    mine = Position.source == f"strategy:{sid}" if sid is not None else Position.source.like("strategy:%")
    done = session.scalars(select(Position).where(Position.status == PositionStatus.CLOSED.value,
                                                  Position.closed_at.is_not(None), mine)
                           .order_by(Position.closed_at.desc()).limit(limit)).all()
    if done:
        since = min(_utc(p.closed_at) for p in done) - timedelta(seconds=_SAME_CLOSE_S)
        logged = [(r.symbol, (r.detail or {}).get("side"), _utc(r.created_at)) for r in session.scalars(
            select(AgentRun).where(AgentRun.agent == "strategy", AgentRun.event == "closed",
                                   AgentRun.created_at >= since)).all()]
        for p in done:
            at = _utc(p.closed_at)
            if any(sym == p.symbol and side == p.direction and abs((at - t).total_seconds()) <= _SAME_CLOSE_S
                   for sym, side, t in logged):
                continue                  # the engine closed it: already in the feed, with its reasons
            owner = p.source.split(":", 1)[1]
            owner = int(owner) if owner.isdigit() else None
            items.append((at, {"id": -p.id, "at": _iso(p.closed_at), "symbol": p.symbol,
                               "asset_class": p.asset_class, "event": "closed",
                               "detail": {"strategy_id": owner, "name": names.get(owner), "side": p.direction,
                                          "by": _exit_kind(p), "pnl": p.realized_pnl, "lots": p.qty,
                                          "entry": p.entry_price, "exit": p.last_price, "stop": p.stop_loss,
                                          "target": p.take_profit}}))
    items.sort(key=lambda it: it[0], reverse=True)
    return [e for _, e in items[:limit]]


@router.get("/activity")
def all_activity(limit: int = Query(40, ge=1, le=200), session: Session = Depends(get_session)) -> list[dict]:
    """Every strategy's activity, newest first - the desk's feed."""
    return _feed(session, None, limit)


@router.get("/{sid}/activity")
def strategy_activity(sid: int, limit: int = Query(40, ge=1, le=200),
                      session: Session = Depends(get_session)) -> list[dict]:
    return _feed(session, sid, limit)
