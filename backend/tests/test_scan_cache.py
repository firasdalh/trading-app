"""ScanCache memoizes the broker open book + account for one watchlist/Hybrid scan, so ranking
N symbols makes O(1) broker round-trips instead of O(N)."""
from __future__ import annotations

import app.risk.service as service
from app.models.schemas import AccountState, PositionView


class _CountingBroker:
    name = "countbroker"

    def __init__(self):
        self.account_calls = 0

    def get_account(self):
        self.account_calls += 1
        return AccountState(equity=1000.0, cash=1000.0, open_positions=0)


def test_build_account_state_caches_broker_account(db_session):
    broker = _CountingBroker()
    cache = service.ScanCache()
    service.build_account_state(db_session, broker, cache=cache)
    service.build_account_state(db_session, broker, cache=cache)
    assert broker.account_calls == 1  # second assessment served from the cache

    # Without a cache, every assessment hits the broker.
    service.build_account_state(db_session, broker)
    assert broker.account_calls == 2


def test_scan_open_book_memoized_and_primeable(db_session, monkeypatch):
    calls = {"n": 0}

    def fake_live(session):
        calls["n"] += 1
        return [PositionView(id=1, symbol="XAUUSDm", asset_class="metal", direction="short",
                             qty=0.01, entry_price=1.0, stop_loss=None, take_profit=None,
                             status="open", last_price=1.0, unrealized_pnl=0.0)]

    monkeypatch.setattr(service, "live_broker_positions", fake_live)

    cache = service.ScanCache()
    first = service._scan_open_book(db_session, cache)
    second = service._scan_open_book(db_session, cache)
    assert calls["n"] == 1  # fetched once, then memoized
    assert first == second == [("XAUUSDm", "short")]

    # A pre-primed cache never fetches at all.
    primed = service.ScanCache(open_book=[("BTCUSDm", "short")])
    assert service._scan_open_book(db_session, primed) == [("BTCUSDm", "short")]
    assert calls["n"] == 1

    # No cache -> fetched each call.
    service._scan_open_book(db_session, None)
    assert calls["n"] == 2
