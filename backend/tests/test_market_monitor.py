"""Tests for the watchlist monitor (app/agents/market_monitor.py, /api/monitor)."""
from __future__ import annotations

import datetime as dt

import pytest

import app.agents.market_monitor as mm
from app.agents.market_monitor import bias_of, notes_for, read_timeframe
from app.models.db import WatchItem
from app.models.schemas import Candle, OHLCVSeries
from tests.test_order_blocks import bullish_setup

T0 = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_table():
    mm.clear_cache()
    yield
    mm.clear_cache()


def _series(candles, tf="1h"):
    return OHLCVSeries(symbol="TEST", timeframe=tf, candles=candles)


def _trend(n=260, step=0.25, px=100.0):
    """A steady staircase with shallow pullbacks - the textbook uptrend (or downtrend if step < 0)."""
    out = []
    for i in range(n):
        drift = step if i % 5 != 4 else -step * 0.6
        o = px
        px = px + drift
        hi, lo = max(o, px) + abs(step) * 0.3, min(o, px) - abs(step) * 0.3
        out.append(Candle(ts=T0 + dt.timedelta(hours=i), open=o, high=hi, low=lo, close=px, volume=1000))
    return out


@pytest.mark.parametrize("h1,h4,d1,expected", [
    ("up", "up", "up", "aligned_up"),
    ("down", "down", "down", "aligned_down"),
    ("down", "up", "up", "pullback_up"),        # 1h dipping inside a 4h + 1d uptrend
    ("up", "down", "down", "pullback_down"),
    ("up", "up", "down", "counter_up"),         # 1h + 4h fighting the daily
    ("up", "down", "up", "mixed"),
    (None, None, None, "mixed"),
    # A timeframe that could not be read is NOT "against" - these used to print a dip / a fight with
    # the daily for a timeframe nobody had measured.
    (None, "up", "up", "mixed"),
    ("up", "up", None, "mixed"),
    ("sideways", "down", "down", "mixed"),
])
def test_bias_labels(h1, h4, d1, expected):
    assert bias_of({"1h": h1, "4h": h4, "1d": d1}) == expected


def test_uptrend_reads_up_with_stacked_emas():
    r = read_timeframe(_series(_trend()))
    assert r["trend"] == "up"
    assert r["ema_stacked"] is True
    assert r["price_vs_ema200"] == "above"
    assert r["rsi"] > 50 and r["rsi_zone"] in ("bullish", "overbought")


def test_downtrend_reads_down():
    r = read_timeframe(_series(_trend(step=-0.25, px=200.0)))
    assert r["trend"] == "down"
    assert r["rsi"] < 50


def test_too_little_history_reads_nothing():
    assert read_timeframe(_series(_trend(n=30))) is None


def test_price_back_in_a_demand_zone_is_flagged_inside():
    cs = bullish_setup()                                   # origin candle low 98.0 .. ~100.2
    px = cs[-1].close
    n = len(cs)
    # drift back down into the zone without closing through its far side
    for i in range(12):
        o = px
        px = max(99.0, px - 1.4)
        cs.append(Candle(ts=cs[-1].ts + dt.timedelta(hours=1), open=o, high=o + 0.1, low=px - 0.1, close=px))
    assert len(cs) == n + 12
    r = read_timeframe(_series(cs))
    assert r is not None
    assert r["zones"]["inside"] is not None
    assert r["zones"]["inside"]["kind"] == "demand"


def _row(bias, **tfs):
    base = {"trend": None, "rsi": 55.0, "state": "range", "choch": False, "divergence": None,
            "strength": "moderate", "zones": {"inside": None, "demand": None, "supply": None}}
    return {"bias": bias, "market_open": True, "events_soon": None, "range_used": 0.5,
            "tfs": {tf: {**base, **v} for tf, v in tfs.items()}}


def test_notes_describe_a_pullback_inside_the_higher_trend():
    row = _row("pullback_up", **{"1h": {"trend": "down", "state": "pullback"},
                                 "4h": {"trend": "up"}, "1d": {"trend": "up"}})
    texts = [n["text"] for n in notes_for(row)]
    assert any("dipping against a 4H + 1D uptrend" in t for t in texts)


def test_notes_flag_a_zone_against_the_bias():
    row = _row("aligned_up", **{"1h": {"trend": "up"}, "1d": {"trend": "up"},
                                "4h": {"trend": "up", "zones": {"inside": {"kind": "supply", "strength": 70},
                                                                 "demand": None, "supply": None}}})
    notes = notes_for(row)
    assert any(n["tone"] == "caution" and "inside a 4H supply zone, against the up bias" in n["text"] for n in notes)


def test_notes_flag_stretched_momentum_in_the_direction_of_travel():
    row = _row("aligned_down", **{"1h": {"trend": "down", "rsi": 24.0}, "4h": {"trend": "down"},
                                  "1d": {"trend": "down"}})
    assert any("oversold" in n["text"] for n in notes_for(row))
    # ...but an oversold RSI is NOT flagged as stretched for an UP bias (it isn't the chase)
    row2 = _row("aligned_up", **{"1h": {"trend": "up", "rsi": 24.0}, "4h": {"trend": "up"}, "1d": {"trend": "up"}})
    assert not any("oversold" in n["text"] for n in notes_for(row2))


