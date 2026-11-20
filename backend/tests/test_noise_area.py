"""The noise area in the learner (2026-11-19, the user: "put the noise area in the learner and let it watch it on the
trades"; Zarattini, Aziz & Barbon 2024): each trade's reading - the day's move from its UTC open our way, over the
move the 14 earlier days made from their open by the same minute - added to its fingerprint from MT5's 1m history,
once, and the trades split by it like any other part. Watched only: no Apply button."""
from __future__ import annotations

from array import array
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.agents.learning as L
import app.agents.signal_review as sr
from app.models.db import StrategyTradeFact
from tests.test_learning import _facts, _strategy

U = timezone.utc
TRADE_DAY = datetime(2026, 11, 18, tzinfo=U)            # a Wednesday
NOW = TRADE_DAY + timedelta(days=1, hours=3)


@pytest.fixture(autouse=True)
def _fresh_cache():
    L._DAY_CACHE.clear()
    yield
    L._DAY_CACHE.clear()


def _day(open_, mins, closes):
    return (open_, array("H", mins), array("d", closes))


def _history(monkeypatch, skip_weekdays=(), first_minute=0, minutes=720, step=0.01, reads=None, broken=False):
    """MT5's 1m history, faked: every day opens at 100 and climbs ``step`` a minute from ``first_minute`` on, for
    ``minutes`` minutes; none on ``skip_weekdays``. The trade's own day climbs twice as fast."""
    def bars(_broker, symbol, tf, start, end):
        assert tf == "1m"
        if broken:
            raise RuntimeError("no MT5")
        if reads is not None:
            reads.append(start.date())
        if start.weekday() in skip_weekdays:
            return []
        k = 2 if start.date() == TRADE_DAY.date() else 1
        out = []
        for m in range(first_minute, first_minute + minutes):
            ts = start + timedelta(minutes=m)
            if start <= ts < end:
                op = 100 + k * step * (m - first_minute)
                out.append(SimpleNamespace(ts=ts, open=op, high=op + k * step, low=op, close=op + k * step))
        return out
    monkeypatch.setattr(sr, "_bars", bars)


# --------------------------------------------------------------------------- the reading

def test_the_reading_is_the_day_s_move_over_the_usual_move_at_that_minute():
    earlier = [_day(100.0, [0, 1, 2], [100.1, 100.2, 100.3]), _day(200.0, [0, 1, 2], [199.0, 198.0, 197.0])]
    # minute 1: usual |move| = (0.2% + 1%) / 2 = 0.6%; the day is +1.2% from its open of 100 -> 2x the usual move
    assert L.noise_of(1, 101.2, 1, 100.0, earlier) == pytest.approx(2.0)
    assert L.noise_of(-1, 101.2, 1, 100.0, earlier) == pytest.approx(-2.0)          # a sell reads it mirrored
    assert L.noise_of(-1, 99.4, 1, 100.0, earlier) == pytest.approx(1.0)
    # a minute past an earlier day's last candle: that candle's close counts (the move it had made by then)
    assert L.noise_of(1, 100.6, 90, 100.0, earlier) == pytest.approx(0.6 / ((0.3 + 1.5) / 2), abs=1e-3)   # 3 places


def test_an_earlier_day_with_no_candle_yet_by_that_minute_is_left_out():
    late = _day(100.0, [600, 601], [101.0, 102.0])                                  # opened at 10:00
    early = _day(100.0, [0, 1], [100.5, 100.5])
    assert L.noise_of(1, 100.5, 30, 100.0, [late, early]) == pytest.approx(1.0)     # only the early one counts
    assert L.noise_of(1, 100.5, 30, 100.0, [late]) is None
    assert L.noise_of(1, 100.5, 30, None, [early]) is None
    assert L.noise_of(1, 100.5, 0, 100.0, [_day(100.0, [0], [100.0])]) is None       # no usual move at all


def test_it_is_read_from_the_14_earlier_days_with_candles(monkeypatch):
    reads = []
    _history(monkeypatch, reads=reads)
    at = TRADE_DAY + timedelta(hours=5, seconds=20)                                  # minute 300 of its day
    entry = 100 + 2 * 0.01 * 300                                                      # its day: +6% so far
    # each earlier day had climbed 0.01 x 301 by the close of the candle that opened at minute 300: 3.01%
    want = 0.06 / 0.0301
    assert L.history_noise(None, "US30m", 1, entry, at, NOW) == pytest.approx(want, abs=1e-3)
    assert L.history_noise(None, "US30m", -1, entry, at, NOW) == pytest.approx(-want, abs=1e-3)
    days = sorted(set(reads))
    assert days[0] == (TRADE_DAY - timedelta(days=14)).date() and days[-1] == TRADE_DAY.date()
    assert len(reads) == 15                                                           # the past days kept: read once


