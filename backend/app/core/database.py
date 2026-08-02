"""Database engine + session management (SQLAlchemy 2.0).

SQLite is the local-dev default; set ``DATABASE_URL`` to a Postgres DSN for production.
The schema is created on startup via ``init_db`` (a migration tool like Alembic can be
layered on later — kept out of M1 to stay lean).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.db import Base

_settings = get_settings()

# SQLite needs check_same_thread=False to be used across FastAPI's threadpool.
_connect_args = {"check_same_thread": False} if _settings.is_sqlite else {}

engine = create_engine(
    _settings.database_url,
    echo=False,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Columns added after a table's first release — applied to pre-existing SQLite DBs so we
# don't need a full migration tool for local dev. (Postgres deployments should use Alembic.)
_SQLITE_ADDED_COLUMNS = {
    "trade_proposals": [
        ("review_decision", "VARCHAR(16)"),
        ("watch", "BOOLEAN DEFAULT 0"),
        ("source", "VARCHAR(24) DEFAULT 'analysis'"),
    ],
    "advisor_config": [
        ("auto_execute", "BOOLEAN DEFAULT 0"),
        ("max_hold_hours", "FLOAT DEFAULT 0"),
    ],
    "risk_config": [
        ("daily_loss_breaker_enabled", "BOOLEAN DEFAULT 1"),
        ("loss_cooldown_minutes", "INTEGER DEFAULT 180"),
        ("max_trades_per_day", "INTEGER DEFAULT 0"),
        ("max_consecutive_losses", "INTEGER DEFAULT 0"),
        ("breaker_cooldown_minutes", "INTEGER DEFAULT 120"),
        ("perf_breaker_enabled", "BOOLEAN DEFAULT 0"),
        ("min_expectancy_r", "FLOAT DEFAULT -0.2"),
        ("expectancy_window", "INTEGER DEFAULT 10"),
        ("spread_gate_enabled", "BOOLEAN DEFAULT 1"),
        ("max_spread_r_fraction", "FLOAT DEFAULT 0.25"),
    ],
    "auto_trade_config": [
        ("min_rr", "FLOAT DEFAULT 1.2"),
        ("min_profit_usd", "FLOAT DEFAULT 20.0"),
        ("last_results", "JSON"),
        ("strategy", "VARCHAR DEFAULT 'scenario'"),
        ("timeframe", "VARCHAR DEFAULT '1h'"),
    ],
    "app_settings": [
        ("trend_only_mode", "BOOLEAN DEFAULT 1"),
        ("st_band_mode", "BOOLEAN DEFAULT 0"),
        ("ai_momentum_read", "BOOLEAN DEFAULT 1"),
        ("ai_regime_read", "BOOLEAN DEFAULT 1"),
        ("ai_priceaction_read", "BOOLEAN DEFAULT 1"),
        ("ai_review_enabled", "BOOLEAN DEFAULT 0"),
        ("journal_reset_at", "DATETIME"),
        ("disabled_filters", "TEXT"),
    ],
    "watch_items": [
        ("recommended", "BOOLEAN DEFAULT 0"),
    ],
    "positions": [
        ("confidence", "FLOAT"),
        ("source", "VARCHAR(24)"),
    ],
    "hybrid_config": [
        ("conditional_enabled", "BOOLEAN DEFAULT 1"),
        ("max_armed", "INTEGER DEFAULT 3"),
    ],
    "conditional_setups": [
        ("cooldown_until", "DATETIME"),
        ("retries", "INTEGER DEFAULT 0"),
        ("desired_lots", "FLOAT"),
    ],
    "rsi_over_config": [
        ("macd", "BOOLEAN DEFAULT 0"),
        ("rsi_div", "BOOLEAN DEFAULT 0"),
        ("rej_candle", "BOOLEAN DEFAULT 0"),
        ("at_level", "BOOLEAN DEFAULT 0"),
        ("pa_confirm", "BOOLEAN DEFAULT 0"),
        ("trend_filter", "BOOLEAN DEFAULT 1"),
        ("auto_approve", "BOOLEAN DEFAULT 0"),
        ("last_scan_at", "DATETIME"),
        ("last_scanned", "INTEGER DEFAULT 0"),
        ("last_candidates", "TEXT"),
    ],
}


def _migrate_sqlite() -> None:
    if not _settings.is_sqlite:
        return
    with engine.begin() as conn:
        for table, cols in _SQLITE_ADDED_COLUMNS.items():
            try:
                existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            except Exception:
                continue
            if not existing:  # table doesn't exist yet (create_all will make it fresh)
                continue
            for name, ddl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db() -> None:
    """Create all tables, then add any columns missing on a pre-existing DB. Idempotent."""
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for use outside request handlers (scheduler, scripts).

    Commits on success, rolls back on exception.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
