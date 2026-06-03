"""Resolve MT5 connection credentials: UI-entered (DB) first, then .env fallback.

Returning ``login == 0`` means "attach to the account the running terminal is already
logged into" — no credentials needed.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.db import Mt5Credentials

log = get_logger("broker.mt5.creds")


def resolve_mt5_credentials() -> dict:
    # 1. UI-entered credentials in the DB take priority.
    try:
        with session_scope() as session:
            row = session.get(Mt5Credentials, 1)
            if row and (row.login or row.server or row.path):
                return {
                    "login": int(row.login or 0),
                    "password": row.password or "",
                    "server": row.server or "",
                    "path": row.path or "",
                }
    except Exception as exc:  # noqa: BLE001 - DB not ready / table missing
        log.warning("mt5 creds DB lookup failed; using env", extra={"error": str(exc)})

    # 2. Fall back to .env.
    cfg = get_settings()
    return {
        "login": int(cfg.mt5_login or 0),
        "password": cfg.mt5_password,
        "server": cfg.mt5_server,
        "path": cfg.mt5_path,
    }
