"""Tests for the autonomous watchlist scanner."""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.scanner as scanner
from app.agents.scanner import get_or_create_scan_config, run_scan, scan_tick
from app.core.state import get_or_create_settings
from app.models.db import Position, WatchItem
from app.models.enums import ExecutionMode, PositionStatus
from app.models.schemas import PositionAdvice


def _add(session, symbol="AAPL", asset="stock", tf="1h"):
    session.add(WatchItem(symbol=symbol, asset_class=asset, timeframe=tf, enabled=True))
    session.commit()


def test_scan_empty_watchlist(db_session):
    res = run_scan(db_session)
    assert res["scanned"] == 0


def test_scan_analyzes_each_pair(db_session):
    _add(db_session, "AAPL", "stock", "1h")
    _add(db_session, "MSFT", "stock", "1h")
    res = run_scan(db_session)
    assert res["scanned"] == 2
    # Every result is either a status (analyzed) or a skip/error reason.
    assert all(("status" in r or "skipped" in r or "error" in r) for r in res["results"])


def test_scan_skips_pair_with_open_position(db_session):
    _add(db_session, "AAPL", "stock", "1h")
    db_session.add(Position(symbol="AAPL", asset_class="stock", direction="long", qty=1,
                            entry_price=100, status=PositionStatus.OPEN.value, last_price=100))
    db_session.commit()
    res = run_scan(db_session)
    assert res["results"][0].get("skipped") == "position open"


def _advice(severity="warn"):
    return PositionAdvice(symbol="XAUUSDm", direction="short", unrealized_pnl=10.0, has_stop=True,
                          severity=severity, headline="XAUUSDm is winning into US: ISM (~30m)",
                          detail="Lock it in...", event_label="US: ISM")


def test_scan_records_advisories_in_autonomous_mode(db_session, monkeypatch):
    settings = get_or_create_settings(db_session)
    settings.execution_mode = ExecutionMode.B_AUTO_PAPER.value
    db_session.commit()
    monkeypatch.setattr(scanner, "advise_positions", lambda session: [_advice("warn")])
    res = run_scan(db_session)
    assert len(res["advisories"]) == 1
    assert res["advisories"][0]["severity"] == "warn"


def test_scan_suppresses_advisories_in_mode_a(db_session, monkeypatch):
    # Mode A surfaces advice live on the dashboard, so the scan loop stays quiet.
    settings = get_or_create_settings(db_session)
    settings.execution_mode = ExecutionMode.A_PROPOSE_APPROVE.value
    db_session.commit()
    monkeypatch.setattr(scanner, "advise_positions", lambda session: [_advice("danger")])
    res = run_scan(db_session)
    assert res["advisories"] == []


def test_scan_tick_respects_disabled(db_session):
    cfg = get_or_create_scan_config(db_session)
    cfg.enabled = False
    db_session.commit()
    assert scan_tick(db_session)["ran"] is False


def test_scan_tick_runs_when_enabled(db_session):
    _add(db_session, "AAPL", "stock", "1h")
    cfg = get_or_create_scan_config(db_session)
    cfg.enabled = True
    cfg.interval_seconds = 60
    db_session.commit()
    first = scan_tick(db_session)
    assert first["ran"] is True
    # Immediately after, the interval hasn't elapsed -> it should not run again.
    second = scan_tick(db_session)
    assert second["ran"] is False and second["reason"] == "interval not elapsed"
