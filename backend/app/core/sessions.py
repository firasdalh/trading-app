"""Trading sessions on the UTC clock: which one a trade belongs to (asked 2026-11-10: "in which session - Asia,
Middle East, New York or London - so I know which session is performing with us better").

Fixed UTC hours with no overlaps, so every trade falls in exactly one, by the time it OPENED:
  Asia              00-06 UTC   Tokyo, Hong Kong, Singapore
  Middle East       06-08 UTC   the Gulf markets open (10:00 in Dubai) while Asia winds down, before London
  London            08-13 UTC
  New York          13-21 UTC   with the London overlap until 16:00, the busiest hours of the day
  Between sessions  21-24 UTC   after New York, before Asia: the thinnest hours (only crypto trades then)

The desk's chart paints the same hours (frontend sessions.ts: the session bands and their key) - change both together.
A strategy can keep these sessions as its trading hours too (strategy.hours_now, asked 2026-11-11): it then opens
trades only in the ones picked, on exactly the hours its trades are counted in here.
"""
from __future__ import annotations

from datetime import datetime, timezone

SESSIONS: tuple[dict, ...] = (
    {"key": "asia", "name": "Asia", "start": 0, "end": 6,
     "about": "Tokyo, Hong Kong, Singapore. Thin liquidity: breaks here fail most often."},
    {"key": "middle_east", "name": "Middle East", "start": 6, "end": 8,
     "about": "The Gulf markets open (10:00 in Dubai) while Asia winds down, before London."},
    {"key": "london", "name": "London", "start": 8, "end": 13,
     "about": "The first big-volume session of the day."},
    {"key": "new_york", "name": "New York", "start": 13, "end": 21,
     "about": "Overlaps London until 16:00 UTC: the busiest hours of the day."},
    {"key": "between", "name": "Between sessions", "start": 21, "end": 24,
     "about": "After New York closes, before Asia opens: the thinnest hours."},
)

KEYS: tuple[str, ...] = tuple(s["key"] for s in SESSIONS)


def utc_hour(at: datetime) -> int:
    """The UTC hour of ``at``; a naive time is taken as UTC already (how the DB and the broker rows keep them)."""
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).hour


def session_at(hour: int) -> str:
    """The key of the session a UTC hour (0-23) belongs to."""
    return next(s["key"] for s in SESSIONS if s["start"] <= hour % 24 < s["end"])


def session_of(at: datetime | None) -> str | None:
    """The key of the session ``at`` falls in; None without a time."""
    return None if at is None else session_at(utc_hour(at))


def name_of(key: str | None) -> str:
    """A session's name from its key ("new_york" -> "New York"); the key itself when it is not one."""
    return next((s["name"] for s in SESSIONS if s["key"] == key), key or "")
