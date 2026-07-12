"""FastAPI application entrypoint.

Owns app lifespan: configure logging, create DB tables, seed singletons, start the agent
scheduler (a no-op stub until M4), and shut everything down cleanly.

Run locally:
    uvicorn app.main:app --reload
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.auto_trade_routes import router as auto_trade_router
from app.api.backtest_routes import router as backtest_router
from app.api.conditional_routes import router as conditional_router
from app.api.rsi_over_routes import router as rsi_over_router
from app.api.journal_routes import router as journal_router
from app.api.market_routes import router as market_router
from app.api.proposal_routes import router as proposal_router
from app.api.routes import router
from app.api.settings_routes import router as settings_router
from app.api.shadow_routes import router as shadow_router
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


def _hybrid_tick() -> None:
    """Scheduled Hybrid auto-pilot pass. Honors its own enabled flag + 30-45 min interval."""
    from app.agents.hybrid import hybrid_tick
    from app.core.database import session_scope

    try:
        with session_scope() as session:
            hybrid_tick(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("hybrid tick failed", extra={"error": str(exc)})


def _rsi_over_tick() -> None:
    """Scheduled RSI-Over auto-watch pass. Honors its own enabled flag + interval."""
    from app.agents.rsi_over import rsi_over_tick
    from app.core.database import session_scope

    try:
        with session_scope() as session:
            rsi_over_tick(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("rsi-over tick failed", extra={"error": str(exc)})


def _auto_trade_tick() -> None:
    """Scheduled per-pair AI auto-trader pass. Honors its own enabled flag + interval (default 15m)."""
    from app.agents.auto_trade import auto_trade_tick
    from app.core.database import session_scope

    try:
        with session_scope() as session:
            auto_trade_tick(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-trade tick failed", extra={"error": str(exc)})


def _conditional_tick() -> None:
    """Watch armed conditional setups: expire stale ones, and on a confirmed trigger re-check +
    open. Its own job/session so a firing setup (which runs the full analysis) never delays the
    position monitor's stop/target management."""
    from app.agents.conditional import check_conditional_setups
    from app.core.database import session_scope

    try:
        with session_scope() as session:
            check_conditional_setups(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("conditional tick failed", extra={"error": str(exc)})


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
    # Polls every 60s; Hybrid auto-pilot enforces its own 30-45 min interval.
    sched.add_job(_hybrid_tick, "interval", seconds=60, id="hybrid_autopilot",
                  replace_existing=True, max_instances=1)
    # Watches armed conditional setups every 15s (its own job so a firing trigger never stalls the
    # position monitor). Only does real work when a trigger is hit.
    sched.add_job(_conditional_tick, "interval", seconds=15, id="conditional_watch",
                  replace_existing=True, max_instances=1)
    # Polls every 60s; the RSI-Over auto-watch enforces its own interval (default 15 min).
    sched.add_job(_rsi_over_tick, "interval", seconds=60, id="rsi_over_watch",
                  replace_existing=True, max_instances=1)
    # Polls every 60s; the per-pair AI auto-trader enforces its own interval (default 15 min).
    sched.add_job(_auto_trade_tick, "interval", seconds=60, id="auto_trade",
                  replace_existing=True, max_instances=1)
    log.info("position monitor (10s) + scanner (20s) + advisor (30s) + hybrid (60s) + "
             "conditional watch (15s) + rsi-over watch (60s) scheduled")


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
app.include_router(conditional_router)
app.include_router(watchlist_router)
app.include_router(shadow_router)
app.include_router(rsi_over_router)
app.include_router(auto_trade_router)
app.include_router(ws_router)


# --- serve the built React UI from this same server (desktop app / single-origin) ---
# The frontend already uses relative /api, /ws, /health, so serving its build here means the whole
# app runs on ONE port — what the desktop launcher points its window at. No-op in dev (dist not
# built): the Vite dev server on :5173 proxies /api back here instead.
# In a PyInstaller bundle the built UI is shipped under sys._MEIPASS; in dev it's at repo
# root/frontend/dist (computed from this file's location).
if getattr(sys, "frozen", False):
    _DIST = os.path.join(sys._MEIPASS, "frontend", "dist")  # type: ignore[attr-defined]
else:
    _DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "frontend", "dist")
_INDEX = os.path.join(_DIST, "index.html")
_HAS_UI = os.path.isfile(_INDEX)
_RESERVED = {"api", "ws", "health", "docs", "redoc", "openapi.json"}

_API_INFO = {
    "name": "AI Multi-Agent Trading App",
    "version": "0.1.0",
    "docs": "/docs",
    "safety": "paper mode default; deterministic risk manager; kill-switch enabled",
}


@app.get("/", include_in_schema=False)
def root():
    # Built UI when present (desktop / single-origin); the API info JSON otherwise (dev / API-only).
    return FileResponse(_INDEX) if _HAS_UI else _API_INFO


if _HAS_UI:
    _assets = os.path.join(_DIST, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    # SPA catch-all — registered LAST so /api, /ws, /health, /docs keep their handlers. Serves a
    # real static file (favicon, etc.) if it exists, otherwise index.html for client-side routing.
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.split("/", 1)[0] in _RESERVED:
            raise HTTPException(status_code=404, detail="not found")
        candidate = os.path.normpath(os.path.join(_DIST, full_path))
        if full_path and candidate.startswith(_DIST) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(_INDEX)
