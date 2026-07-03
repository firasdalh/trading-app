"""Task 10 — data-feed integrity checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.data.integrity import check_candles, sanitize_candles
from app.models.schemas import Candle

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)


def _c(i, o, h, l, cl, step_min=60):
    return Candle(ts=T0 + timedelta(minutes=step_min * i), open=o, high=h, low=l, close=cl, volume=100.0)


def _clean(n=40, base=100.0):
    # A gently trending, internally-consistent hourly series.
    out = []
    px = base
    for i in range(n):
        o = px
        cl = px + 0.1
        out.append(_c(i, o, max(o, cl) + 0.2, min(o, cl) - 0.2, cl))
        px = cl
    return out


def _kinds(candles):
    return {iss.kind for iss in check_candles(candles)}


def test_clean_series_has_no_issues():
    assert check_candles(_clean()) == []


def test_detects_ohlc_inconsistent():
    c = _clean()
    c[20] = _c(20, 100.0, 99.0, 101.0, 100.5)  # high < low
    assert "ohlc_invalid" in _kinds(c)


def test_detects_nonpositive():
    c = _clean()
    c[10] = _c(10, 0.0, 0.0, 0.0, 0.0)
    assert "nonpositive" in _kinds(c)


def test_detects_anomalous_range_spike():
    c = _clean()
    bad = c[30]
    # A bar ~50x the normal ~0.4 range -> way over 6x ATR.
    c[30] = _c(30, bad.open, bad.open + 20.0, bad.open - 20.0, bad.close)
    assert "anomalous_range" in _kinds(c)


def test_detects_stale_feed():
    c = _clean()
    for i in range(15, 20):  # 5 identical closes
        c[i] = _c(i, 105.0, 105.2, 104.8, 105.0)
    assert "stale" in _kinds(c)


def test_detects_time_gap():
    c = _clean(20)
    # Shove everything after index 10 forward by 10 hours -> a big intraday gap (not a weekend).
    for i in range(10, len(c)):
        c[i] = Candle(ts=c[i].ts + timedelta(hours=10), open=c[i].open, high=c[i].high,
                      low=c[i].low, close=c[i].close, volume=100.0)
    assert "gap" in _kinds(c)


def test_weekend_gap_not_flagged():
    # A ~2.5-day gap (Fri->Mon) must NOT be flagged as missing bars.
    c = _clean(20)
    for i in range(10, len(c)):
        c[i] = Candle(ts=c[i].ts + timedelta(hours=60), open=c[i].open, high=c[i].high,
                      low=c[i].low, close=c[i].close, volume=100.0)
    assert "gap" not in _kinds(c)


def test_empty_series_safe():
    assert check_candles([]) == []


# --- repair (hard-reject) ------------------------------------------------------

def test_sanitize_leaves_clean_series_unchanged():
    c = _clean()
    out, repaired = sanitize_candles(c)
    assert repaired == [] and [x.close for x in out] == [x.close for x in c]


def test_sanitize_clamps_spike_and_clears_the_issue():
    c = _clean()
    bad = c[30]
    c[30] = _c(30, bad.open, bad.open + 20.0, bad.open - 20.0, bad.close)  # ~50x normal range
    out, repaired = sanitize_candles(c)
    assert any(r.kind == "anomalous_range" for r in repaired)
    # After repair the spike is gone and the body (open/close) is preserved.
    assert (out[30].high - out[30].low) < (c[30].high - c[30].low)
    assert out[30].open == bad.open and out[30].close == bad.close
    assert "anomalous_range" not in {i.kind for i in check_candles(out)}


def test_sanitize_flattens_invalid_bar_to_prior_close():
    c = _clean()
    c[15] = _c(15, 100.0, 99.0, 101.0, 100.5)  # high < low -> invalid
    out, repaired = sanitize_candles(c)
    assert any(r.kind in ("ohlc_invalid", "nonpositive") for r in repaired)
    prev_close = c[14].close
    assert out[15].open == out[15].high == out[15].low == out[15].close == prev_close
    assert "ohlc_invalid" not in {i.kind for i in check_candles(out)}


def test_sanitize_does_not_touch_soft_issues():
    # A stale run is a soft issue -> left intact (not repaired).
    c = _clean()
    for i in range(15, 20):
        c[i] = _c(i, 105.0, 105.2, 104.8, 105.0)
    out, repaired = sanitize_candles(c)
    assert all(r.kind not in ("stale", "gap") for r in repaired)
    assert "stale" in {i.kind for i in check_candles(out)}
