"""Tests for the Fundamental Analyst (deterministic path) + provider stubs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.fundamental import run_fundamental
from app.data import providers
from app.data.providers import CalendarEvent, SentimentScore, StubCalendarProvider, StubSentimentProvider
from app.models.enums import TradingBias

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


class _PosSentiment(StubSentimentProvider):
    def get_sentiment(self, symbol):
        return SentimentScore(symbol=symbol, score=0.6, sample_size=40, summary="bullish chatter")


class _NegSentiment(StubSentimentProvider):
    def get_sentiment(self, symbol):
        return SentimentScore(symbol=symbol, score=-0.6, sample_size=40, summary="bearish chatter")


def teardown_function():
    # Restore default providers after each test.
    providers.set_providers(
        news=providers.StubNewsProvider(),
        calendar=providers.StubCalendarProvider(),
        sentiment=providers.StubSentimentProvider(),
    )


def test_neutral_by_default():
    read = run_fundamental("AAPL", now=NOW)
    assert read.bias == TradingBias.NEUTRAL
    assert read.stand_aside_windows == []


def _fake_read(bias=TradingBias.BULLISH):
    from app.models.schemas import FundamentalRead
    return FundamentalRead(symbol="AAPL", bias=bias, key_drivers=[], surprise_assessment="x",
                           stand_aside_windows=[], confidence=0.3, notes="llm")


def test_fundamental_llm_read_is_cached(monkeypatch):
    # The ~2000-token LLM read is cached per symbol -> a repeat analysis makes NO second call.
    import app.agents.fundamental as fund
    monkeypatch.setattr(fund, "llm_available", lambda: True)
    calls = {"n": 0}
    monkeypatch.setattr(fund, "analyze", lambda **k: (calls.__setitem__("n", calls["n"] + 1) or _fake_read()))
    r1 = run_fundamental("AAPL", now=NOW)
    r2 = run_fundamental("AAPL", now=NOW)
    assert calls["n"] == 1 and r1.bias == TradingBias.BULLISH and r2.bias == TradingBias.BULLISH


def test_fundamental_cache_keeps_windows_fresh(monkeypatch):
    # SAFETY: even a cache hit must pick up a NEWLY-added high-impact event window (windows never cached).
    import app.agents.fundamental as fund
    monkeypatch.setattr(fund, "llm_available", lambda: True)
    calls = {"n": 0}
    monkeypatch.setattr(fund, "analyze", lambda **k: (calls.__setitem__("n", calls["n"] + 1) or _fake_read()))
    r1 = run_fundamental("AAPL", now=NOW)
    assert r1.stand_aside_windows == [] and calls["n"] == 1
    event = CalendarEvent(label="FOMC", when=NOW, importance="high", forecast=5.0, previous=5.0)
    providers.set_providers(calendar=StubCalendarProvider([event]))
    r2 = run_fundamental("AAPL", now=NOW)          # cache hit for the bias ...
    assert calls["n"] == 1                          # ... no second LLM call ...
    assert len(r2.stand_aside_windows) == 1         # ... but the fresh news window IS applied


def test_bias_follows_sentiment():
    providers.set_providers(sentiment=_PosSentiment())
    assert run_fundamental("AAPL", now=NOW).bias == TradingBias.BULLISH
    providers.set_providers(sentiment=_NegSentiment())
    assert run_fundamental("AAPL", now=NOW).bias == TradingBias.BEARISH


def test_high_impact_event_creates_stand_aside_window():
    event = CalendarEvent(label="FOMC", when=NOW, importance="high", forecast=5.0, previous=5.0)
    providers.set_providers(calendar=StubCalendarProvider([event]))
    read = run_fundamental("AAPL", now=NOW)
    assert len(read.stand_aside_windows) == 1
    w = read.stand_aside_windows[0]
    assert w.label == "FOMC" and w.start < NOW < w.end


def test_low_impact_event_no_window():
    event = CalendarEvent(label="minor", when=NOW, importance="low")
    providers.set_providers(calendar=StubCalendarProvider([event]))
    assert run_fundamental("AAPL", now=NOW).stand_aside_windows == []


def _nfp_beat() -> CalendarEvent:
    # Strong US jobs surprise released an hour ago (actual >> forecast).
    return CalendarEvent(label="US: Non Farm Payrolls", when=NOW - timedelta(hours=1),
                         importance="high", country="US", forecast=100.0, previous=90.0, actual=180.0)


def test_surprise_bias_bearish_for_eurusd_on_strong_us_data():
    providers.set_providers(calendar=StubCalendarProvider([_nfp_beat()]))
    r = run_fundamental("EURUSD", now=NOW)
    assert r.bias == TradingBias.BEARISH  # strong USD (quote) -> EUR/USD down
    assert any("Non Farm" in d for d in r.key_drivers)


def test_surprise_bias_bullish_for_usdjpy_on_strong_us_data():
    providers.set_providers(calendar=StubCalendarProvider([_nfp_beat()]))
    assert run_fundamental("USDJPY", now=NOW).bias == TradingBias.BULLISH  # strong USD (base)


def test_surprise_ignored_when_too_old():
    old = _nfp_beat()
    old.when = NOW - timedelta(hours=20)  # outside the surprise window
    providers.set_providers(calendar=StubCalendarProvider([old]))
    assert run_fundamental("EURUSD", now=NOW).bias == TradingBias.NEUTRAL


def test_calendar_country_mapping():
    from app.data.providers import TradingViewCalendarProvider
    p = TradingViewCalendarProvider()
    assert set(p._countries("EURUSDm")) == {"EU", "US"}
    assert set(p._countries("USDJPYm")) == {"US", "JP"}
    assert p._countries("XAUUSDm") == ["US"]      # gold -> USD-driven
    assert set(p._countries("GBPAUDm")) == {"GB", "AU"}
