"""Fundamental Analyst agent (Milestone 6).

Inputs: recent news, economic-calendar events, and sentiment for the symbol (from the
provider layer). Output: a structured ``FundamentalRead`` with a bias, key drivers, a
surprise-vs-expectation assessment, and — critically — a list of high-impact event windows
where the system should STAND ASIDE.

LLM-driven when a key is configured (the prompt instructs it to weigh *surprise vs.
expectation*, not raw headlines); otherwise a deterministic fallback derives bias from
sentiment and builds stand-aside windows around high-impact calendar events.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.data.providers import (
    CalendarEvent,
    get_calendar_provider,
    get_news_provider,
    get_sentiment_provider,
)
from app.models.enums import TradingBias
from app.models.schemas import EventWindow, FundamentalRead

log = get_logger("agents.fundamental")

# How wide a stand-aside window to draw around a high-impact event (each side).
_STAND_ASIDE_MINUTES = 30

# Map a TradingView country code to its currency (for surprise -> bias attribution).
_COUNTRY_CCY = {
    "US": "USD", "EU": "EUR", "JP": "JPY", "GB": "GBP", "AU": "AUD",
    "CA": "CAD", "CH": "CHF", "NZ": "NZD", "CN": "CNY",
}

# Does a higher-than-expected reading help (+1) or hurt (-1) that currency?
# Conservative, well-known relationships; unknown indicators are ignored (stay neutral).
_POS_IF_HIGHER = ("gdp", "retail sales", "payroll", "employment change", "pmi",
                  "manufacturing", "services", "industrial production", "durable goods",
                  "cpi", "inflation", "ppi", "confidence", "sentiment", "ifo", "zew")
_NEG_IF_HIGHER = ("unemployment", "jobless", "claims", "deficit")

# How recently a release must have happened (hours) to still drive bias.
_SURPRISE_WINDOW_HOURS = 8


def _pair_currencies(symbol: str) -> tuple[str | None, str | None]:
    s = "".join(ch for ch in symbol.upper() if ch.isalpha())
    if len(s) >= 6:
        return s[:3], s[3:6]
    return None, None


def _indicator_polarity(label: str) -> int | None:
    low = label.lower()
    if any(k in low for k in _NEG_IF_HIGHER):
        return -1
    if any(k in low for k in _POS_IF_HIGHER):
        return 1
    return None


def _directional_bias_from_events(symbol: str, events: list[CalendarEvent], now: datetime):
    """Derive a (conservative, low-confidence) directional bias from RELEASED high-impact
    events' surprise (actual vs. forecast/previous), attributed to the pair's currencies."""
    base, quote = _pair_currencies(symbol)
    score = 0.0
    drivers: list[str] = []
    for ev in events:
        if ev.actual is None:
            continue
        when = ev.when if ev.when.tzinfo else ev.when.replace(tzinfo=timezone.utc)
        if when > now or (now - when) > timedelta(hours=_SURPRISE_WINDOW_HOURS):
            continue
        expectation = ev.forecast if ev.forecast is not None else ev.previous
        if expectation is None:
            continue
        polarity = _indicator_polarity(ev.label)
        if polarity is None:
            continue
        ccy = _COUNTRY_CCY.get(ev.country)
        if ccy is None:
            continue
        surprise = ev.actual - expectation
        if surprise == 0:
            continue
        ccy_effect = polarity * (1 if surprise > 0 else -1)  # + = currency strengthens
        if ccy == base:
            pair_effect = ccy_effect
        elif ccy == quote:
            pair_effect = -ccy_effect
        else:
            continue
        score += pair_effect
        drivers.append(
            f"{ev.label}: actual {ev.actual} vs exp {expectation} "
            f"({'beat' if surprise > 0 else 'miss'}) -> {'bullish' if pair_effect > 0 else 'bearish'} {symbol}"
        )

    if score > 0:
        return TradingBias.BULLISH, drivers, "Recent data surprises lean bullish."
    if score < 0:
        return TradingBias.BEARISH, drivers, "Recent data surprises lean bearish."
    return None, drivers, ""

_SYSTEM = """You are a Fundamental Analyst on a trading desk. You receive recent news,
upcoming/just-released economic-calendar events, and a sentiment score for one instrument.
Produce a structured read:
- bias: bullish / bearish / neutral, weighing SURPRISE vs. EXPECTATION (actual vs. forecast),
  not raw headline counts. A strong beat already priced in is not bullish.
