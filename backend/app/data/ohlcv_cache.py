"""Short-TTL cache for OHLCV fetches, shared across all agents.

Why this exists: the scanner (~20s), the position advisor (~30s), Hybrid (~60s) and the on-demand
Opportunities / Run-analysis sweeps all pull the SAME watchlist symbols' candles from the broker
within seconds of each other. Without a cache that's a storm of duplicate broker round-trips — and
on MT5/Exness each one is a synchronous call over a single terminal connection (the scan
bottleneck; that connection also isn't meant for concurrent use, so fewer calls is strictly
better). Caching the last fetch per (broker, symbol, timeframe, limit) for a few seconds collapses
that storm into one fetch.

Staleness is a non-issue at this TTL: 15m/1h/1d candles don't change meaningfully within a dozen
seconds, and the actual order fill still reads a LIVE quote at execution time (the Monitor checks
stops/targets every ~10s off live quotes, not these cached candles). The chart endpoint and the
backtest path deliberately do NOT use this cache — they want fresh / bespoke series.
"""
from __future__ import annotations

import threading
import time

from app.brokers.base import BrokerAdapter
from app.models.schemas import OHLCVSeries

# A dozen seconds: long enough to dedupe back-to-back agent passes on the same symbols, short
# enough that the candles are effectively current. Tune here if agent intervals change.
_TTL_SECONDS = 12.0

_lock = threading.Lock()
_cache: dict[tuple[str, str, str, int], tuple[float, OHLCVSeries]] = {}


def get_ohlcv_cached(broker: BrokerAdapter, symbol: str, timeframe: str = "1h",
                     limit: int = 200, ttl: float = _TTL_SECONDS) -> OHLCVSeries:
    """Return cached candles if a fetch for this exact key happened within ``ttl`` seconds, else
    fetch from the broker and cache it. Thread-safe (agents run on the scheduler's thread pool).

    The broker call is made OUTSIDE the lock so a slow fetch never blocks other symbols/threads;
    a rare double-miss just fetches twice (idempotent) — strictly fewer calls than no cache.
    """
    key = (getattr(broker, "name", "?"), symbol, timeframe, limit)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    series = broker.get_ohlcv(symbol, timeframe, limit)
    # Data-feed integrity (Task 10): sanity-check the fresh fetch (bad ticks / gaps / stale prices)
    # before it can poison ATR-based sizing or structure detection. With DATA_INTEGRITY_REJECT on
    # (default) the clearest corruption is REPAIRED (spikes clamped, invalid bars flattened) and soft
    # issues logged; off = log-only. Never breaks a fetch.
    try:
        from app.core.config import get_settings
        from app.data.integrity import log_integrity, repair_and_log
        if get_settings().data_integrity_reject:
            series = repair_and_log(symbol, timeframe, series)
        else:
            log_integrity(symbol, timeframe, series)
    except Exception:  # noqa: BLE001 - integrity checking must never break a fetch
        pass
    with _lock:
        _cache[key] = (time.monotonic(), series)
    return series


def clear_ohlcv_cache() -> None:
    """Drop all cached series (used by tests; safe to call anytime)."""
    with _lock:
        _cache.clear()
