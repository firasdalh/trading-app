"""Tests for the macro / news / positioning layer (Forex Factory integration).

The two that matter most are at the bottom: this layer must never emit a direction. Everything
else is parsing and arithmetic; those two are the reason the layer is allowed to exist at all.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.data.forexfactory import ForexFactoryCalendarProvider, event_key, parse_number
from app.data.providers import CalendarEvent, countries_for_symbol, currencies_for_symbol
from app.macro.engine import MacroWindows, assess, band_for
from app.macro.positioning import PositioningSnapshot, build_context

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 1, 12, 30, tzinfo=UTC)


def ev(minutes: int, impact: str = "high", country: str = "US", title: str = "CPI m/m",
       **kw) -> CalendarEvent:
    return CalendarEvent(label=title, when=T0 + dt.timedelta(minutes=minutes),
                         importance=impact, country=country, **kw)


# --------------------------------------------------------------------- parsing


@pytest.mark.parametrize("raw,expected", [
    ("3.6%", 3.6), ("-0.3%", -0.3), ("1.25K", 1250.0), ("222B", 222e9),
    ("<0.1%", 0.1), ("1,234", 1234.0), ("", None), (None, None), ("--", None), ("n/a", None),
])
def test_parse_number_handles_forex_factory_display_values(raw, expected):
    assert parse_number(raw) == expected


def test_parse_normalises_timestamps_to_utc():
    """The feed publishes US/Eastern offsets; everything internal must be UTC."""
    rows = [{"title": "CPI m/m", "country": "USD", "impact": "High",
             "date": "2026-10-01T08:30:00-04:00", "forecast": "3.1%", "previous": "2.9%"}]
    out = ForexFactoryCalendarProvider()._parse(rows)
    assert len(out) == 1
    assert out[0].when == dt.datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    assert out[0].when.tzinfo is UTC
    assert out[0].country == "US"          # currency code mapped to the app's country code
    assert out[0].importance == "high"
    assert out[0].forecast == 3.1 and out[0].previous == 2.9
    assert out[0].actual is None           # this feed never carries actuals


def test_parse_skips_malformed_rows_without_losing_the_week():
    rows = [{"title": "bad", "country": "USD", "impact": "High", "date": "not-a-date"},
            {"title": "ok", "country": "EUR", "impact": "Low", "date": "2026-10-01T08:30:00-04:00"}]
    out = ForexFactoryCalendarProvider()._parse(rows)
    assert [e.label for e in out] == ["ok"]


def test_impact_mapping_covers_holiday():
    rows = [{"title": "Bank Holiday", "country": "EUR", "impact": "Holiday",
             "date": "2026-10-01T08:30:00-04:00"}]
    assert ForexFactoryCalendarProvider()._parse(rows)[0].importance == "holiday"


def test_event_key_is_stable_and_unique():
    a = event_key("CPI m/m", "usd", T0)
    assert a == event_key(" CPI M/M ", "USD", T0)         # same event, refetched
    assert a != event_key("CPI m/m", "EUR", T0)           # different currency
    assert a != event_key("CPI m/m", "USD", T0 + dt.timedelta(minutes=1))


# ------------------------------------------------------------- symbol matching


@pytest.mark.parametrize("symbol,asset,expect", [
    ("EURUSDm", "forex", {"EUR", "USD"}),
    ("GBPUSDm", "forex", {"GBP", "USD"}),
    ("USDJPYm", "forex", {"USD", "JPY"}),
    ("USDCHFm", "forex", {"USD", "CHF"}),
    ("AUDUSDm", "forex", {"AUD", "USD"}),
    ("NZDUSDm", "forex", {"NZD", "USD"}),
    ("USDCADm", "forex", {"USD", "CAD"}),
    ("AUDNZDm", "forex", {"AUD", "NZD"}),
    ("XAUUSDm", "metal", {"USD"}),
    ("DE30m", "index", {"EUR"}),
    ("JP225m", "index", {"JPY"}),
    ("USOILm", "energy", {"USD"}),
])
def test_currency_matching_is_pair_by_pair(symbol, asset, expect):
    assert set(currencies_for_symbol(symbol, asset)) == expect


def test_cadjpy_does_not_pick_up_the_us_calendar():
    """"CADJPYm" contains "DJ" (the Dow Jones index token).

    Matched as a bare substring, that attached the US calendar to a CAD/JPY trade — which with a
    news gate in front of it would hold the pair for a US CPI release that cannot move it.
    """
    assert set(currencies_for_symbol("CADJPYm", "forex")) == {"CAD", "JPY"}
    assert "US" not in countries_for_symbol("CADJPYm", "forex")


# ------------------------------------------------------------------ news risk


def test_quiet_calendar_scores_zero_and_proceeds():
    r = assess([ev(600)], base_currency="EUR", quote_currency="USD", now=T0)
    assert r.score == 0 and r.action == "proceed"


def test_score_rises_as_the_event_approaches():
    scores = [assess([ev(m)], base_currency="EUR", quote_currency="USD", now=T0).score
              for m in (240, 120, 90, 60, 30)]
    assert scores == sorted(scores), scores           # monotonic, no cliff
    assert scores[0] == 0 and scores[-1] >= 70


def test_high_impact_inside_the_lock_window_says_wait():
    r = assess([ev(18)], base_currency="EUR", quote_currency="USD", now=T0)
    assert r.action == "wait"
    assert r.band in ("HIGH", "SEVERE")
    assert "CPI" in r.reason


def test_just_released_event_keeps_the_lock_then_releases_it():
    """Post-news the tape is at its most hostile; after lock_after it is normal again."""
    inside = assess([ev(-10)], base_currency="EUR", quote_currency="USD", now=T0)
    outside = assess([ev(-45)], base_currency="EUR", quote_currency="USD", now=T0)
    assert inside.action == "wait"
    assert outside.action != "wait"


def test_medium_impact_alone_does_not_lock():
    r = assess([ev(20, impact="medium", title="Claims")],
               base_currency="EUR", quote_currency="USD", now=T0)
    assert r.action != "wait"


def test_both_legs_scores_above_one_leg():
    one = assess([ev(60)], base_currency="EUR", quote_currency="USD", now=T0)
    two = assess([ev(60), ev(55, impact="medium", country="EU", title="PMI")],
                 base_currency="EUR", quote_currency="USD", now=T0)
    assert two.both_sides and not one.both_sides
    assert two.score > one.score


def test_minutes_away_and_currency_are_reported_for_the_ui():
    r = assess([ev(24)], base_currency="EUR", quote_currency="USD", now=T0)
    assert r.next_high is not None
    assert r.next_high.currency == "USD"          # country code converted to a currency
    assert r.next_high.minutes_away == pytest.approx(24, abs=0.1)
    assert r.affects_quote and not r.affects_base


@pytest.mark.parametrize("score,band", [(0, "LOW"), (20, "LOW"), (21, "MODERATE"), (41, "ELEVATED"),
                                        (61, "HIGH"), (81, "SEVERE"), (100, "SEVERE")])
def test_band_thresholds(score, band):
    assert band_for(score) == band


def test_windows_are_configurable():
    """A 5-minute lock must not hold a trade 18 minutes out — the thresholds are ablation knobs."""
    tight = MacroWindows(warning_minutes=30, lock_before_minutes=5, wait_score=95)
    assert assess([ev(18)], base_currency="EUR", quote_currency="USD",
                  now=T0, windows=tight).action != "wait"


def test_unavailable_feed_is_reported_not_faked():
    r = assess([], base_currency="EUR", quote_currency="USD", now=T0, available=False)
    assert r.available is False
    assert "UNAVAILABLE" in r.note
    assert r.score == 0 and r.next_high is None      # no invented events


def test_stale_feed_is_flagged():
    r = assess([ev(600)], base_currency="EUR", quote_currency="USD", now=T0, stale=True)
    assert r.stale and "stale" in r.note.lower()


# ---------------------------------------------------------------- positioning


def test_positioning_percentages_and_imbalances():
    s = PositioningSnapshot(symbol="EURUSD", long_traders_pct=59, short_traders_pct=41,
                            long_lots_pct=76, short_lots_pct=24)
    assert s.trader_imbalance == 18          # the spec's worked example
    assert s.lot_imbalance == 52


def test_crowding_is_a_magnitude_not_a_direction():
    """A 76% long book and a 76% short book are equally crowded. Any asymmetry here would be a
    disguised directional signal."""
    long_side = PositioningSnapshot(symbol="X", long_lots_pct=76, short_lots_pct=24,
                                    long_traders_pct=59, short_traders_pct=41)
    short_side = PositioningSnapshot(symbol="X", long_lots_pct=24, short_lots_pct=76,
                                     long_traders_pct=41, short_traders_pct=59)
    assert (build_context(long_side, []).crowding_score
            == build_context(short_side, []).crowding_score)


def test_crowding_rises_with_imbalance():
    mild = PositioningSnapshot(symbol="X", long_lots_pct=55, short_lots_pct=45)
    heavy = PositioningSnapshot(symbol="X", long_lots_pct=90, short_lots_pct=10)
    assert build_context(heavy, []).crowding_score > build_context(mild, []).crowding_score


def test_percentile_needs_history_before_it_claims_anything():
    s = PositioningSnapshot(symbol="X", long_lots_pct=76, short_lots_pct=24)
    thin = build_context(s, [])
    assert thin.percentile is None and "building history" in thin.note
    hist = [PositioningSnapshot(symbol="X", long_lots_pct=55, short_lots_pct=45) for _ in range(25)]
    rich = build_context(s, hist)
    assert rich.percentile == 100.0 and rich.samples == 25


def test_same_reading_is_more_crowded_when_it_is_unusual_for_the_pair():
    """The point of the history: 70% long means nothing on a pair that lives at 70% long."""
    s = PositioningSnapshot(symbol="X", long_lots_pct=76, short_lots_pct=24)
    normal = [PositioningSnapshot(symbol="X", long_lots_pct=76, short_lots_pct=24) for _ in range(25)]
    unusual = [PositioningSnapshot(symbol="X", long_lots_pct=52, short_lots_pct=48) for _ in range(25)]
    assert build_context(s, unusual).crowding_score >= build_context(s, normal).crowding_score


def test_missing_positioning_says_so():
    c = build_context(None)
    assert c.available is False and "UNAVAILABLE" in c.note
    assert c.crowding_score == 0 and c.crowding_band == "UNKNOWN"


def test_stale_positioning_is_flagged():
    old = PositioningSnapshot(symbol="X", long_lots_pct=76, short_lots_pct=24,
                              source_timestamp=T0 - dt.timedelta(hours=9))
    c = build_context(old, [], now=T0)
    assert c.stale and "STALE" in c.note


# ============================================================================
#  THE TWO RULES. Everything above is arithmetic; these are why the layer may exist.
# ============================================================================

_DIRECTION_WORDS = ("buy", "sell", "long", "short", "bullish", "bearish")


def test_59_percent_long_never_produces_sell():
    """The spec's own example. Crowding is fragility, not a fade signal — crowded markets stay
    crowded for months, and 59% long is barely crowded at all."""
    s = PositioningSnapshot(symbol="EURUSD", long_traders_pct=59, short_traders_pct=41,
                            long_lots_pct=76, short_lots_pct=24, source="test")
    c = build_context(s, [])
    payload = c.as_dict()

    # No field anywhere in the payload is an instruction.
    assert "action" not in payload and "signal" not in payload and "direction" not in payload
    assert "recommendation" not in payload
    # The one descriptive string names a SIDE, but only as "…-crowded", never as an imperative.
    assert payload["bias_label"] == "strongly long-crowded"
    for word in ("sell", "buy", "fade", "short ", "long "):
        assert word not in payload["bias_label"].lower().replace("long-", "").replace("short-", "")
    assert payload["disclaimer"] == "Positioning is context only - not a directional signal."


def test_high_impact_event_soon_never_produces_a_direction():
    """An approaching release is a volatility event of unknown sign. The only honest outputs are
    proceed / warn / wait — never a side."""
    r = assess([ev(18)], base_currency="EUR", quote_currency="USD", now=T0)
    payload = r.as_dict()
    assert r.action in ("proceed", "warn", "wait")
    assert payload.get("action") == "wait"
    for key in ("direction", "side", "signal", "bias", "recommendation", "trade"):
        assert key not in payload, f"news risk must not expose {key!r}"
    # And it must not smuggle one into the prose either.
    assert not any(w in r.reason.lower() for w in _DIRECTION_WORDS)


def test_news_risk_is_symmetric_for_longs_and_shorts():
    """The same clock is equally risky whichever way you were going to trade. If this ever differs,
    the gate has become a signal."""
    r = assess([ev(18)], base_currency="EUR", quote_currency="USD", now=T0)
    again = assess([ev(18)], base_currency="EUR", quote_currency="USD", now=T0)
    assert r.score == again.score and r.action == again.action
    # There is no parameter by which a caller could ask for a direction-specific answer.
    import inspect
    assert "direction" not in inspect.signature(assess).parameters


# ------------------------------------------------------- service integration


@pytest.fixture
def macro_db(db_session, monkeypatch):
    """A session with the calendar seeded locally and the network provider silenced."""
    from app.macro import service
    from app.models.db import EconomicEventRow

    class _Dead:
        name, url = "forexfactory", "test://offline"

        def all_events(self):
            return []

        def status(self):
            from app.data.forexfactory import FeedStatus
            return FeedStatus(ok=False, events=0, error="offline in tests")

    monkeypatch.setattr(service, "_ff", _Dead())
    for title, ccy, country, impact, off in [
        ("CPI m/m", "USD", "US", "high", 0),
        ("ECB Press Conference", "EUR", "EU", "high", 90),
        ("Unemployment Claims", "USD", "US", "medium", -20),
    ]:
        when = T0 + dt.timedelta(minutes=off)
        db_session.add(EconomicEventRow(
            event_key=event_key(title, ccy, when), title=title, currency=ccy, country=country,
            impact=impact, when_utc=when, forecast=3.1, previous=2.9, source="seed"))
    db_session.commit()
    return db_session


def test_stored_calendar_is_used_when_the_live_feed_is_down(macro_db):
    """A 429 must not blank the calendar: a quiet week and an unreadable feed look identical, and
    only one of them is safe to trade through."""
    from app.macro.service import macro_context
    c = macro_context(macro_db, "EURUSDm", "forex", now=T0 - dt.timedelta(minutes=18))
    assert c["source_mode"] == "cached"
    assert c["news"]["available"] is True
    assert c["action"] == "wait"
    assert "CPI" in c["gate_reason"]


def test_gate_annotates_but_does_not_block_by_default(macro_db):
    """An unmeasured filter that silently changes which trades get taken destroys the comparison
    that would show whether it helps (see analysis/strategy_review_2026-09-23.md)."""
    from app.macro.service import macro_context
    c = macro_context(macro_db, "EURUSDm", "forex", now=T0 - dt.timedelta(minutes=18))
    assert c["action"] == "wait"
    assert c["blocked"] is False


def test_block_new_trades_flag_turns_the_gate_on(macro_db):
    from app.macro.service import get_macro_config, macro_context
    cfg = get_macro_config(macro_db)
    cfg.block_new_trades = True
    macro_db.commit()
    c = macro_context(macro_db, "EURUSDm", "forex", now=T0 - dt.timedelta(minutes=18))
    assert c["action"] == "wait" and c["blocked"] is True


def test_fail_mode_block_new_trades_holds_when_there_is_no_data(db_session, monkeypatch):
    from app.macro import service
    from app.macro.service import get_macro_config, macro_context

    class _Dead:
        name, url = "forexfactory", "test://offline"

        def all_events(self):
            return []

        def status(self):
            from app.data.forexfactory import FeedStatus
            return FeedStatus(ok=False, events=0, error="offline")

    monkeypatch.setattr(service, "_ff", _Dead())
    cfg = get_macro_config(db_session)
    cfg.fail_mode = "block_new_trades"
    db_session.commit()
    c = macro_context(db_session, "EURUSDm", "forex", now=T0)
    assert c["action"] == "wait" and c["blocked"] is True
    assert "UNAVAILABLE" in c["gate_reason"]


def test_fail_mode_allow_with_warning_keeps_trading(db_session, monkeypatch):
    from app.macro import service
    from app.macro.service import macro_context

    class _Dead:
        name, url = "forexfactory", "test://offline"

        def all_events(self):
            return []

        def status(self):
            from app.data.forexfactory import FeedStatus
            return FeedStatus(ok=False, events=0, error="offline")

    monkeypatch.setattr(service, "_ff", _Dead())
    c = macro_context(db_session, "EURUSDm", "forex", now=T0)
    assert c["blocked"] is False and c["action"] == "warn"


def test_sync_is_idempotent_and_does_not_duplicate_events(db_session, monkeypatch):
    """The same week is refetched every 30 minutes; without a stable key that inserts the whole
    calendar again on every poll."""
    from app.macro import service
    from app.models.db import EconomicEventRow

    class _Feed:
        name, url = "forexfactory", "test://feed"

        def all_events(self):
            return [ev(30), ev(90, country="EU", title="ECB Press Conference")]

        def status(self):
            from app.data.forexfactory import FeedStatus
            return FeedStatus(ok=True, events=2)

    monkeypatch.setattr(service, "_ff", _Feed())
    first = service.sync_events(db_session)
    second = service.sync_events(db_session)
    assert first["inserted"] == 2
    assert second["inserted"] == 0 and second["updated"] == 2
    assert db_session.query(EconomicEventRow).count() == 2


def test_sync_updates_a_revised_forecast_in_place(db_session, monkeypatch):
    from app.macro import service
    from app.models.db import EconomicEventRow

    class _Feed:
        name, url = "forexfactory", "test://feed"
        forecast = 3.1

        def all_events(self):
            return [ev(30, forecast=self.forecast)]

        def status(self):
            from app.data.forexfactory import FeedStatus
            return FeedStatus(ok=True, events=1)

    feed = _Feed()
    monkeypatch.setattr(service, "_ff", feed)
    service.sync_events(db_session)
    feed.forecast = 3.4                      # the site revises during the week
    service.sync_events(db_session)
    rows = db_session.query(EconomicEventRow).all()
    assert len(rows) == 1 and rows[0].forecast == 3.4


def test_every_decision_records_its_macro_state(macro_db):
    """Section 17 is impossible without this row: you cannot ask afterwards whether the news layer
    improved expectancy if you never recorded what it saw."""
    from app.macro.service import macro_context, record_trade_macro
    from app.models.db import TradeMacroContext

    now = T0 - dt.timedelta(minutes=18)
    ctx = macro_context(macro_db, "EURUSDm", "forex", now=now)
    row = record_trade_macro(macro_db, symbol="EURUSDm", context=ctx, proposal_id=None,
                             direction="long", technical_confidence=0.76)
    assert row is not None
    stored = macro_db.query(TradeMacroContext).one()
    assert stored.symbol == "EURUSDM"
    assert stored.news_action == "wait"
    assert stored.event_name == "CPI m/m" and stored.event_currency == "USD"
    assert stored.minutes_to_event == pytest.approx(18, abs=0.1)
    assert stored.technical_confidence == 0.76
    assert stored.direction == "long"        # recorded, never decided, by this layer
    assert stored.payload["news"]["score"] == ctx["news"]["score"]


def test_journal_write_failure_never_breaks_the_trade_path(db_session):
    from app.macro.service import record_trade_macro
    assert record_trade_macro(db_session, symbol="X", context={"news": "not-a-dict"}) is None


def test_positioning_snapshots_accumulate_without_duplicating(db_session):
    from app.models.db import PositioningSnapshotRow
    for _ in range(3):
        db_session.add(PositioningSnapshotRow(symbol="EURUSD", long_lots_pct=76,
                                              short_lots_pct=24, source="test"))
    db_session.commit()
    rows = db_session.query(PositioningSnapshotRow).all()
    assert len(rows) == 3                       # history is a time series, not a unique set
    assert len({r.id for r in rows}) == 3
