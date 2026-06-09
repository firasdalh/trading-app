"""Autonomous watchlist scanner.

Periodically runs the analysis pipeline over the enabled watchlist pairs. Behaviour depends
on the execution mode (handled downstream in the pipeline):
- Mode A: produces proposals queued for the user's approval.
- Modes B/C: risk-approved proposals auto-execute (paper / live-gated).
Open/close of positions at stop/target is handled by the Monitor; this loop is about
spotting and acting on new setups.

To avoid noise and stacking, a pair is skipped when it already has an open position or a
proposal awaiting approval.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pipeline import analyze_symbol
from app.agents.position_advisor import advise_positions
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.db import AgentRun, Position, ScanConfig, TradeProposalRecord, WatchItem
from app.models.enums import AssetClass, ExecutionMode, PositionStatus, ProposalStatus

log = get_logger("agents.scanner")


def _scan_open_positions(session: Session) -> list[dict]:
    """In autonomous modes the user isn't watching the dashboard, so the scan loop becomes the
    'eyes' on open trades: compute management advisories and record the actionable ones (e.g.
    protect a winner / cut a loser ahead of news). Advisory only — it never closes or modifies
    a position; the Monitor handles stop/target exits and the user acts on alerts."""
    mode = get_or_create_settings(session).execution_mode
    if mode == ExecutionMode.A_PROPOSE_APPROVE.value:
        return []  # Mode A: the live advisor panel already surfaces this on the dashboard.

    actionable: list[dict] = []
    for a in advise_positions(session):
        if a.severity in ("warn", "danger"):
            actionable.append({"symbol": a.symbol, "severity": a.severity,
                               "headline": a.headline, "detail": a.detail,
                               "event": a.event_label})
            log.warning("position advisory", extra={"symbol": a.symbol, "severity": a.severity,
                                                     "advice": a.headline})
    return actionable


# Pending proposals older than this many BARS of their own timeframe are stale (price has moved,
# the entry/stop are no longer valid) -> expire them so they stop blocking the scanner/Hybrid.
_STALE_BARS = 2
_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def expire_stale_proposals(session: Session) -> int:
    """Mark PENDING_APPROVAL proposals older than ~2 bars of their timeframe as EXPIRED.

    The scanner and Hybrid skip any symbol that has a pending proposal (to avoid duplicates), so
    stale pendings that never get approved/rejected would otherwise block those symbols forever.
    """
    now = datetime.now(timezone.utc)
    rows = session.scalars(
        select(TradeProposalRecord).where(
            TradeProposalRecord.status == ProposalStatus.PENDING_APPROVAL.value
        )
    ).all()
    expired = 0
    for r in rows:
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        max_age = _STALE_BARS * _TF_MINUTES.get(r.timeframe, 60) * 60
        if (now - created).total_seconds() > max_age:
            r.status = ProposalStatus.EXPIRED.value
            expired += 1
    if expired:
        session.commit()
        log.info("expired stale pending proposals", extra={"count": expired})
    return expired


def get_or_create_scan_config(session: Session) -> ScanConfig:
    cfg = session.get(ScanConfig, 1)
    if cfg is None:
        cfg = ScanConfig(id=1, enabled=False, interval_seconds=120)
        session.add(cfg)
        session.commit()
    return cfg


def run_scan(session: Session) -> dict:
    """Analyze every enabled watch item once. Returns a small summary."""
    expire_stale_proposals(session)
    items = session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()

    open_syms = {
        p.symbol for p in session.scalars(
            select(Position).where(Position.status == PositionStatus.OPEN.value)
        )
    }
    pending_syms = {
        r.symbol for r in session.scalars(
            select(TradeProposalRecord).where(
                TradeProposalRecord.status == ProposalStatus.PENDING_APPROVAL.value
            )
        )
    }

    results = []
    for it in items:
        if it.symbol in open_syms:
            results.append({"symbol": it.symbol, "skipped": "position open"})
            continue
        if it.symbol in pending_syms:
            results.append({"symbol": it.symbol, "skipped": "proposal pending approval"})
            continue
        try:
            # Scanner stays deterministic so the recurring loop never burns LLM quota;
            # manual "Run analysis" uses the LLM. (Flip use_llm=True here if on a paid plan.)
            res = analyze_symbol(session, it.symbol, AssetClass(it.asset_class), it.timeframe,
                                 use_llm=False)
            results.append({"symbol": it.symbol, "status": res.status,
                            "direction": res.proposal.direction.value})
        except Exception as exc:  # noqa: BLE001 - one bad pair shouldn't stop the scan
            log.warning("scan failed for pair", extra={"symbol": it.symbol, "error": str(exc)})
            results.append({"symbol": it.symbol, "error": str(exc)})

    advisories = _scan_open_positions(session)

    session.add(AgentRun(agent="scanner", event="scan",
                         detail={"scanned": len(items), "results": results,
                                 "advisories": advisories}))
    session.commit()
    log.info("watchlist scan complete",
             extra={"scanned": len(items), "advisories": len(advisories)})
    return {"scanned": len(items), "results": results, "advisories": advisories}


def scan_tick(session: Session) -> dict:
    """Scheduler entrypoint: respect the enabled flag + interval, then scan."""
    cfg = get_or_create_scan_config(session)
    if not cfg.enabled:
        return {"ran": False, "reason": "disabled"}

    now = datetime.now(timezone.utc)
    if cfg.last_scan_at is not None:
        last = cfg.last_scan_at if cfg.last_scan_at.tzinfo else cfg.last_scan_at.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cfg.interval_seconds:
            return {"ran": False, "reason": "interval not elapsed"}

    cfg.last_scan_at = now
    session.add(cfg)
    session.commit()
    summary = run_scan(session)
    return {"ran": True, **summary}
