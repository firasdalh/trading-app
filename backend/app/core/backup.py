"""A copy of the database every day, OUTSIDE OneDrive (2026-11-22, the full check the user asked for).

The database - the app's trade rows, the strategies and their state, the journal's repairs, the follow entries, the
learner's facts - is one SQLite file inside the OneDrive-synced Desktop, and there was no copy of it anywhere: a bad
write, a sync conflict or a deleted folder, and it is gone. Once a day (and a couple of minutes after the backend
starts) SQLite's own online backup copies it - consistent even while the app writes - to
%LOCALAPPDATA%\\AITradingDesk\\backups, which OneDrive does not sync. The newest KEEP copies are kept.

To restore: stop the app, copy a backup over backend\\trading.sqlite3 (and delete trading.sqlite3-wal / -shm), start it.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

KEEP = 14


def backup_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AITradingDesk" / "backups"


def db_path() -> Path | None:
    """The app's SQLite file; None when the database is not SQLite."""
    from app.core.database import engine

    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        return None
    return Path(engine.url.database).resolve()


def backup_now(src: Path | None = None, dest: Path | None = None, keep: int = KEEP) -> Path | None:
    """Copy the database to ``dest`` (backup_dir) as trading-<UTC time>.sqlite3 and keep the newest ``keep``.
    Returns the copy, or None when there is no SQLite file to copy."""
    src = src or db_path()
    if src is None or not src.exists():
        return None
    dest = dest or backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"trading-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3"
    part = out.with_suffix(".part")
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(part)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    part.replace(out)
    for old in sorted(dest.glob("trading-*.sqlite3"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return out
