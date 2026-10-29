"""Trading hours learned from bar history: next close / reopen, the kind of closure, and the New York
time projection that follows US clock changes."""
from datetime import datetime, timedelta, timezone

from app.data.market_hours import TradingHours, describe

UTC = timezone.utc
STEP = timedelta(minutes=5)


def _bars(start: datetime, end: datetime, closed) -> list[datetime]:
    out, t = [], start
    while t < end:
        if not closed(t):
            out.append(t)
        t += STEP
    return out


def _fx_closed(t: datetime) -> bool:
    # Exness FX in US summer time: Fri 21:00 -> Sun 21:05 UTC.
    hm = (t.hour, t.minute)
    return (t.weekday() == 4 and hm >= (21, 0)) or t.weekday() == 5 or (t.weekday() == 6 and hm < (21, 5))


def _metal_closed(t: datetime) -> bool:
    # XAUUSDm / USOILm / the index CFDs in US summer time: a daily 21:00-22:00 UTC break, and the
    # weekend from Fri 21:00 to Sun 22:00.
    wd, h = t.weekday(), t.hour
    return wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21) or (wd < 4 and h == 21)


def _stock_closed(t: datetime) -> bool:
    # AAPLm: 10:00-19:50 UTC, Monday to Friday.
    return t.weekday() >= 5 or (t.hour, t.minute) < (10, 0) or (t.hour, t.minute) >= (19, 50)


def _zinc_closed(t: datetime) -> bool:
    # XZNUSDm (LME zinc) keeps London time: 01:00-19:00 London, Monday to Friday - 00:00-18:00 UTC
    # until UK clocks went back on 2026-10-25, 01:00-19:00 UTC after.
    o, c = (0, 18) if t < _at("2026-10-25T01:00") else (1, 19)
    return t.weekday() >= 5 or not (o <= t.hour < c)


def _at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


DATA_START = _at("2026-09-27T00:00")   # a Sunday, well inside US summer time
DATA_END = _at("2026-10-28T00:00")     # Wednesday 00:00


def _model(closed, extra_gap=None) -> TradingHours:
    gap = extra_gap or (lambda t: False)
    return TradingHours(_bars(DATA_START, DATA_END, lambda t: closed(t) or gap(t)))


def test_weekday_close_is_the_weekend_close_and_reopen_follows_new_york_time():
    h = describe(_model(_fx_closed), live=True, now=_at("2026-10-30T18:00"))   # Friday
    assert h["closes_at"] == "2026-10-30T21:00:00+00:00"
    assert h["closure"] == "weekend"
    # US clocks go back on Sunday Nov 1: 17:05 New York is then 22:05 UTC, not 21:05.
    assert h["opens_at"] == "2026-11-01T22:05:00+00:00"
    assert h["always_open"] is False


def test_daily_break_while_open_and_while_closed():
    m = _model(_metal_closed)
    open_ = describe(m, live=True, now=_at("2026-10-27T19:00"))               # Tuesday
    assert (open_["closes_at"], open_["opens_at"], open_["closure"]) == (
        "2026-10-27T21:00:00+00:00", "2026-10-27T22:00:00+00:00", "daily_break")
    shut = describe(m, live=False, now=_at("2026-10-27T21:30"))
    assert shut["opens_at"] == "2026-10-27T22:00:00+00:00"
    assert shut["closure"] == "daily_break"
    assert shut["off_schedule"] is False


def test_us_clock_change_moves_the_daily_break_with_new_york():
    # Learned in summer time; after Nov 1 the 17:00-18:00 New York break is 22:00-23:00 UTC.
    h = describe(_model(_metal_closed), live=True, now=_at("2026-11-03T19:00"))
    assert h["closes_at"] == "2026-11-03T22:00:00+00:00"
    assert h["opens_at"] == "2026-11-03T23:00:00+00:00"


def test_overnight_and_weekend_closures_are_named():
    m = _model(_stock_closed)
    night = describe(m, live=False, now=_at("2026-10-27T20:30"))              # Tuesday evening
    assert night["opens_at"] == "2026-10-28T10:00:00+00:00"
    assert night["closure"] == "overnight"
    weekend = describe(m, live=False, now=_at("2026-10-31T12:00"))            # Saturday
    assert weekend["closure"] == "weekend"
    # Monday 10:00 UTC was 06:00 New York in summer time - after the change that is 11:00 UTC.
    assert weekend["opens_at"] == "2026-11-02T11:00:00+00:00"


def test_a_london_clock_symbol_is_read_on_london_time():
    # Wednesday 00:40 UTC, after the UK change: zinc now opens at 01:00 UTC. Read on New York time it
    # would still be "open since 00:00" and look closed outside its hours.
    m = _model(_zinc_closed)
    assert str(m.zone) == "Europe/London"
    h = describe(m, live=False, now=_at("2026-10-28T00:40"))
    assert h["scheduled_open"] is False and h["off_schedule"] is False
    assert h["opens_at"] == "2026-10-28T01:00:00+00:00"
    assert h["closure"] == "overnight"


def test_new_york_wins_when_the_data_cannot_tell():
    # No US clock change inside the learned weeks: New York and UTC fit equally - FX convention wins.
    assert str(_model(_fx_closed).zone) == "America/New_York"


def test_round_the_clock_market_is_always_open():
    h = describe(_model(lambda t: False), live=True, now=_at("2026-10-28T12:00"))
    assert h["always_open"] is True
    assert h["closes_at"] is None


def test_a_one_off_gap_is_outvoted():
    # A 20-minute feed gap on one Monday must not become a "close" on the next.
    once = lambda t: t.date() == _at("2026-10-19T00:00").date() and (11, 30) <= (t.hour, t.minute) < (11, 50)  # noqa: E731
    h = describe(_model(_metal_closed, once), live=True, now=_at("2026-10-26T10:00"))
    assert h["closes_at"] == "2026-10-26T21:00:00+00:00"


def test_last_weeks_holiday_is_outvoted():
    holiday = lambda t: t.date() == _at("2026-10-19T00:00").date()  # noqa: E731
    m = _model(_metal_closed, holiday)
    h = describe(m, live=True, now=_at("2026-10-26T10:00"))
    assert h["scheduled_open"] is True
    assert h["closes_at"] == "2026-10-26T21:00:00+00:00"


def test_closed_when_it_should_be_open_says_so():
    # A holiday TODAY: no prices although the schedule says open - flagged, with the usual reopen.
    h = describe(_model(_metal_closed), live=False, now=_at("2026-10-27T12:00"))
    assert h["off_schedule"] is True
    assert h["closure"] == "unscheduled"
    assert h["opens_at"] == "2026-10-27T22:00:00+00:00"


def test_no_history_gives_live_status_only():
    h = describe(TradingHours([]), live=True, now=_at("2026-10-27T12:00"))
    assert h["open"] is True
    assert h["closes_at"] is None and h["opens_at"] is None and h["scheduled_open"] is None
