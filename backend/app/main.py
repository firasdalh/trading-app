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

from app.api.routes import router
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
    start_scheduler()
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


@app.get("/", tags=["system"])
def root() -> dict:
    return {
        "name": "AI Multi-Agent Trading App",
        "version": "0.1.0",
        "docs": "/docs",
        "safety": "paper mode default; deterministic risk manager; kill-switch enabled",
    }
