"""FastAPI application entrypoint.

Owns app lifespan: configure logging, create DB tables, seed singletons, start the agent
scheduler (a no-op stub until M4), and shut everything down cleanly.

Run locally:
    uvicorn app.main:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.backtest_routes import router as backtest_router
from app.api.journal_routes import router as journal_router
from app.api.market_routes import router as market_router
from app.api.proposal_routes import router as proposal_router
from app.api.routes import router
from app.api.settings_routes import router as settings_router
from app.api.watchlist_routes import router as watchlist_router
from app.api.ws import router as ws_router
from app.core.config import get_settings
from app.core.database import init_db, session_scope
from app.core.logging import configure_logging, get_logger
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.core.state import (
    get_or_create_risk_config,
    get_or_create_settings,
    kill_switch_active,
)

cfg = get_settings()
configure_logging(cfg.log_level)
log = get_logger("main")


def _reconcile_brokers(session) -> None:
    """On startup, reconcile each configured broker against local state (safety req #).

    Failures here are logged but never block startup — we surface the error instead of
    crashing the app.
    """
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.models.enums import AssetClass

    settings = get_or_create_settings(session)
    seen: set[str] = set()
    for asset_class in AssetClass:
        try:
            broker = get_broker_for(asset_class, settings.broker_map)
            if broker.name in seen:
                continue
            seen.add(broker.name)
            info = broker.reconcile()
            log.info("broker reconciled", extra=info)
        except Exception as exc:  # noqa: BLE001
            log.warning("broker reconcile failed", extra={"asset_class": asset_class.value, "error": str(exc)})


def _monitor_tick() -> None:
    """Scheduled position-monitor pass. Runs in its own session; never raises."""
    from app.core.database import session_scope
    from app.execution.monitor import monitor_positions

    try:
        with session_scope() as session:
            monitor_positions(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("monitor tick failed", extra={"error": str(exc)})


def _scan_tick() -> None:
    """Scheduled autonomous watchlist scan. Honors its own enabled flag + interval."""
    from app.agents.scanner import scan_tick
    from app.core.database import session_scope

    try:
        with session_scope() as session:
            scan_tick(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("scan tick failed", extra={"error": str(exc)})


def _advisor_tick() -> None:
    """Scheduled open-position advisor pass. Honors its own enabled flag + interval."""
    from app.agents.position_advisor import advisor_tick
    from app.core.database import session_scope

    try:
        with session_scope() as session:
            advisor_tick(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor tick failed", extra={"error": str(exc)})


def _register_monitor_job() -> None:
    from app.core.scheduler import get_scheduler

    sched = get_scheduler()
    sched.add_job(_monitor_tick, "interval", seconds=10, id="position_monitor",
                  replace_existing=True, max_instances=1)
    # Polls every 20s; the scanner itself enforces the user-configured interval.
    sched.add_job(_scan_tick, "interval", seconds=20, id="watchlist_scan",
                  replace_existing=True, max_instances=1)
    # Polls every 30s; the advisor enforces the user-configured auto-watch interval.
    sched.add_job(_advisor_tick, "interval", seconds=30, id="position_advisor",
                  replace_existing=True, max_instances=1)
    log.info("position monitor (10s) + watchlist scanner (20s) + advisor (30s poll) scheduled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "starting backend",
        extra={
            "app_env": cfg.app_env,
            "broker_env": cfg.broker_env,
            "env_kill_switch": cfg.kill_switch,
        },
    )
    init_db()
    with session_scope() as session:
        get_or_create_settings(session)
        get_or_create_risk_config(session)
        if kill_switch_active(session):
            log.warning("KILL SWITCH ACTIVE at startup — no new orders will be submitted")
        _reconcile_brokers(session)
    start_scheduler()
    _register_monitor_job()
    try:
        yield
    finally:
        shutdown_scheduler()
        log.info("backend stopped")


app = FastAPI(
    title="AI Multi-Agent Trading App",
    version="0.1.0",
    description="Decision-support and (optional) automation. Paper mode by default. "
    "Deterministic risk manager. Read RISK.md.",
    lifespan=lifespan,
)

# CORS for the Vite dev server (frontend arrives in M5).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(market_router)
app.include_router(proposal_router)
app.include_router(settings_router)
app.include_router(backtest_router)
app.include_router(journal_router)
app.include_router(watchlist_router)
app.include_router(ws_router)


@app.get("/", tags=["system"])
def root() -> dict:
    return {
        "name": "AI Multi-Agent Trading App",
        "version": "0.1.0",
        "docs": "/docs",
        "safety": "paper mode default; deterministic risk manager; kill-switch enabled",
    }
