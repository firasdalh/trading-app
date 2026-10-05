"""Macro / news / positioning API (Forex Factory layer).

- GET  /api/macro/context   : the full macro context for one instrument (analysis panel).
- GET  /api/macro/status    : feed health, config and row counts (monitoring dashboard).
- POST /api/macro/sync      : force a calendar refresh + persist.
- POST /api/macro/config    : update windows / thresholds / ablation flags.
- GET  /api/macro/analytics : expectancy by news-risk and crowding bucket (section 17/19).

Context and measurement only. Nothing here can open, close or direct a trade.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.macro.service import calendar_provider, get_macro_config, macro_context, sync_events
from app.models.db import EconomicEventRow, PositioningSnapshotRow, TradeMacroContext

router = APIRouter(prefix="/api/macro", tags=["macro"])


@router.get("/context")
def get_context(symbol: str = Query(...), asset_class: str | None = Query(None),
                session: Session = Depends(get_session)) -> dict:
    return macro_context(session, symbol, asset_class)


@router.get("/status")
def get_status(session: Session = Depends(get_session)) -> dict:
    cfg = get_macro_config(session)
    events = session.scalar(select(func.count(EconomicEventRow.id))) or 0
    upcoming = session.scalar(
        select(func.count(EconomicEventRow.id)).where(
            EconomicEventRow.when_utc >= datetime.now(timezone.utc))
    ) or 0
    snaps = session.scalar(select(func.count(PositioningSnapshotRow.id))) or 0
    decisions = session.scalar(select(func.count(TradeMacroContext.id))) or 0
    held = session.scalar(
        select(func.count(TradeMacroContext.id)).where(TradeMacroContext.blocked_by_news.is_(True))
    ) or 0
    return {
        "calendar": calendar_provider().status().as_dict(),
        "events_stored": events,
        "events_upcoming": upcoming,
        "positioning_snapshots": snaps,
        "positioning_source": "unavailable",
        "decisions_recorded": decisions,
        "decisions_held_by_news": held,
        "last_sync_at": cfg.last_sync_at.isoformat() if cfg.last_sync_at else None,
        "last_sync_note": cfg.last_sync_note,
        "config": {
            "enabled": cfg.enabled, "calendar_source": cfg.calendar_source,
            "warning_minutes": cfg.warning_minutes,
            "lock_before_minutes": cfg.lock_before_minutes,
            "lock_after_minutes": cfg.lock_after_minutes,
            "warn_score": cfg.warn_score, "wait_score": cfg.wait_score,
            "block_new_trades": cfg.block_new_trades,
            "delay_armed_triggers": cfg.delay_armed_triggers,
            "fail_mode": cfg.fail_mode,
            "use_news_risk": cfg.use_news_risk, "use_positioning": cfg.use_positioning,
            "use_news_articles": cfg.use_news_articles, "use_surprise": cfg.use_surprise,
        },
    }


@router.post("/sync")
def post_sync(session: Session = Depends(get_session)) -> dict:
    calendar_provider()._refresh(force=True)
    return sync_events(session)


class MacroConfigPatch(BaseModel):
    """Every window and threshold is an ablation knob, so all of them are settable."""

    enabled: bool | None = None
    warning_minutes: int | None = Field(None, ge=0, le=1440)
    lock_before_minutes: int | None = Field(None, ge=0, le=240)
    lock_after_minutes: int | None = Field(None, ge=0, le=240)
    normalize_minutes: int | None = Field(None, ge=0, le=480)
    warn_score: int | None = Field(None, ge=0, le=100)
    wait_score: int | None = Field(None, ge=0, le=100)
    block_new_trades: bool | None = None
    delay_armed_triggers: bool | None = None
    fail_mode: str | None = None
    use_news_risk: bool | None = None
    use_positioning: bool | None = None
    use_news_articles: bool | None = None
    use_surprise: bool | None = None


@router.post("/config")
def post_config(patch: MacroConfigPatch, session: Session = Depends(get_session)) -> dict:
    cfg = get_macro_config(session)
    for field, value in patch.model_dump(exclude_none=True).items():
        if field == "fail_mode" and value not in ("allow_with_warning", "block_new_trades"):
            continue
        setattr(cfg, field, value)
    cfg.updated_at = datetime.now(timezone.utc)
    session.add(cfg)
    session.commit()
    return get_status(session)


# Below this many closed trades in a bucket, an expectancy is noise wearing a decimal point. The
# whole purpose of this endpoint is to test whether the macro layer helps, so it must not be the
# thing that manufactures a premature yes.
_MIN_BUCKET = 30


@router.get("/analytics")
def get_analytics(session: Session = Depends(get_session)) -> dict:
    """Expectancy by news-risk band and by crowding band - or an honest 'not yet'."""
    from app.models.db import Position

    rows = session.execute(
        select(TradeMacroContext, Position)
        .join(Position, TradeMacroContext.position_id == Position.id)
        .where(Position.status == "closed", Position.realized_pnl.isnot(None),
               Position.risk_amount > 0)
    ).all()

    def bucket(key):
        out: dict[str, dict] = {}
        for ctx, pos in rows:
            k = key(ctx) or "unknown"
            b = out.setdefault(k, {"trades": 0, "total_r": 0.0})
            b["trades"] += 1
            b["total_r"] += pos.realized_pnl / pos.risk_amount
        for k, b in out.items():
            enough = b["trades"] >= _MIN_BUCKET
            b["expectancy_r"] = round(b["total_r"] / b["trades"], 3) if enough else None
            b["total_r"] = round(b["total_r"], 2)
            b["sufficient_sample"] = enough
        return out

    held = session.scalar(
        select(func.count(TradeMacroContext.id)).where(TradeMacroContext.blocked_by_news.is_(True))
    ) or 0
    return {
        "closed_trades_with_macro_context": len(rows),
        "min_sample_per_bucket": _MIN_BUCKET,
        "by_news_band": bucket(lambda c: c.news_band),
        "by_news_action": bucket(lambda c: c.news_action),
        "by_crowding": bucket(
            lambda c: None if c.crowding_score is None
            else "LOW" if c.crowding_score <= 20 else "MODERATE" if c.crowding_score <= 40
            else "ELEVATED" if c.crowding_score <= 60 else "HIGH" if c.crowding_score <= 80
            else "EXTREME"
        ),
        "trades_held_by_news": held,
        "note": (
            "Expectancy is reported only for buckets with at least "
            f"{_MIN_BUCKET} closed trades. Until then this layer is unproven, not proven neutral."
        ),
    }
