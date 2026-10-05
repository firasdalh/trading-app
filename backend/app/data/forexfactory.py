"""Forex Factory economic calendar - the weekly structured export.

SOURCE CHOICE
-------------
Forex Factory publishes its calendar as a machine-readable weekly file:

    https://nfs.faireconomy.media/ff_calendar_thisweek.json

That is the source this provider uses. It is the structured export officially exposed by the site,
it needs no key, no login and no browser, and it answers in ~19 KB of clean JSON.

The site's own HTML pages (forexfactory.com/calendar, /market/<pair>, /news) are served behind a
Cloudflare managed challenge - a request for even /robots.txt returns the "Just a moment..."
interstitial rather than a file. Reading those pages from a program would therefore mean defeating
a technical access-control measure, so this module does not attempt it. That is a deliberate
limitation, not an oversight: it is why positioning and pair-story data are not available here
(see app/macro/positioning.py for what that costs and what would unblock it).

WHAT THE FEED DOES AND DOES NOT CARRY
-------------------------------------
Each row is {title, country, date, impact, forecast, previous}, where ``country`` is actually a
CURRENCY code ("USD", "JPY", ...) and ``date`` is ISO-8601 with a US/Eastern offset.

There is no ``actual`` field, and only the CURRENT week is published (ff_calendar_lastweek and
ff_calendar_nextweek both 404). So this provider is authoritative for what is scheduled and when -
the thing the news-risk gate needs - and cannot supply post-release outcomes. The existing
TradingView provider does carry ``actual``; app/macro/service.py merges the two.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.logging import get_logger
from app.data.providers import (
    _CCY_TO_COUNTRY,
    CalendarEvent,
    EconomicCalendarProvider,
    countries_for_symbol,
)

log = get_logger("data.forexfactory")

FF_SOURCE_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# The file is regenerated weekly and revised during the week (forecasts firm up, times shift), so a
# 15-minute cache is far more often than the data changes while still catching a same-day revision.
_CACHE_TTL_SEC = 1800
# Never issue two requests closer together than this, even across callers/threads. The feed is a
# courtesy; hammering it is both rude and the fastest way to get an IP blocked.
_MIN_REQUEST_INTERVAL_SEC = 120
# The feed answers 429 readily. A rate limit is not a transient error, so it must never be retried
# in-loop; instead the whole provider backs off, doubling from here up to the ceiling.
_BACKOFF_START_SEC = 900
_BACKOFF_MAX_SEC = 4 * 3600
_TIMEOUT_SEC = 20
_RETRIES = 2

_IMPACT = {"high": "high", "medium": "medium", "low": "low", "holiday": "holiday"}


@dataclass
class FeedStatus:
    """What the monitoring dashboard needs to say whether this data can be trusted right now."""

    ok: bool = False
    events: int = 0
    fetched_at: datetime | None = None
    age_seconds: float | None = None
    error: str | None = None
    source_url: str = FF_SOURCE_URL

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "events": self.events,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "age_seconds": round(self.age_seconds) if self.age_seconds is not None else None,
            "error": self.error,
            "source_url": self.source_url,
            "source": "forexfactory",
        }


def parse_number(raw: str | None) -> float | None:
    """Turn a Forex Factory display value into a number, or None when there isn't one.

    The feed writes values the way the site shows them - "3.6%", "-0.3%", "1.25K", "222B", "<0.1%",
    "1,234" - so a bare float() fails on almost everything worth reading.
    """
    if raw is None:
        return None
    t = str(raw).strip().replace(",", "").replace("%", "").replace("<", "").replace(">", "")
    if not t or t in ("-", "--"):
        return None
    mult = 1.0
    if t and t[-1] in "KkMmBbTt":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[t[-1].lower()]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def event_key(title: str, currency: str, when: datetime) -> str:
    """A stable identity for an event across refetches.

    The export carries no id, and the same release appears in every fetch of that week - so without
    a key, storing the feed would insert duplicates on every poll. Title + currency + the scheduled
    minute is unique in practice and survives a forecast being revised.
    """
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return f"{currency.upper()}|{title.strip().lower()}|{stamp}"


class ForexFactoryCalendarProvider(EconomicCalendarProvider):
    """The weekly Forex Factory export, behind the app's existing calendar interface.

    Thread-safe, cached, rate-limited and non-fatal: any failure returns an empty list and is
    recorded in :meth:`status`, so the UI can say NEWS DATA UNAVAILABLE instead of showing a quiet
    calendar - which looks exactly like a day with no events.
    """

    name = "forexfactory"

    def __init__(self, *, url: str = FF_SOURCE_URL, cache_ttl: int = _CACHE_TTL_SEC) -> None:
        self.url = url
        self.cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._events: list[CalendarEvent] = []
        self._fetched_at: datetime | None = None
        self._fetched_mono: float | None = None
        self._last_attempt_mono: float | None = None
        self._backoff_sec: float = 0.0
        self._error: str | None = None

    def _download(self) -> list[dict]:
        last: Exception | None = None
        for attempt in range(_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    self.url,
                    headers={
                        "User-Agent": "ProPilot/1.0 (trading desk; local install)",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
                    if resp.status != 200:
                        raise urllib.error.HTTPError(
                            self.url, resp.status, "bad status", resp.headers, None
                        )
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 429/503 mean "stop asking", so retrying inside the loop actively makes it worse:
                # three requests in four seconds is what earns a longer ban. Surface it immediately
                # and let the caller's backoff handle the waiting.
                if exc.code in (429, 503):
                    raise
                last = exc
                if attempt < _RETRIES:
                    time.sleep(1.5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 - every other failure degrades the same way
                last = exc
                if attempt < _RETRIES:
                    time.sleep(1.5 * (attempt + 1))
        raise last if last else RuntimeError("forex factory fetch failed")

    def _parse(self, rows: list[dict]) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        for r in rows:
            try:
                when = datetime.fromisoformat(str(r["date"]))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                when = when.astimezone(timezone.utc)      # everything internal is UTC
            except Exception:  # noqa: BLE001 - one malformed row must not lose the week
                continue
            ccy = str(r.get("country") or "").upper()
            imp = _IMPACT.get(str(r.get("impact") or "").strip().lower(), "low")
            out.append(
                CalendarEvent(
                    label=str(r.get("title") or "").strip(),
                    when=when,
                    importance=imp,
                    country=_CCY_TO_COUNTRY.get(ccy, ccy),
                    forecast=parse_number(r.get("forecast")),
                    previous=parse_number(r.get("previous")),
                    actual=None,   # not published in this feed - see the module docstring
                )
            )
        out.sort(key=lambda e: e.when)
        return out

    def _refresh(self, force: bool = False) -> None:
        """Repopulate the cache if it is stale. Never raises."""
        with self._lock:
            now_mono = time.monotonic()
            fresh = self._fetched_mono is not None and now_mono - self._fetched_mono < self.cache_ttl
            if fresh and not force:
                return
            # Rate limit: back off even when the cache is stale. `_backoff_sec` grows after each
            # failure so a 429 is answered with silence rather than another request.
            wait = max(_MIN_REQUEST_INTERVAL_SEC, self._backoff_sec)
            if (
                self._last_attempt_mono is not None
                and now_mono - self._last_attempt_mono < wait
                and not force
            ):
                return
            self._last_attempt_mono = now_mono
            try:
                rows = self._download()
                self._events = self._parse(rows)
                self._fetched_at = datetime.now(timezone.utc)
                self._fetched_mono = now_mono
                self._error = None
                self._backoff_sec = 0.0
                log.info(
                    "forex factory calendar refreshed",
                    extra={"events": len(self._events), "url": self.url},
                )
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"
                self._backoff_sec = min(
                    _BACKOFF_MAX_SEC, max(_BACKOFF_START_SEC, self._backoff_sec * 2)
                )
                log.warning(
                    "forex factory calendar fetch failed; backing off",
                    extra={"error": self._error, "url": self.url,
                           "backoff_sec": int(self._backoff_sec)},
                )

    def all_events(self) -> list[CalendarEvent]:
        """Every event in the published week (UTC), refreshing the cache when due."""
        self._refresh()
        return list(self._events)

    def status(self) -> FeedStatus:
        age = None
        if self._fetched_at is not None:
            age = (datetime.now(timezone.utc) - self._fetched_at).total_seconds()
        return FeedStatus(
            ok=self._error is None and bool(self._events),
            events=len(self._events),
            fetched_at=self._fetched_at,
            age_seconds=age,
            error=self._error,
            source_url=self.url,
        )

    def get_events(self, symbol: str, lookahead_hours: int = 24,
                   include_medium: bool = False,
                   asset_class: str | None = None) -> list[CalendarEvent]:
        """Events for this instrument's currencies inside the lookahead window."""
        countries = set(countries_for_symbol(symbol, asset_class))
        if not countries:
            return []
        wanted = {"high", "medium"} if include_medium else {"high"}
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=lookahead_hours)
        return [
            e for e in self.all_events()
            if e.country in countries and e.importance in wanted and now <= e.when <= horizon
        ]