- key_drivers: the few things that actually matter.
- stand_aside_windows: time ranges around HIGH-impact events where trading should pause.
- confidence (0-1): be honest; little data means low confidence.
Return strict JSON matching the schema."""


def _high_impact_windows(events: list[CalendarEvent], now: datetime) -> list[EventWindow]:
    windows: list[EventWindow] = []
    for ev in events:
        if ev.importance.lower() != "high":
            continue
        when = ev.when if ev.when.tzinfo else ev.when.replace(tzinfo=timezone.utc)
        windows.append(EventWindow(
            label=ev.label,
            start=when - timedelta(minutes=_STAND_ASIDE_MINUTES),
            end=when + timedelta(minutes=_STAND_ASIDE_MINUTES),
            importance="high",
        ))
    return windows


def _deterministic_read(symbol: str, now: datetime) -> FundamentalRead:
    sentiment = get_sentiment_provider().get_sentiment(symbol)
    events = get_calendar_provider().get_events(symbol)
    news = get_news_provider().get_news(symbol, limit=5)

    # Directional bias: prefer real economic-surprise from released events; else sentiment.
    surprise_bias, surprise_drivers, surprise_note = _directional_bias_from_events(symbol, events, now)
    if surprise_bias is not None:
        bias = surprise_bias
        confidence = 0.3  # data-driven but a modest nudge, not a primary signal
        assessment = surprise_note
    elif sentiment.score > 0.2:
        bias, confidence, assessment = TradingBias.BULLISH, round(min(0.6, sentiment.sample_size / 100.0), 2), sentiment.summary
    elif sentiment.score < -0.2:
        bias, confidence, assessment = TradingBias.BEARISH, round(min(0.6, sentiment.sample_size / 100.0), 2), sentiment.summary
    else:
        bias, confidence, assessment = TradingBias.NEUTRAL, 0.0, sentiment.summary or "no surprise data available"

    drivers = surprise_drivers + [n.headline for n in news[:3]]
    windows = _high_impact_windows(events, now)

    return FundamentalRead(
        symbol=symbol,
        bias=bias,
        key_drivers=drivers,
        surprise_assessment=assessment or "no surprise data available",
        stand_aside_windows=windows,
        confidence=confidence,
        notes="deterministic read from economic-calendar surprise + sentiment (no LLM)",
    )


def run_fundamental(symbol: str, now: datetime | None = None, use_llm: bool = True) -> FundamentalRead:
    now = now or datetime.now(timezone.utc)

    if use_llm and llm_available():
        sentiment = get_sentiment_provider().get_sentiment(symbol)
        events = get_calendar_provider().get_events(symbol)
        news = get_news_provider().get_news(symbol, limit=10)
        user = (
            f"symbol={symbol} now={now.isoformat()}\n\n"
            f"SENTIMENT: score={sentiment.score} n={sentiment.sample_size} {sentiment.summary}\n\n"
            "NEWS:\n" + ("\n".join(f"- {n.published_at.isoformat()} {n.headline}: {n.summary}"
                                   for n in news) or "(none)") + "\n\n"
            "CALENDAR:\n" + ("\n".join(
                f"- {e.when.isoformat()} [{e.importance}] {e.label} "
                f"forecast={e.forecast} previous={e.previous} actual={e.actual}"
                for e in events) or "(none)")
        )
        result = analyze(system=_SYSTEM, user=user, schema=FundamentalRead, max_tokens=2000)
        if result is not None:
            result.symbol = result.symbol or symbol
            # The calendar is the single source of truth for stand-aside windows — replace the
            # model's (which can duplicate or mis-time the same event).
            result.stand_aside_windows = _high_impact_windows(events, now)
            log.info("fundamental read via LLM", extra={"symbol": symbol, "bias": result.bias.value})
            return result

    read = _deterministic_read(symbol, now)
    log.info("fundamental read deterministic", extra={"symbol": symbol, "bias": read.bias.value})
    return read
