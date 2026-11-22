"""The daily database copy outside OneDrive (2026-11-22): a consistent copy, the newest KEEP kept."""
from __future__ import annotations

import sqlite3

from app.core import backup


def _db(path):
    c = sqlite3.connect(path)
    c.execute("create table t (x integer)")
    c.executemany("insert into t values (?)", [(i,) for i in range(100)])
    c.commit()
    c.close()


def test_a_copy_is_made_and_reads_the_same(tmp_path):
    src = tmp_path / "trading.sqlite3"
    _db(src)
    out = backup.backup_now(src, tmp_path / "backups")
    assert out is not None and out.exists() and out.name.startswith("trading-") and out.suffix == ".sqlite3"
    assert sqlite3.connect(out).execute("select count(*), sum(x) from t").fetchone() == (100, 4950)
    assert not list((tmp_path / "backups").glob("*.part"))


def test_only_the_newest_are_kept(tmp_path):
    src = tmp_path / "trading.sqlite3"
    _db(src)
    dest = tmp_path / "backups"
    dest.mkdir()
    for i in range(5):
        (dest / f"trading-2026010{i}-000000.sqlite3").write_bytes(b"old")
    backup.backup_now(src, dest, keep=3)
    names = sorted(p.name for p in dest.glob("trading-*.sqlite3"))
    assert len(names) == 3 and names[-1] > "trading-20260104-000000.sqlite3"


def test_no_database_no_copy(tmp_path):
    assert backup.backup_now(tmp_path / "missing.sqlite3", tmp_path / "b") is None


def test_the_default_folder_is_outside_onedrive(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert backup.backup_dir() == tmp_path / "AITradingDesk" / "backups"
