"""Singleton/state helpers: seed and fetch AppSettings, RiskConfig, DailyRiskState.

Also tracks the process start time so Mode C (live auto-exec) can require
re-confirmation on every restart (safety requirement #7).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.db import AppSettings, DailyRiskState, RiskConfig

log = get_logger("state")

# Captured once at import time (process start). Used for live re-confirmation logic.
PROCESS_START = datetime.now(timezone.utc)


def get_or_create_settings(session: Session) -> AppSettings:
    settings = session.get(AppSettings, 1)
    if settings is None:
        cfg = get_settings()
        settings = AppSettings(
            id=1,
            execution_mode="A_PROPOSE_APPROVE",  # default per RISK.md
            broker_env=cfg.broker_env,            # paper by default
            broker_map={"stock": "alpaca", "crypto": "alpaca", "forex": "oanda", "metal": "oanda"},
            kill_switch_engaged=False,
        )
        session.add(settings)
        session.commit()
        log.info("seeded app_settings", extra={"execution_mode": settings.execution_mode})
    return settings


def get_or_create_risk_config(session: Session) -> RiskConfig:
    risk = session.get(RiskConfig, 1)
    if risk is None:
        cfg = get_settings()
        risk = RiskConfig(
            id=1,
            risk_per_trade=cfg.default_risk_per_trade,
            max_open_positions=cfg.default_max_open_positions,
            max_daily_loss=cfg.default_max_daily_loss,
            max_total_exposure=cfg.default_max_total_exposure,
            per_pair_cooldown_minutes=cfg.default_per_pair_cooldown_minutes,
        )
        session.add(risk)
        session.commit()
        log.info("seeded risk_config (RISK.md defaults)")
    return risk


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_or_create_daily_state(session: Session, starting_equity: float | None = None) -> DailyRiskState:
    date = today_str()
    state = session.scalar(select(DailyRiskState).where(DailyRiskState.trade_date == date))
    if state is None:
        state = DailyRiskState(trade_date=date, starting_equity=starting_equity)
        session.add(state)
        session.commit()
        log.info("created daily_risk_state", extra={"trade_date": date})
    return state


def live_reconfirm_required(settings: AppSettings) -> bool:
    """Mode C requires re-confirmation each restart: the stored confirmation must be
    *after* this process started, otherwise the user must confirm again."""
    if settings.execution_mode != "C_AUTO_LIVE":
        return False
    if settings.live_confirmed_at is None:
        return True
    confirmed = settings.live_confirmed_at
    if confirmed.tzinfo is None:
        confirmed = confirmed.replace(tzinfo=timezone.utc)
    return confirmed < PROCESS_START


def kill_switch_active(session: Session) -> bool:
    """True if EITHER the env backstop OR the UI toggle is engaged."""
    if get_settings().kill_switch:
        return True
    settings = get_or_create_settings(session)
    return settings.kill_switch_engaged