def test_days_without_candles_are_skipped_further_back(monkeypatch):
    reads = []
    _history(monkeypatch, skip_weekdays=(5, 6), reads=reads)                          # no weekends
    at = TRADE_DAY + timedelta(hours=5, seconds=20)
    assert L.history_noise(None, "US30m", 1, 106.0, at, NOW) == pytest.approx(0.06 / 0.0301, abs=1e-3)
    assert min(reads) == (TRADE_DAY - timedelta(days=20)).date()                      # 14 weekdays: 20 days back


def test_too_little_history_or_a_trade_before_its_day_s_first_candle_reads_nothing(monkeypatch):
    _history(monkeypatch, skip_weekdays=(0, 1, 3, 4, 5, 6))                           # only Wednesdays: 4 in 30 days
    assert L.history_noise(None, "US30m", 1, 106.0, TRADE_DAY + timedelta(hours=5), NOW) is None
    L._DAY_CACHE.clear()
    _history(monkeypatch, first_minute=60)                                            # the market opens at 01:00
    assert L.history_noise(None, "US30m", 1, 100.0, TRADE_DAY + timedelta(minutes=30), NOW) is None
    assert L.history_noise(None, "US30m", 1, 100.0, TRADE_DAY + timedelta(minutes=90), NOW) is not None


def test_today_is_never_kept_in_memory(monkeypatch):
    _history(monkeypatch)
    at = TRADE_DAY + timedelta(hours=5)
    L.history_noise(None, "US30m", 1, 106.0, at, TRADE_DAY + timedelta(hours=6))      # the trade's day is today
    assert ("US30m", TRADE_DAY.date().isoformat()) not in L._DAY_CACHE
    assert ("US30m", (TRADE_DAY - timedelta(days=1)).date().isoformat()) in L._DAY_CACHE


def test_the_memory_stays_bounded(monkeypatch):
    _history(monkeypatch)
    monkeypatch.setattr(L, "_DAY_CACHE_MAX", 20)
    for k in range(3):
        L.history_noise(None, f"S{k}", 1, 106.0, TRADE_DAY + timedelta(hours=5), NOW)
    assert len(L._DAY_CACHE) <= 20


class _NpMt5:
    """MT5's copy_rates_range as it really answers (2026-11-19, checked on the terminal): a numpy array of the 1m
    rates - empty on a day without candles, the next day's 00:00 candle included - and None when the call fails."""

    def __init__(self, fail_symbols=()):
        self.fail = set(fail_symbols)
        self.calls = 0

    def copy_rates_range(self, sym, tf, start, end):
        import numpy as np

        self.calls += 1
        if sym in self.fail:
            return None
        dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                       ("tick_volume", "<u8")])
        if start.weekday() == 5:
            return np.zeros(0, dtype=dt)
        k = 2 if start.date() == TRADE_DAY.date() else 1
        rows = [(int(start.timestamp()) + 60 * m, 100 + k * 0.01 * m, 0, 0, 100 + k * 0.01 * (m + 1), 1)
                for m in range(0, 721)]
        rows.append((int(end.timestamp()), 999.0, 0, 0, 999.0, 1))            # the next day's first candle
        return np.array(rows, dtype=dt)

    def last_error(self):
        return (-1, "Terminal: Call failed")


class _NpBroker:
    def __init__(self, mt5):
        self._mt5 = mt5

    def _resolve_symbol(self, s):
        return s

    def _tf(self, tf):
        return tf


def test_on_mt5_the_rates_are_read_as_they_come():
    b = _NpBroker(_NpMt5())
    at = TRADE_DAY + timedelta(hours=5, seconds=20)
    # 14 earlier days with candles, the Saturdays (11-14, 11-07) skipped: 16 earlier days read, + its own
    assert L.history_noise(b, "US30m", 1, 106.0, at, NOW) == pytest.approx(0.06 / 0.0301, abs=1e-3)
    o, mins, closes = L._DAY_CACHE[("US30m", (TRADE_DAY - timedelta(days=1)).date().isoformat())]
    assert o == 100.0 and mins[0] == 0 and mins[-1] == 720 and 999.0 not in closes   # the next day's candle dropped
    assert L._DAY_CACHE[("US30m", "2026-11-14")] is None                              # a Saturday: no candles
    assert b._mt5.calls == 17
    L.history_noise(b, "US30m", -1, 106.0, at, NOW)
    assert b._mt5.calls == 17                                                          # all kept in memory


def test_a_failed_mt5_read_is_not_kept_as_a_day_without_candles():
    b = _NpBroker(_NpMt5(fail_symbols={"US30m"}))
    with pytest.raises(RuntimeError):
        L.history_noise(b, "US30m", 1, 106.0, TRADE_DAY + timedelta(hours=5), NOW)
    assert not any(k[0] == "US30m" for k in L._DAY_CACHE)


# --------------------------------------------------------------------------- once per trade, from the history

def _fact(session, cfg, key, *, entry=106.0, fp=None, opened=TRADE_DAY + timedelta(hours=5, seconds=20)):
    row = StrategyTradeFact(key=key, strategy_id=cfg.id, symbol="BTCUSDm", side="long", opened_at=opened,
                            closed_at=opened + timedelta(minutes=20), pnl=5.0, r=0.2, entry=entry, risk_dist=1.0,
                            fp=fp, fp_from="entry" if fp else None)
    session.add(row)
    return row


