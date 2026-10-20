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
            broker_map={"stock": "alpaca", "crypto": "alpaca", "forex": "oanda",
                        "metal": "oanda", "energy": "sim", "index": "sim"},
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
            loss_cooldown_minutes=cfg.default_loss_cooldown_minutes,
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
    # Keyed on LIVE, not on Mode C: Mode C was removed, so this always said False and the UI's
    # re-confirm button never showed - after a restart a live account could not be re-armed from the
    # app at all (real-money audit 2026-10-21).
    if settings.broker_env != "live":
        return False
    if settings.live_confirmed_at is None:
        return True
    confirmed = settings.live_confirmed_at
    if confirmed.tzinfo is None:
        confirmed = confirmed.replace(tzinfo=timezone.utc)
    return confirmed < PROCESS_START


def live_execution_allowed(settings: AppSettings) -> bool:
    """Whether orders may be sent to a REAL-money account right now.

    Callers ask only for a non-paper broker (``broker.is_paper`` False — e.g. the MT5 terminal
    logged into a real Exness account); paper/demo brokers never need it. A real account requires
    the typed live confirmation: ``broker_env`` switched to 'live' AND confirmed *after* this
    process started (re-confirm on every restart — safety requirement #7).

    A real account while the app is still set to 'paper' is BLOCKED. (It used to pass: logging the
    terminal into a real account with the app left on 'paper' let every opener trade real money
    with no confirmation, while the header still said PAPER.)
    """
    if settings.broker_env != "live" or settings.live_confirmed_at is None:
        return False
    confirmed = settings.live_confirmed_at
    if confirmed.tzinfo is None:
        confirmed = confirmed.replace(tzinfo=timezone.utc)
    return confirmed >= PROCESS_START


def kill_switch_active(session: Session) -> bool:
    """True if EITHER the env backstop OR the UI toggle is engaged."""
    if get_settings().kill_switch:
        return True
    settings = get_or_create_settings(session)
    return settings.kill_switch_engaged
