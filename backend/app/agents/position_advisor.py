"""Open-position management advisor (read-only).

Separate from new-entry analysis: for each OPEN broker position it gives disciplined
guidance — is the trade still on track vs. the original plan, protect a winner, cut a loser,
set a missing stop — with special attention to imminent high-impact news events (the classic
"do I hold through the release?" decision).

It runs two checks per position:
  1. Thesis re-check — a fresh deterministic read of the symbol; does the current trend /
     momentum still agree with the side you're holding? (intact / weakening / invalidated)
  2. Event proximity — is a high-impact release imminent?

Advisory only: it suggests; the user acts via the positions table (Set SL/TP, Close). It
never moves money on its own. Can be run on demand or on a user-set auto-watch interval.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.data.providers import get_calendar_provider
from app.models.db import AdvisorConfig, AgentRun, TradeProposalRecord
from app.models.schemas import PositionAdvice
from app.risk.service import live_broker_positions

log = get_logger("agents.advisor")

_IMMINENT_BEFORE_MIN = 90   # an event this soon is "imminent"
_IN_WINDOW_AFTER_MIN = 30   # still relevant up to 30 min after the release
_SEV_RANK = {"info": 0, "warn": 1, "danger": 2}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _planned_timeframe(session: Session, symbol: str) -> str:
    """The timeframe the trade was last analysed on (so the re-check matches the plan)."""
    row = session.scalars(
        select(TradeProposalRecord).where(TradeProposalRecord.symbol == symbol)
        .order_by(TradeProposalRecord.id.desc())
    ).first()
    return row.timeframe if (row and row.timeframe) else "1h"


def _position_thesis(session: Session, p) -> dict | None:
    """Best-effort deterministic re-check: does the open position's side still agree with the
    current trend/momentum? Returns ``{"label", "note"}`` or ``None`` if data is unavailable.

    Deterministic only (no LLM) so it can run on every auto-watch tick for free.
    """
    try:
        from app.agents.orchestrator import _trend_from_indicators
        from app.agents.technical import run_technical
        from app.brokers.registry import get_broker_for
        from app.models.enums import AssetClass

        ac = AssetClass(p.asset_class)
        broker = get_broker_for(ac, get_or_create_settings(session).broker_map)
        tf = _planned_timeframe(session, p.symbol)
        series = []
        for t in dict.fromkeys([tf, "1h", "1d"]):
            try:
                series.append(broker.get_ohlcv(p.symbol, t, limit=200))
            except Exception:  # noqa: BLE001
                pass
        if not series:
            return None
        tech = run_technical(p.symbol, series, use_llm=False)
        if not tech.timeframes:
            return None
        prim = next((x for x in tech.timeframes if x.timeframe == tf), tech.timeframes[0])
        trend = _trend_from_indicators(prim.indicators, prim.trend)
        macd_hist = prim.indicators.get("macd_hist")
    except Exception:  # noqa: BLE001 - the advisor must never crash the scan/endpoint
        return None

    want = "up" if p.direction == "long" else "down"
    opp = "down" if p.direction == "long" else "up"

    if trend == opp:
        return {"label": "invalidated",
                "note": (f"Plan check: the {tf} trend now reads {trend.upper()}, against your "
                         f"{p.direction}. The setup that justified this trade no longer holds — "
                         "consider exiting rather than hoping.")}

    mom_against = macd_hist is not None and (
        (p.direction == "long" and macd_hist < 0) or (p.direction == "short" and macd_hist > 0)
    )
    if trend != want or mom_against:
        why = "trend is flattening" if trend != want else "momentum is rolling over"
        return {"label": "weakening",
                "note": (f"Plan check: {why} on {tf} (trend {trend}, MACD hist {macd_hist}). "
                         "Thesis weakening — tighten the stop or consider trimming.")}

    return {"label": "intact",
            "note": f"Plan check: thesis intact — the {tf} trend is still {trend}, in your favour."}


def _base_advice(p, ev_label, ev_mins, winning, has_stop) -> tuple[str, str, str]:
    """Event-proximity + protection advice (before folding in the thesis re-check)."""
    if ev_label is not None:
        when_txt = f"in ~{ev_mins}m" if (ev_mins or 0) > 0 else "now"
        if not has_stop:
            return ("danger", f"Protect {p.symbol} before {ev_label} ({when_txt})",
                    "No stop is set and a high-impact event is imminent. Set a stop now or close — "
                    "holding through news unprotected is high risk.")
        if winning:
            return ("warn", f"{p.symbol} is winning into {ev_label} ({when_txt})",
                    f"Lock it in: move the stop to breakeven (entry {p.entry_price}) or take "
                    "partial/full profit before the release. News can reverse a winner fast.")
        return ("warn", f"{p.symbol} is losing into {ev_label} ({when_txt})",
                "Consider closing or reducing before the release — a spike can deepen the loss. "
                "At minimum keep a tight stop.")
    if not has_stop:
        return ("warn", f"{p.symbol}: no stop set", "Add a stop to cap risk on this open position.")
    if winning:
        return ("info", f"{p.symbol} in profit",
                "No imminent events. Consider trailing the stop to lock gains; otherwise hold and "
                "let it work toward target.")
    return ("info", f"{p.symbol} open",
            "No imminent events and a stop is in place — hold and let stop/target manage it.")


def advise_positions(session: Session) -> list[PositionAdvice]:
    now = datetime.now(timezone.utc)
    cal = get_calendar_provider()
    out: list[PositionAdvice] = []

    for p in live_broker_positions(session):
        try:
            events = cal.get_events(p.symbol, lookahead_hours=12)
        except Exception:  # noqa: BLE001
            events = []

        ev_label: str | None = None
        ev_mins: int | None = None
        for e in events:
            if str(e.importance).lower() != "high":
                continue
            mins = int((_aware(e.when) - now).total_seconds() / 60)
            if -_IN_WINDOW_AFTER_MIN <= mins <= _IMMINENT_BEFORE_MIN:
                if ev_mins is None or mins < ev_mins:
                    ev_label, ev_mins = e.label, mins

        winning = p.unrealized_pnl > 0
        has_stop = p.stop_loss is not None and p.stop_loss != 0

        severity, headline, detail = _base_advice(p, ev_label, ev_mins, winning, has_stop)

        thesis = _position_thesis(session, p)
        thesis_label = thesis["label"] if thesis else "unknown"
        if thesis is not None:
            detail = f"{detail} {thesis['note']}"
            # The thesis can escalate urgency. News keeps its headline (it's the nearer concern);
            # otherwise the thesis drives the headline too.
            if thesis_label == "invalidated":
                severity = "danger"
                if ev_label is None:
                    headline = f"{p.symbol}: thesis broken — consider exiting"
            elif thesis_label == "weakening" and _SEV_RANK[severity] < _SEV_RANK["warn"]:
                severity = "warn"
                if ev_label is None:
                    headline = f"{p.symbol}: thesis weakening"

        out.append(PositionAdvice(
            symbol=p.symbol, direction=p.direction, unrealized_pnl=round(p.unrealized_pnl, 2),
            has_stop=has_stop, severity=severity, headline=headline, detail=detail,
            thesis=thesis_label, event_label=ev_label, minutes_to_event=ev_mins,
        ))
    return out


# --------------------------------------------------------------------------- #
#  Auto-watch config + scheduled tick
# --------------------------------------------------------------------------- #


def get_or_create_advisor_config(session: Session) -> AdvisorConfig:
    cfg = session.get(AdvisorConfig, 1)
    if cfg is None:
        cfg = AdvisorConfig(id=1, enabled=False, interval_seconds=300)
        session.add(cfg)
        session.commit()
    return cfg


def run_advisor(session: Session) -> dict:
    """Compute advisories now, record actionable ones to the audit log, stamp last_run_at."""
    advice = advise_positions(session)
    cfg = get_or_create_advisor_config(session)
    cfg.last_run_at = datetime.now(timezone.utc)
    actionable = [a for a in advice if a.severity in ("warn", "danger")]
    for a in actionable:
        log.warning("position advisory", extra={"symbol": a.symbol, "severity": a.severity,
                                                 "thesis": a.thesis, "advice": a.headline})
    session.add(AgentRun(agent="advisor", event="check",
                         detail={"positions": len(advice),
                                 "advisories": [a.model_dump(mode="json") for a in actionable]}))
    session.commit()
    return {"last_run_at": cfg.last_run_at.isoformat(), "advice": [a.model_dump(mode="json") for a in advice]}


def advisor_tick(session: Session) -> dict:
    """Scheduler entrypoint: respect the enabled flag + interval, then run the advisor."""
    cfg = get_or_create_advisor_config(session)
    if not cfg.enabled:
        return {"ran": False, "reason": "disabled"}

    now = datetime.now(timezone.utc)
    if cfg.last_run_at is not None:
        last = cfg.last_run_at if cfg.last_run_at.tzinfo else cfg.last_run_at.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cfg.interval_seconds:
            return {"ran": False, "reason": "interval not elapsed"}

    summary = run_advisor(session)
    return {"ran": True, **summary}