def test_notes_never_tell_you_to_trade():
    row = _row("aligned_up", **{"1h": {"trend": "up", "rsi": 80.0, "choch": True},
                                "4h": {"trend": "up", "divergence": "bear"}, "1d": {"trend": "up"}})
    row["events_soon"] = "US: CPI ~45m (high)"
    row["range_used"] = 1.4
    for n in notes_for(row):
        first = n["text"].split()[0].lower()
        assert first not in ("buy", "sell", "go", "enter", "short", "long")


def test_endpoint_returns_one_row_per_pair_with_all_timeframes(db_session):
    from app.api.monitor_routes import monitor

    db_session.add_all([
        WatchItem(symbol="EURUSD", asset_class="forex", timeframe="1h", enabled=True),
        WatchItem(symbol="EURUSD", asset_class="forex", timeframe="4h", enabled=True),   # same pair twice
        WatchItem(symbol="GBPUSD", asset_class="forex", timeframe="1h", enabled=False),  # disabled still watched
    ])
    db_session.commit()
    out = monitor(refresh=False, session=db_session)
    assert out["timeframes"] == ["1h", "4h", "1d"]
    assert [r["symbol"] for r in out["rows"]] == ["EURUSD", "GBPUSD"]
    for r in out["rows"]:
        assert set(r["tfs"]) == {"1h", "4h", "1d"}
        assert r["bias"] in {"aligned_up", "aligned_down", "pullback_up", "pullback_down",
                             "counter_up", "counter_down", "mixed"}
        assert isinstance(r["notes"], list)


def test_table_is_built_once_within_its_ttl(db_session, monkeypatch):
    from app.api.monitor_routes import monitor

    db_session.add(WatchItem(symbol="EURUSD", asset_class="forex", timeframe="1h", enabled=True))
    db_session.commit()
    calls = []
    real = mm.read_pair
    monkeypatch.setattr("app.api.monitor_routes.read_pair",
                        lambda b, s, a: calls.append(s) or real(b, s, a))
    monitor(refresh=False, session=db_session)
    monitor(refresh=False, session=db_session)
    assert calls == ["EURUSD"]
    monitor(refresh=True, session=db_session)         # Refresh rebuilds
    assert calls == ["EURUSD", "EURUSD"]


def test_choch_that_confirms_the_bias_is_not_a_warning():
    """The STOXX50 case from the live sample: 4H down, swing structure 'up' + CHoCH = a BEARISH break.
    It confirms the down bias; it used to be shown as 'an early sign of a turn'."""
    row = _row("aligned_down", **{"1h": {"trend": "down"}, "1d": {"trend": "down"},
                                  "4h": {"trend": "down", "choch": True, "choch_side": "down"}})
    notes = [n for n in notes_for(row) if "change of character" in n["text"]]
    assert len(notes) == 1
    assert notes[0]["tone"] == "info"
    assert "bearish" in notes[0]["text"] and "in line with the down bias" in notes[0]["text"]
    assert "turn" not in notes[0]["text"]


def test_choch_against_the_bias_is_a_warning():
    row = _row("aligned_down", **{"1h": {"trend": "down"}, "1d": {"trend": "down"},
                                  "4h": {"trend": "down", "choch": True, "choch_side": "up"}})
    notes = [n for n in notes_for(row) if "change of character" in n["text"]]
    assert notes[0]["tone"] == "caution" and "bullish" in notes[0]["text"] and "against the down bias" in notes[0]["text"]


def test_an_opposing_zone_lower_down_is_not_hidden_by_an_in_line_zone_higher_up():
    row = _row("aligned_up", **{
        "1h": {"trend": "up"},
        "1d": {"trend": "up", "zones": {"inside": {"kind": "demand", "strength": 60}, "inside_kinds": ["demand"],
                                        "demand": None, "supply": None}},
        "4h": {"trend": "up", "zones": {"inside": None, "inside_kinds": [], "demand": None,
                                        "supply": {"dist_atr": 0.2, "strength": 70}}},
    })
    notes = notes_for(row)
    zone = [n for n in notes if "zone" in n["text"]]
    assert zone[0]["tone"] == "caution" and "4H supply zone is just ahead" in zone[0]["text"]
    assert any("inside a 1D demand zone, in line with the up bias" in n["text"] for n in zone)


def test_price_inside_overlapping_demand_and_supply_warns_about_the_opposing_one():
    row = _row("aligned_down", **{
        "1h": {"trend": "down"}, "1d": {"trend": "down"},
        "4h": {"trend": "down", "zones": {"inside": {"kind": "supply", "strength": 85},
                                          "inside_kinds": ["demand", "supply"], "demand": None, "supply": None}},
    })
    notes = notes_for(row)
    assert any(n["tone"] == "caution" and "inside a 4H demand zone, against the down bias" in n["text"] for n in notes)
    assert not any("in line with the down bias" in n["text"] and "zone" in n["text"] for n in notes)


