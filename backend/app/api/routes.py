"""HTTP API routes (Milestone 1 surface).

Endpoints here are intentionally minimal: health, settings (read), risk state (read), and
the kill-switch toggle. Trading/agent routes arrive in later milestones. WebSocket handlers
live in ``app.api.ws`` (added in M5).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import (
    get_or_create_daily_state,
    get_or_create_risk_config,
    get_or_create_settings,
    kill_switch_active,
    live_reconfirm_required,
)
from app.models.schemas import (
    AppSettingsView,
    HealthResponse,
    RiskConfigView,
    RiskDecision,
    RiskStateView,
    SettingsResponse,
    TradeProposal,
)
from app.risk.service import assess, realized_today, total_unrealized

log = get_logger("api")
router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(session: Session = Depends(get_session)) -> HealthResponse:
    cfg = get_settings()
    return HealthResponse(
        status="ok",
        app_env=cfg.app_env,
        broker_env=cfg.broker_env,
        kill_switch=kill_switch_active(session),
        time=datetime.now(timezone.utc),
    )


def build_settings_response(session: Session) -> SettingsResponse:
    cfg = get_settings()
    app_settings = get_or_create_settings(session)
    risk = get_or_create_risk_config(session)
    return SettingsResponse(
        app=AppSettingsView.model_validate(app_settings),
        risk=RiskConfigView.model_validate(risk),
        env_kill_switch=cfg.kill_switch,
        env_broker_env=cfg.broker_env,
        live_re_confirm_required=live_reconfirm_required(app_settings),
    )


@router.get("/api/settings", response_model=SettingsResponse, tags=["settings"])
def read_settings(session: Session = Depends(get_session)) -> SettingsResponse:
    return build_settings_response(session)


@router.get("/api/risk/state", response_model=RiskStateView, tags=["risk"])
def read_risk_state(session: Session = Depends(get_session)) -> RiskStateView:
    risk = get_or_create_risk_config(session)
    state = get_or_create_daily_state(session)
    limit_amount = (
        state.starting_equity * risk.max_daily_loss
        if state.starting_equity is not None
        else None
    )
    # Broker-truth realized today (counts terminal-side closes); falls back to app-tracked.
    realized = realized_today(session)
    # Exposure = sum of risk-at-entry across app-tracked open positions.
    from sqlalchemy import select
    from app.models.db import Position
    from app.models.enums import PositionStatus

    open_rows = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()
    total_risk = round(sum((p.risk_amount or 0.0) for p in open_rows), 2)
    # The pause only blocks while the breaker is armed; with it OFF the badge/banner shouldn't claim
    # "paused" (the manager already skips the veto). The stored flag is kept for the new UTC day reset.
    effective_paused = state.trading_paused and risk.daily_loss_breaker_enabled
    return RiskStateView(
        trade_date=state.trade_date,
        starting_equity=state.starting_equity,
        realized_pnl=realized,
        unrealized_pnl=total_unrealized(session),
        total_risk_amount=total_risk,
        trades_count=state.trades_count,
        trading_paused=effective_paused,
        pause_reason=state.pause_reason if effective_paused else None,
        max_daily_loss=risk.max_daily_loss,
        daily_loss_limit_amount=limit_amount,
        daily_loss_breaker_enabled=risk.daily_loss_breaker_enabled,
    )


@router.post("/api/risk/resume", response_model=RiskStateView, tags=["risk"])
def resume_trading(session: Session = Depends(get_session)) -> RiskStateView:
    """Manually clear today's daily-loss trading pause (the 'Resume trading' button). The breaker
    stays armed and can pause again if the day's realized loss hits the limit later today."""
    state = get_or_create_daily_state(session)
    state.trading_paused = False
    state.pause_reason = None
    session.commit()
    return read_risk_state(session)


@router.get("/api/decisions", tags=["decisions"])
def list_decisions(
    symbol: str | None = None,
    gate: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Structured decision log (Task 12): every funnel evaluation incl. 'no trade', with the deciding
    gate, indicator values, confidence and AI verdict. Answers 'why didn't it fire on this setup?'.
    Filter by ``symbol`` and/or ``gate`` (e.g. regime_not_trending, mtf_conflict, ai_veto, approved)."""
    from sqlalchemy import select

    from app.models.db import AgentRun

    limit = max(1, min(limit, 500))
    q = select(AgentRun).where(AgentRun.agent == "decision")
    if symbol:
        q = q.where(AgentRun.symbol == symbol)
    rows = session.scalars(q.order_by(AgentRun.id.desc()).limit(limit * 3 if gate else limit)).all()
    out = []
    for r in rows:
        d = dict(r.detail or {})
        if gate and d.get("gate") != gate:
            continue
        d["symbol"] = r.symbol
        d["at"] = r.created_at.isoformat() if r.created_at else None
        out.append(d)
        if len(out) >= limit:
            break
    return out


@router.post("/api/risk/preview", response_model=RiskDecision, tags=["risk"])
def preview_risk(
    proposal: TradeProposal,
    session: Session = Depends(get_session),
) -> RiskDecision:
    """Run a proposal through the DETERMINISTIC Risk Manager without executing anything.

    Returns the approved (possibly resized) size or a veto with reason. This is the same
    gate the agent pipeline (M4) uses; exposing it lets the UI show sizing before approval.
    """
    return assess(session, proposal)


@router.post("/api/kill-switch/{engage}", tags=["safety"])
def set_kill_switch(engage: bool, session: Session = Depends(get_session)) -> dict:
    """Engage/disengage the UI kill-switch. Halts all NEW orders when engaged.

    Note: the env ``KILL_SWITCH`` is a separate backstop and cannot be cleared from the UI.
    """
    settings = get_or_create_settings(session)
    settings.kill_switch_engaged = engage
    session.commit()
    log.warning("kill_switch toggled", extra={"engaged": engage})
    return {
        "kill_switch_engaged": settings.kill_switch_engaged,
        "env_kill_switch": get_settings().kill_switch,
        "effective": kill_switch_active(session),
    }
