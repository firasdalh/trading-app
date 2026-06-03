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