def test_pullback_note_says_which_way_the_latest_1h_bar_leans():
    resuming = _row("pullback_up", **{"1h": {"trend": "down", "state": "rejection", "pattern_side": "up"},
                                      "4h": {"trend": "up"}, "1d": {"trend": "up"}})
    fading = _row("pullback_up", **{"1h": {"trend": "down", "state": "pullback", "pattern_side": "down"},
                                    "4h": {"trend": "up"}, "1d": {"trend": "up"}})
    assert any("leans back up (rejection)" in n["text"] for n in notes_for(resuming))
    assert any("still leans down (pullback)" in n["text"] for n in notes_for(fading))


def test_quiet_crypto_is_not_called_a_closed_market():
    row = _row("mixed", **{"1h": {"trend": "up"}, "4h": {"trend": "down"}, "1d": {"trend": "up"}})
    row["market_open"] = False
    row["asset_class"] = "crypto"
    texts = [n["text"] for n in notes_for(row)]
    assert any("No fresh prices" in t for t in texts)
    assert not any("Market closed" in t for t in texts)
    row["asset_class"] = "forex"
    assert any("Market closed" in n["text"] for n in notes_for(row))


class _FakeBroker:
    name = "fake-monitor"

    def __init__(self, series):
        self.series = series

    def get_ohlcv(self, symbol, timeframe, limit):
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=self.series[timeframe][-limit:])

    def market_open(self, symbol):
        return True


def _bars(start, step, n, px=100.0):
    return [Candle(ts=start + step * i, open=px + i * 0.01, high=px + i * 0.01 + 0.5,
                   low=px + i * 0.01 - 0.5, close=px + i * 0.01 + 0.1, volume=1000) for i in range(n)]


def test_today_uses_the_right_prior_close_when_the_daily_lags_past_midnight():
    """Just after midnight the cached daily can still end at YESTERDAY while the 1h already has today's
    bars. 'Today' must then be measured from yesterday's close, not from the day before's."""
    from app.data.ohlcv_cache import clear_ohlcv_cache

    clear_ohlcv_cache()
    today = dt.datetime(2026, 3, 11, tzinfo=dt.timezone.utc)
    daily = _bars(today - dt.timedelta(days=300), dt.timedelta(days=1), 300)      # ends 2026-03-10
    hourly = _bars(today - dt.timedelta(hours=297), dt.timedelta(hours=1), 300)   # ends 2026-03-11 02:00
    assert daily[-1].ts.date() < hourly[-1].ts.date()
    row = mm.read_pair(_FakeBroker({"1h": hourly, "4h": hourly, "1d": daily}), "TEST", "forex")
    expected = round((hourly[-1].close / daily[-1].close - 1) * 100, 2)
    assert row["change_pct"] == expected
    todays = [c for c in hourly if c.ts.date() == today.date()]
    rng = max(c.high for c in todays) - min(c.low for c in todays)
    assert row["range_used"] == round(rng / row["tfs"]["1d"]["atr"], 2)
    clear_ohlcv_cache()


def test_a_timeframe_without_enough_history_says_so():
    from app.data.ohlcv_cache import clear_ohlcv_cache

    clear_ohlcv_cache()
    start = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    short_daily = _bars(start, dt.timedelta(days=1), 30)
    hourly = _bars(start, dt.timedelta(hours=1), 300)
    row = mm.read_pair(_FakeBroker({"1h": hourly, "4h": hourly, "1d": short_daily}), "NEWCFD", "index")
    assert row["tfs"]["1d"] is None
    assert any(e.startswith("1d: not enough history") for e in row["errors"])
    assert row["bias"] == "mixed"
    clear_ohlcv_cache()


def test_unchanged_series_is_not_re_read(monkeypatch):
    calls = []
    real = mm.read_timeframe
    monkeypatch.setattr(mm, "read_timeframe", lambda s: calls.append(s.timeframe) or real(s))
    s = _series(_trend())
    mm._read_cached("AAA", "4h", s)
    mm._read_cached("AAA", "4h", s)            # same cached object -> reused
    assert calls == ["1h"]
    mm._read_cached("AAA", "4h", _series(_trend()))   # a new fetch -> read again
    assert len(calls) == 2


def test_refresh_waits_for_a_build_in_flight_instead_of_rebuilding():
    import threading
    import time as _t

    builds = []
    gate = threading.Event()

    def slow_build():
        builds.append(1)
        gate.wait(2)
        return [{"symbol": "X"}]

    t = threading.Thread(target=lambda: mm.cached_table(("k",), slow_build))
    t.start()
    _t.sleep(0.1)                               # first build is now in flight, holding the lock
    asked = _t.time()
    result = {}
    t2 = threading.Thread(target=lambda: result.update(r=mm.cached_table(("k",), slow_build, fresh_after=asked)))
    t2.start()
    gate.set()
    t.join(5)
    t2.join(5)
    assert len(builds) == 1                     # the refresh got the in-flight build
    assert result["r"][0] == [{"symbol": "X"}]
