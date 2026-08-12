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
    def get_events(self, symbol: str, lookahead_hours: int = 24,
                   include_medium: bool = False,
                   asset_class: str | None = None) -> list[CalendarEvent]: ...


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

    def get_events(self, symbol: str, lookahead_hours: int = 24,
                   include_medium: bool = False,
                   asset_class: str | None = None) -> list[CalendarEvent]:
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
    # The exotics the broker actually lists. Without these, crosses like NOKDKK and BTCZAR
    # resolved to no country and so to an empty calendar — which reads exactly like a quiet day.
    "CNH": "CN", "CZK": "CZ", "DKK": "DK", "HUF": "HU", "PLN": "PL", "SGD": "SG",
    "ZAR": "ZA", "NOK": "NO", "SEK": "SE", "TRY": "TR", "MXN": "MX", "HKD": "HK",
    "KRW": "KR", "THB": "TH", "INR": "IN",
}

# Majors whose own macro is what moves a crypto pair. A crypto cross with no fiat leg (ETHBTC) has
# no national calendar of its own, but both legs still react to US rates and inflation.
_CRYPTO_TOKENS = (
    "BTC", "ETH", "XRP", "LTC", "BCH", "ADA", "SOL", "DOGE", "DOT", "LINK",
    "AVAX", "MATIC", "BNB", "TRX", "XLM", "ATOM", "UNI", "ETC", "FIL", "NEAR",
)
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
    "HK50": "HK", "HSI": "HK", "IN50": "IN", "NIFTY": "IN", "CN50": "CN", "CHINA": "CN",
    "DXY": "US",   # the dollar index: US macro by definition
}

# Energy symbols carry no currency code either ("USOILm" contains no "USD"), so they matched nothing
# and returned an empty calendar — which also meant the fundamental agent's news-blackout windows
# never applied to an oil trade. Crude and gas are priced in dollars and moved by US macro (CPI,
# FOMC, EIA inventories), so US is the calendar that matters, Brent included.
_ENERGY_TOKENS = {
    "USOIL": "US", "UKOIL": "US", "WTI": "US", "XTI": "US", "XBR": "US", "BRENT": "US",
    "CRUDE": "US", "NGAS": "US", "XNG": "US", "NATGAS": "US",
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

    def _countries(self, symbol: str, asset_class: str | None = None) -> list[str]:
        # Two forms, because they are matched against different things. Currency codes are letters,
        # so digits are stripped to stop "US30" reading as a USD pair. Index and energy tickers ARE
        # partly digits — matching those against the stripped string turned "JP225M" into "JPM", so
        # every entry in _INDEX_COUNTRY silently matched nothing and index trades came back with an
        # empty calendar (and therefore no news blackout).
        raw = symbol.upper()
        s = "".join(ch for ch in raw if ch.isalpha())
        countries: list[str] = []
        # Currencies are matched as the pair's LEGS (first three letters, next three), not as a
        # substring anywhere. "ETHBTC" contains "THB" straddling the two halves, so an anywhere
        # match handed an Ethereum cross the Thai calendar. A currency only counts where a currency
        # can actually sit.
        # The quote currency is the LAST three letters, not always characters 3-6: "AAVEUSD" has a
        # four-letter base, so a fixed slice missed its USD. The broker's trailing "m" is dropped
        # first or it would eat the final letter of the quote.
        core = s[:-1] if s.endswith("M") and len(s) > 4 else s
        legs = {core[:3], core[-3:]} if len(core) >= 3 else set()
        for ccy, country in _CCY_TO_COUNTRY.items():
            if ccy in legs and country not in countries:
                countries.append(country)
        if any(s.startswith(p) or p in s for p in _METAL_PREFIXES) and "US" not in countries:
            countries.append("US")
        # Stock indices: add the country whose macro calendar drives them.
        for token, country in _INDEX_COUNTRY.items():
            if token in raw and country not in countries:
                countries.append(country)
        # Energy: same idea, and the reason oil used to come back with an empty calendar.
        for token, country in _ENERGY_TOKENS.items():
            if token in raw and country not in countries:
                countries.append(country)
        if not countries:
            # Last resorts, only when nothing above matched.
            # A single-stock ticker carries no country in its name at all, and this broker lists US
            # equities — so CPI and FOMC are its calendar. Crypto crosses with no fiat leg key off
            # US rates too. Guessing wrong here costs a few irrelevant rows; guessing NOTHING costs
            # the news blackout entirely, which is the far worse error.
            if (asset_class or "").lower() == "stock":
                countries.append("US")
            elif any(t in raw for t in _CRYPTO_TOKENS):
                countries.append("US")
        return countries

    def get_events(self, symbol: str, lookahead_hours: int = 24,
                   include_medium: bool = False,
                   asset_class: str | None = None) -> list[CalendarEvent]:
        countries = self._countries(symbol, asset_class)
        if not countries:
            return []
        cache_key = ",".join(sorted(countries)) + ("|med" if include_medium else "")
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
            min_imp = 0 if include_medium else 1   # medium(0)+high when asked, else high(1) only
            events: list[CalendarEvent] = []
            for e in rows:
                imp = int(e.get("importance", -2))
                if imp < min_imp:
                    continue
                when = datetime.fromisoformat(str(e.get("date", "")).replace("Z", "+00:00"))
                events.append(CalendarEvent(
                    label=f"{e.get('country', '')}: {e.get('title', 'event')}",
                    when=when, importance="high" if imp >= 1 else "medium",
                    country=str(e.get("country", "")),
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
