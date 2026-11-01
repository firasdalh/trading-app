"""The chart's order-flow pane: buy vs sell ticks per bar, by the tick rule on the bid (read-only)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.brokers.mt5_adapter as mt5_adapter
from app.brokers.mt5_adapter import Mt5BrokerAdapter, _flow_from_ticks

B = 1_800_000_000                # a whole minute


@pytest.fixture(autouse=True)
def _empty_cache():
    mt5_adapter._FLOW_CACHE.clear()
    yield
    mt5_adapter._FLOW_CACHE.clear()


def test_a_tick_that_lifts_the_bid_is_a_buy_and_one_that_drops_it_a_sell():
    # Bar 100: the first tick has nothing before it (flat), +0.2, the bid-less tick is skipped, -0.3,
    # unchanged (flat). Bar 105 opens against the last bid of bar 100.
    rows, last = _flow_from_ticks([100, 101, 102, 103, 104, 106, 107], [1.0, 1.2, 0.0, 0.9, 0.9, 1.0, 1.05], 5)
    assert sorted(rows) == [100, 105]
    assert rows[100][:3] == [1, 1, 2]
    assert rows[100][3] == pytest.approx(0.2) and rows[100][4] == pytest.approx(0.3)
    assert rows[105][:3] == [2, 0, 0] and rows[105][3] == pytest.approx(0.15) and rows[105][4] == 0
    assert last == 1.05


def test_prev_carries_the_bid_into_the_next_read_and_keep_stops_at_final_before():
    rows, keep = _flow_from_ticks([105, 106], [1.1, 1.0], 5, prev=1.2, final_before=105)
    assert rows[105][:3] == [0, 2, 0]          # 1.2 -> 1.1 -> 1.0
    assert keep == 1.2                           # no tick before 105 in this read: the one it was given
    _, keep = _flow_from_ticks([101, 106], [1.1, 1.0], 5, prev=1.2, final_before=105)
    assert keep == 1.1


class FlowFake:
    TIMEFRAME_M1 = 1; TIMEFRAME_M5 = 5; TIMEFRAME_H1 = 16385
    COPY_TICKS_ALL = 1
    _SEC = {1: 60, 5: 300, 16385: 3600}

    def __init__(self, ticks):
        self.ticks = list(ticks)                 # (time, bid)
        self.calls = []

    def symbol_info(self, sym):
        return SimpleNamespace(digits=2)

    def symbol_select(self, sym, on=True):
        return True

    def copy_rates_from_pos(self, sym, tf, start, count):
        sec = self._SEC[tf]
        starts = sorted({t - t % sec for t, _ in self.ticks})
        return [{"time": s, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "tick_volume": 1}
                for s in starts[-count:]]

    def copy_ticks_range(self, sym, date_from, date_to, flags):
        self.calls.append((date_from, date_to))
        return [{"time": t, "bid": b} for t, b in self.ticks if date_from <= t <= date_to]


def _adapter(fake) -> Mt5BrokerAdapter:
    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._mt5 = fake
    a._paper = True
    return a


TICKS = [(B + 1, 10.0), (B + 5, 10.5), (B + 30, 10.2), (B + 61, 10.4), (B + 62, 10.4), (B + 90, 10.1),
         (B + 125, 10.3)]


def test_one_minute_bars_count_each_sides_ticks_and_move():
    flow = _adapter(FlowFake(TICKS)).order_flow("BTCUSDm", "1m", 3)
    assert flow["bars"] == [[B, 1, 1, 1, 0.5, 0.3], [B + 60, 1, 1, 1, 0.2, 0.3], [B + 120, 1, 0, 0, 0.2, 0.0]]
    assert flow["seconds"] == 60 and flow["covered_from"] == B and flow["truncated"] is False


def test_a_refresh_reads_only_the_ticks_since_the_last_closed_bar_and_counts_the_same():
    fake = FlowFake(TICKS)
    a = _adapter(fake)
    a.order_flow("BTCUSDm", "1m", 3)
    fake.ticks += [(B + 150, 10.6), (B + 185, 10.0)]
    warm = a.order_flow("BTCUSDm", "1m", 3)
    # The bars up to B+120 had closed (a tick at B+125 was in), so the refresh starts there, and the
    # bar at B+120 opens against the 10.1 its predecessor closed at.
    assert fake.calls[-1][0] == B + 120
    assert warm["bars"] == [[B + 60, 1, 1, 1, 0.2, 0.3], [B + 120, 2, 0, 0, 0.5, 0.0], [B + 180, 0, 1, 0, 0.0, 0.6]]
    mt5_adapter._FLOW_CACHE.clear()
    cold = _adapter(FlowFake(fake.ticks)).order_flow("BTCUSDm", "1m", 4)
    assert cold["bars"][1:] == warm["bars"]


def test_a_request_for_older_bars_than_the_cache_holds_reads_them_all_again():
    fake = FlowFake(TICKS)
    a = _adapter(fake)
    a.order_flow("BTCUSDm", "1m", 1)
    assert fake.calls[-1][0] == B + 120
    full = a.order_flow("BTCUSDm", "1m", 3)
    assert fake.calls[-1][0] == B
    assert [r[0] for r in full["bars"]] == [B, B + 60, B + 120]


def test_flow_reaches_back_one_day_from_the_newest_bar_at_most():
    hours = [(B + 3600 * h + 7, 10.0 + (h % 3) / 10) for h in range(30)]
    fake = FlowFake(hours)
    flow = _adapter(fake).order_flow("BTCUSDm", "1h", 400)
    newest = (B + 3600 * 29 + 7) // 3600 * 3600
    assert flow["truncated"] is True
    assert flow["covered_from"] == newest - 86400
    assert fake.calls[-1][0] == newest - 86400
    assert len(flow["bars"]) == 25


def test_seconds_bars_are_the_seconds_candles():
    ticks = [(B + 1, 1.1), (B + 2, 1.3), (B + 7, 1.2), (B + 61, 1.25), (B + 62, 1.22), (B + 66, 1.24)]
    a = _adapter(FlowFake(ticks))
    candles = a.get_ohlcv("EURUSD", "5s", limit=3).candles
    flow = a.order_flow("EURUSD", "5s", 3)
    assert [r[0] for r in flow["bars"]] == [int(c.ts.timestamp()) for c in candles]
    assert flow["bars"][-1][:4] == [B + 65, 1, 0, 0]


def test_no_bars_at_the_terminal_is_an_empty_flow():
    flow = _adapter(FlowFake([])).order_flow("BTCUSDm", "1m", 10)
    assert flow["bars"] == [] and flow["covered_from"] is None


def test_charts_of_a_minute_or_more_sum_shared_minute_buckets():
    # 6 minutes of ticks, B..B+359; the 5m bars are B (5 whole minutes) and B+300 (forming).
    ticks = [(B + 60 * m + s, 10.0 + ((m * 7 + s) % 5) / 10) for m in range(6) for s in (3, 17, 31, 44)]
    fake = FlowFake(ticks)
    a = _adapter(fake)
    ones = a.order_flow("BTCUSDm", "1m", 6)["bars"]
    fives = a.order_flow("BTCUSDm", "5m", 2)["bars"]
    assert list(mt5_adapter._FLOW_CACHE) == [("BTCUSDm", 60)]       # one cache for both charts
    assert [r[0] for r in fives] == [B, B + 300]
    first = [sum(r[i] for r in ones[:5]) for i in range(1, 6)]
    assert fives[0][1:4] == first[:3]
    assert fives[0][4] == pytest.approx(first[3]) and fives[0][5] == pytest.approx(first[4])
    # A refresh of the 5m chart reads from the last closed MINUTE, not from the 5m bar's start.
    fake.ticks.append((B + 352, 11.0))
    a.order_flow("BTCUSDm", "5m", 2)
    assert fake.calls[-1][0] == B + 300


def test_the_cache_keeps_the_most_recently_used_charts():
    a = _adapter(FlowFake(TICKS))
    for tf in ("1m", "5s"):
        a.order_flow("BTCUSDm", tf, 3)
    for i in range(mt5_adapter._FLOW_CACHE_MAX):
        mt5_adapter._FLOW_CACHE[(f"X{i}", 60)] = {"from": 0, "final": 0, "prev": None, "rows": {}}
    a.order_flow("BTCUSDm", "1m", 3)
    assert len(mt5_adapter._FLOW_CACHE) == mt5_adapter._FLOW_CACHE_MAX
    assert ("BTCUSDm", 60) in mt5_adapter._FLOW_CACHE and ("BTCUSDm", 5) not in mt5_adapter._FLOW_CACHE


# ---- the route ----

class _Broker:
    name = "stub"

    def __init__(self, result):
        self._result = result

    def order_flow(self, symbol, timeframe, bars):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _get(monkeypatch, result, **params):
    from fastapi.testclient import TestClient

    import app.api.market_routes as routes
    from app.main import app

    monkeypatch.setattr(routes, "_broker_for", lambda asset_class, session: _Broker(result))
    q = {"symbol": "BTCUSDm", "asset_class": "crypto", "timeframe": "1m", **params}
    return TestClient(app).get("/api/market/order-flow", params=q)


def test_the_route_returns_the_brokers_flow(db_session, monkeypatch):
    r = _get(monkeypatch, {"symbol": "BTCUSDm", "bars": [[B, 1, 2, 0, 0.5, 1.0]]})
    assert r.status_code == 200 and r.json()["bars"] == [[B, 1, 2, 0, 0.5, 1.0]]


def test_a_broker_without_ticks_is_a_503(db_session, monkeypatch):
    r = _get(monkeypatch, None)
    assert r.status_code == 503 and "tick" in r.json()["detail"]


def test_an_unknown_timeframe_is_a_422(db_session, monkeypatch):
    assert _get(monkeypatch, ValueError("unsupported timeframe: 2m"), timeframe="2m").status_code == 422


def test_a_broker_error_is_a_503(db_session, monkeypatch):
    from app.brokers.base import BrokerError

    r = _get(monkeypatch, BrokerError("symbol NOPE not found on this MT5 account"))
    assert r.status_code == 503 and "NOPE" in r.json()["detail"]

