"""News / economic-calendar / sentiment provider interfaces (+ offline stubs).

Like brokers and market data, these are swappable behind interfaces. Milestone 6 ships
deterministic stubs (no external account needed); real providers (with free tiers) can be
dropped in by implementing the same ABCs and registering them in config.

The stubs are intentionally *neutral* and deterministic so the Fundamental Analyst and the
pipeline behave predictably offline. A synthetic high-impact calendar event can be injected
for testing stand-aside behavior.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.core.logging import get_logger

_log = get_logger("data.providers")


@dataclass
class NewsItem:
    headline: str
    summary: str = ""
    source: str = ""
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    url: str = ""  # NOTE: never log full URLs with embedded keys


@dataclass
class CalendarEvent:
    label: str
    when: datetime
    importance: str = "medium"  # low | medium | high
    country: str = ""           # TradingView country code (US, EU, JP, ...)
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None  # populated after release; drives surprise assessment


@dataclass
class SentimentScore:
    symbol: str
    score: float = 0.0          # -1 (very bearish) .. +1 (very bullish)
    sample_size: int = 0
    summary: str = ""


# --------------------------------------------------------------------------- #
#  Interfaces
# --------------------------------------------------------------------------- #


class NewsProvider(ABC):
    name = "abstract"

    @abstractmethod
    def get_news(self, symbol: str, limit: int = 10) -> list[NewsItem]: ...


class EconomicCalendarProvider(ABC):
    name = "abstract"

    @abstractmethod
    def get_events(self, symbol: str, lookahead_hours: int = 24) -> list[CalendarEvent]: ...


class SentimentProvider(ABC):
    name = "abstract"

    @abstractmethod
    def get_sentiment(self, symbol: str) -> SentimentScore: ...


# --------------------------------------------------------------------------- #
#  Offline stubs
# --------------------------------------------------------------------------- #


class StubNewsProvider(NewsProvider):
    name = "stub"

    def get_news(self, symbol: str, limit: int = 10) -> list[NewsItem]:
        return []  # no news offline


class StubCalendarProvider(EconomicCalendarProvider):
    """Returns no events by default. Tests can subclass / inject events to exercise the
    stand-aside logic."""

    name = "stub"

    def __init__(self, events: list[CalendarEvent] | None = None) -> None:
        self._events = events or []

    def get_events(self, symbol: str, lookahead_hours: int = 24) -> list[CalendarEvent]:
        return list(self._events)


class StubSentimentProvider(SentimentProvider):
    name = "stub"

    def get_sentiment(self, symbol: str) -> SentimentScore:
        return SentimentScore(symbol=symbol, score=0.0, sample_size=0, summary="no data (stub)")


# --------------------------------------------------------------------------- #
#  Real economic calendar (TradingView public JSON — no API key)
# --------------------------------------------------------------------------- #

# Currency code -> TradingView country code. Metals/crypto map to US (USD-driven).
_CCY_TO_COUNTRY = {
    "USD": "US", "EUR": "EU", "JPY": "JP", "GBP": "GB", "AUD": "AU",
    "CAD": "CA", "CHF": "CH", "NZD": "NZ", "CNY": "CN",
}
_METAL_PREFIXES = ("XAU", "XAG", "XPT", "XPD")

# Stock-index symbols carry no currency code, so map them to the country whose macro events
# move them (FOMC moves US500, ECB moves DE40, etc.). Checked as substrings of the symbol.
_INDEX_COUNTRY = {
    "US500": "US", "US30": "US", "USTEC": "US", "US2000": "US", "SPX": "US", "NAS": "US",
    "NDX": "US", "DOW": "US", "DJ": "US",
    "DE40": "EU", "DE30": "EU", "GER": "EU", "DAX": "EU", "STOXX": "EU", "EU50": "EU", "FR40": "EU", "CAC": "EU",
    "UK100": "GB", "FTSE": "GB",
    "JP225": "JP", "JPN225": "JP", "NIK": "JP",
    "AUS200": "AU", "ASX": "AU",
}


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class TradingViewCalendarProvider(EconomicCalendarProvider):
    """Pulls high-impact economic events from TradingView's public calendar JSON.

    No API key required. Results are cached per country-set per hour to avoid hammering it,
    and any failure degrades to an empty list (the agent then treats fundamentals as neutral).
    The instrument's currencies determine which countries' events matter.
    """

    name = "tradingview"
    _URL = "https://economic-calendar.tradingview.com/events"

    def __init__(self, stand_aside_minutes: int = 30) -> None:
        self.stand_aside_minutes = stand_aside_minutes
        self._cache: dict[str, tuple[datetime, list[CalendarEvent]]] = {}

    def _countries(self, symbol: str) -> list[str]:
        s = "".join(ch for ch in symbol.upper() if ch.isalpha())
        countries: list[str] = []
        for ccy, country in _CCY_TO_COUNTRY.items():
            if ccy in s and country not in countries:
                countries.append(country)
        if any(s.startswith(p) or p in s for p in _METAL_PREFIXES) and "US" not in countries:
            countries.append("US")
        # Stock indices: add the country whose macro calendar drives them.
        for token, country in _INDEX_COUNTRY.items():
            if token in s and country not in countries:
                countries.append(country)
        return countries

    def get_events(self, symbol: str, lookahead_hours: int = 24) -> list[CalendarEvent]:
        countries = self._countries(symbol)
        if not countries:
            return []
        cache_key = ",".join(sorted(countries))
        now = datetime.now(timezone.utc)
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < 3600:
            return cached[1]

        try:
            import httpx

            frm = now - timedelta(hours=8)   # lookback to catch just-released surprises
            to = now + timedelta(hours=lookahead_hours)
            params = {
                "from": frm.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to": to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "countries": ",".join(countries),
            }
            headers = {
                "Origin": "https://www.tradingview.com",
                "Referer": "https://www.tradingview.com/",
                "User-Agent": "Mozilla/5.0 (trading-app)",
            }
            resp = httpx.get(self._URL, params=params, headers=headers, timeout=8.0)
            resp.raise_for_status()
            rows = resp.json().get("result", [])
            events: list[CalendarEvent] = []
            for e in rows:
                if int(e.get("importance", -2)) < 1:   # high-impact only
                    continue
                when = datetime.fromisoformat(str(e.get("date", "")).replace("Z", "+00:00"))
                events.append(CalendarEvent(
                    label=f"{e.get('country', '')}: {e.get('title', 'event')}",
                    when=when, importance="high", country=str(e.get("country", "")),
                    forecast=_f(e.get("forecast")), previous=_f(e.get("previous")),
                    actual=_f(e.get("actual")),
                ))
            self._cache[cache_key] = (now, events)
            _log.info("calendar fetched", extra={"symbol": symbol, "countries": cache_key, "high_events": len(events)})
            return events
        except Exception as exc:  # noqa: BLE001 - degrade to neutral
            _log.warning("calendar fetch failed; treating as no events",
                         extra={"symbol": symbol, "error": str(exc)})
            self._cache[cache_key] = (now, [])  # cache the empty to avoid retry storms
            return []


# --------------------------------------------------------------------------- #
#  Registry (swap via config later)
# --------------------------------------------------------------------------- #

_news: NewsProvider = StubNewsProvider()
# Real economic calendar by default (no key); falls back to neutral on any failure.
_calendar: EconomicCalendarProvider = TradingViewCalendarProvider()
_sentiment: SentimentProvider = StubSentimentProvider()


def get_news_provider() -> NewsProvider:
    return _news


def get_calendar_provider() -> EconomicCalendarProvider:
    return _calendar


def get_sentiment_provider() -> SentimentProvider:
    return _sentiment


def set_providers(
    *,
    news: NewsProvider | None = None,
    calendar: EconomicCalendarProvider | None = None,
    sentiment: SentimentProvider | None = None,
) -> None:
    """Override providers (used by tests and, later, config-driven wiring)."""
    global _news, _calendar, _sentiment
    if news is not None:
        _news = news
    if calendar is not None:
        _calendar = calendar
    if sentiment is not None:
        _sentiment = sentiment
