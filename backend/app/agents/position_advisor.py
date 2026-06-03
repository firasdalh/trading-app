"""Open-position management advisor (read-only).

Separate from new-entry analysis: for each OPEN broker position it gives disciplined
guidance — protect a winner, cut a loser, set a missing stop — with special attention to
imminent high-impact news events (the classic "do I hold through the release?" decision).

Advisory only: it suggests; the user acts via the positions table (Set SL/TP, Close). It
never moves money on its own.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.data.providers import get_calendar_provider
from app.models.schemas import PositionAdvice
from app.risk.service import live_broker_positions

log = get_logger("agents.advisor")

_IMMINENT_BEFORE_MIN = 90   # an event this soon is "imminent"
_IN_WINDOW_AFTER_MIN = 30   # still relevant up to 30 min after the release


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def advise_positions(session: Session) -> list[PositionAdvice]:
    now = datetime.now(timezone.utc)
    cal = get_calendar_provider()
    out: list[PositionAdvice] = []

    for p in live_broker_positions(session):
        try:
            events = cal.get_events(p.symbol, lookahead_hours=12)
        except Exception:  # noqa: BLE001
            events = []

        # Nearest high-impact event in the relevant window around now.
        nearest_label: str | None = None
        nearest_mins: int | None = None
        for e in events:
            if str(e.importance).lower() != "high":
                continue
            mins = int((_aware(e.when) - now).total_seconds() / 60)
            if -_IN_WINDOW_AFTER_MIN <= mins <= _IMMINENT_BEFORE_MIN:
                if nearest_mins is None or mins < nearest_mins:
                    nearest_label, nearest_mins = e.label, mins

        winning = p.unrealized_pnl > 0
        has_stop = p.stop_loss is not None and p.stop_loss != 0

        if nearest_label is not None:
            when_txt = f"in ~{nearest_mins}m" if (nearest_mins or 0) > 0 else "now"
            if not has_stop:
                severity, headline = "danger", f"Protect {p.symbol} before {nearest_label} ({when_txt})"
                detail = ("No stop is set and a high-impact event is imminent. Set a stop now or "
                          "close — holding through news unprotected is high risk.")
            elif winning:
                severity, headline = "warn", f"{p.symbol} is winning into {nearest_label} ({when_txt})"
                detail = (f"Lock it in: move the stop to breakeven (entry {p.entry_price}) or take "
                          "partial/full profit before the release. News can reverse a winner fast.")
            else:
                severity, headline = "warn", f"{p.symbol} is losing into {nearest_label} ({when_txt})"
                detail = ("Consider closing or reducing before the release — a spike can deepen the "
                          "loss. At minimum keep a tight stop.")
        else:
            if not has_stop:
                severity, headline = "warn", f"{p.symbol}: no stop set"
                detail = "Add a stop to cap risk on this open position."
            elif winning:
                severity, headline = "info", f"{p.symbol} in profit"
                detail = ("No imminent events. Consider trailing the stop to lock gains; otherwise "
                          "hold and let it work toward target.")
            else:
                severity, headline = "info", f"{p.symbol} open"
                detail = "No imminent events and a stop is in place — hold and let stop/target manage it."

        out.append(PositionAdvice(
            symbol=p.symbol, direction=p.direction, unrealized_pnl=round(p.unrealized_pnl, 2),
            has_stop=has_stop, severity=severity, headline=headline, detail=detail,
            event_label=nearest_label, minutes_to_event=nearest_mins,
        ))
    return out
