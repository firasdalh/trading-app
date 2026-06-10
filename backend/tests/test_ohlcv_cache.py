"""The shared OHLCV cache collapses the many duplicate broker fetches a multi-agent scan makes
into one per (broker, symbol, timeframe) for a short TTL."""
from __future__ import annotations

import pytest

from app.data import ohlcv_cache
from app.models.schemas import OHLCVSeries


@pytest.fixture(autouse=True)
def _clear():
    ohlcv_cache.clear_ohlcv_cache()
    yield
    ohlcv_cache.clear_ohlcv_cache()


class _CountingBroker:
    name = "countbroker"

    def __init__(self):
        self.calls = 0

    def get_ohlcv(self, symbol, timeframe="1h", limit=200):
        self.calls += 1
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=[])


def test_cache_serves_within_ttl():
    b = _CountingBroker()
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h")
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h")
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h")
    assert b.calls == 1  # one fetch, then served from cache


def test_cache_key_separates_symbol_and_timeframe():
    b = _CountingBroker()
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h")
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1d")   # different tf -> distinct key
    ohlcv_cache.get_ohlcv_cached(b, "BTCUSDm", "1h")   # different symbol -> distinct key
    assert b.calls == 3


def test_cache_expires_after_ttl():
    b = _CountingBroker()
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h", ttl=0.0)  # already expired -> refetch
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h", ttl=0.0)
    assert b.calls == 2


def test_clear_forces_refetch():
    b = _CountingBroker()
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h")
    ohlcv_cache.clear_ohlcv_cache()
    ohlcv_cache.get_ohlcv_cached(b, "XAUUSDm", "1h")
    assert b.calls == 2