def test_each_fingerprint_gets_its_reading_once(db_session, monkeypatch):
    _history(monkeypatch)
    cfg = _strategy(db_session)
    _fact(db_session, cfg, "a", fp={"v": 2, "streak": 1})
    _fact(db_session, cfg, "b", fp={"v": 2, "streak": 3, "noise": 0.4})             # has it already
    _fact(db_session, cfg, "c", fp={"v": 2}, entry=None)                             # no entry to read it at
    _fact(db_session, cfg, "d", fp=None)                                              # no fingerprint yet
    db_session.commit()
    assert L.fill_noise(db_session, cfg, lambda ac: None, NOW) == 1
    rows = {r.key: r for r in db_session.query(StrategyTradeFact).all()}
    assert rows["a"].fp["noise"] == pytest.approx(0.06 / 0.0301, abs=1e-3)
    assert rows["a"].fp["streak"] == 1 and rows["a"].fp["v"] == 2                     # the rest kept as it was
    assert rows["b"].fp == {"v": 2, "streak": 3, "noise": 0.4}
    assert rows["c"].fp == {"v": 2} and rows["d"].fp is None
    assert L.fill_noise(db_session, cfg, lambda ac: None, NOW) == 0                   # once


def test_a_failed_read_is_tried_three_times_then_left_out(db_session, monkeypatch):
    _history(monkeypatch, broken=True)
    cfg = _strategy(db_session)
    _fact(db_session, cfg, "a", fp={"v": 2, "streak": 1})
    db_session.commit()
    for _ in range(L.NOISE_TRIES):
        assert L.fill_noise(db_session, cfg, lambda ac: None, NOW) == 1
    assert L.fill_noise(db_session, cfg, lambda ac: None, NOW) == 0
    row = db_session.query(StrategyTradeFact).one()
    assert row.fp == {"v": 2, "streak": 1, "noise_tries": L.NOISE_TRIES}


def test_a_read_that_works_on_a_later_try_drops_the_count(db_session, monkeypatch):
    cfg = _strategy(db_session)
    _fact(db_session, cfg, "a", fp={"v": 2, "streak": 1})
    db_session.commit()
    _history(monkeypatch, broken=True)
    assert L.fill_noise(db_session, cfg, lambda ac: None, NOW) == 1
    _history(monkeypatch)
    assert L.fill_noise(db_session, cfg, lambda ac: None, NOW) == 1
    fp = db_session.query(StrategyTradeFact).one().fp
    assert "noise_tries" not in fp and fp["noise"] > 1


def test_the_budget_caps_the_reads_newest_first(db_session, monkeypatch):
    _history(monkeypatch)
    cfg = _strategy(db_session)
    for i in range(5):
        _fact(db_session, cfg, f"k{i}", fp={"v": 2}, opened=TRADE_DAY + timedelta(hours=5, minutes=i))
    db_session.commit()
    assert L.fill_noise(db_session, cfg, lambda ac: None, NOW, budget=2) == 2
    have = {r.key for r in db_session.query(StrategyTradeFact).all() if "noise" in r.fp}
    assert have == {"k4", "k3"}


def test_the_tick_reads_it_on_its_own_budget(db_session, monkeypatch):
    from app.api.strategy_routes import _config_of

    _history(monkeypatch)
    cfg = _strategy(db_session)
    _fact(db_session, cfg, "a", fp={"v": 2, "streak": 1})
    db_session.commit()
    out = L.learning_tick(db_session, now=NOW, broker_for=lambda ac: None, config_of=_config_of, book=[])
    assert out["noise"] == 1
    assert "noise" in db_session.query(StrategyTradeFact).one().fp


# --------------------------------------------------------------------------- the review watches it

def test_the_review_splits_trades_by_it():
    part = L.FEATURES["noise"][1]
    assert part({"fp": {"noise": -0.3}})[0] == "low" and part({"fp": {"noise": 0.49}})[0] == "low"
    assert part({"fp": {"noise": 0.5}})[0] == "mid" and part({"fp": {"noise": 0.99}})[0] == "mid"
    assert part({"fp": {"noise": L.NOISE_OUT}})[0] == "high" and part({"fp": {"noise": 3.2}})[0] == "high"
    assert part({"fp": {"streak": 1}}) is None and part({"fp": None}) is None


def test_trades_deep_in_the_noise_show_up_as_losing():
    facts = _facts(key=lambda bad: {"noise": 0.2 if bad else 1.6})
    parts = {p["key"]: p for p in L.find_parts(facts)}
    low = parts["noise:low"]
    assert low["kind"] == "weak" and low["feature_name"].startswith("the noise area")
    assert parts["noise:high"]["kind"] == "sweet"


def test_it_is_watched_only_no_button():
    for bucket in ("low", "mid", "high"):
        for kind in ("weak", "sweet"):
            assert L.action_for({"feature": "noise", "bucket": bucket, "kind": kind}, {"entry_long": []}) is None
