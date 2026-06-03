"""Tests for the ccxt and OANDA adapters using injected fake clients (no network/keys).

We bypass the real constructors with ``__new__`` and inject a duck-typed client, so these
test the request-building + response-mapping logic deterministically.
"""
from __future__ import annotations

from app.brokers.ccxt_adapter import CcxtBrokerAdapter
from app.brokers.oanda import OandaBrokerAdapter, normalize_instrument
from app.models.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.models.schemas import OrderRequest


# ----------------------------------------------------------------- ccxt ----


class _FakeExchange:
    has = {"fetchPositions": False}

    def fetch_ticker(self, symbol):
        return {"last": 50000.0, "timestamp": 1_700_000_000_000}

    def fetch_ohlcv(self, symbol, timeframe, limit):
        base = 1_700_000_000_000
        return [[base + i * 60000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i] for i in range(limit)]

    def create_order(self, symbol, otype, side, amount, price):
        return {"id": "ord-1", "status": "closed", "filled": amount, "average": 50000.0}

    def fetch_balance(self):
        return {"total": {"USDT": 1000.0, "BTC": 0.1}, "free": {"USDT": 800.0}}


def _ccxt() -> CcxtBrokerAdapter:
    a = CcxtBrokerAdapter.__new__(CcxtBrokerAdapter)
    a._paper = True
    a._exchange = _FakeExchange()
    return a


def test_ccxt_is_paper_and_quote():
    a = _ccxt()
    assert a.is_paper is True
    q = a.get_quote("BTC/USDT")
    assert q.price == 50000.0


def test_ccxt_ohlcv_mapping():
    a = _ccxt()
    series = a.get_ohlcv("BTC/USDT", "1h", limit=20)
    assert len(series.candles) == 20
    assert series.candles[0].open == 100 and series.candles[0].volume == 10


def test_ccxt_submit_order_filled():
    a = _ccxt()
    res = a.submit_order(OrderRequest(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO,
                                      side=OrderSide.BUY, order_type=OrderType.MARKET, qty=0.5))
    assert res.status == OrderStatus.FILLED
    assert res.avg_fill_price == 50000.0 and res.filled_qty == 0.5
    assert res.broker_order_id == "ord-1"


def test_ccxt_account_sums_quote_currencies():
    a = _ccxt()
    acct = a.get_account()
    assert acct.equity == 1000.0 and acct.cash == 800.0  # BTC excluded from the quote sum


def test_ccxt_spot_has_no_positions():
    assert _ccxt().get_open_positions() == []


# ---------------------------------------------------------------- oanda ----


class _FakeOandaClient:
    def __init__(self):
        self.next_response: dict = {}

    def request(self, req):
        req.response = self.next_response
        return self.next_response


def _oanda() -> tuple[OandaBrokerAdapter, _FakeOandaClient]:
    a = OandaBrokerAdapter.__new__(OandaBrokerAdapter)
    a._env = "practice"
    a._account_id = "acc-1"
    client = _FakeOandaClient()
    a._client = client
    return a, client


def test_normalize_instrument():
    assert normalize_instrument("EUR/USD") == "EUR_USD"
    assert normalize_instrument("EURUSD") == "EUR_USD"
    assert normalize_instrument("XAUUSD") == "XAU_USD"
    assert normalize_instrument("EUR_USD") == "EUR_USD"


def test_oanda_is_paper():
    a, _ = _oanda()
    assert a.is_paper is True


def test_oanda_ohlcv_mapping():
    a, client = _oanda()
    client.next_response = {
        "candles": [
            {"time": "2026-06-02T00:00:00.000000Z", "complete": True,
             "mid": {"o": "1.10", "h": "1.12", "l": "1.09", "c": "1.115"}, "volume": 100},
            {"time": "2026-06-02T01:00:00.000000Z", "complete": False,  # skipped
             "mid": {"o": "1.115", "h": "1.13", "l": "1.11", "c": "1.12"}, "volume": 50},
        ]
    }
    series = a.get_ohlcv("EUR/USD", "1h", limit=10)
    assert len(series.candles) == 1  # incomplete candle dropped
    assert series.candles[0].close == 1.115


def test_oanda_quote_mid():
    a, client = _oanda()
    client.next_response = {"prices": [{"bids": [{"price": "1.10"}], "asks": [{"price": "1.12"}]}]}
    q = a.get_quote("EURUSD")
    assert q.price == 1.11 and q.symbol == "EUR_USD"


def test_oanda_submit_order_filled():
    a, client = _oanda()
    client.next_response = {"orderFillTransaction": {"id": "f1", "orderID": "o1",
                                                     "units": "1000", "price": "1.105"}}
    res = a.submit_order(OrderRequest(symbol="EUR_USD", asset_class=AssetClass.FOREX,
                                      side=OrderSide.BUY, order_type=OrderType.MARKET, qty=1000))
    assert res.status == OrderStatus.FILLED
    assert res.avg_fill_price == 1.105 and res.filled_qty == 1000


def test_oanda_submit_order_rejected():
    a, client = _oanda()
    client.next_response = {"orderCreateTransaction": {"id": "c1"},
                            "orderCancelTransaction": {"id": "x1", "reason": "INSUFFICIENT_MARGIN"}}
    res = a.submit_order(OrderRequest(symbol="EUR_USD", asset_class=AssetClass.FOREX,
                                      side=OrderSide.SELL, order_type=OrderType.MARKET, qty=1000))
    assert res.status == OrderStatus.REJECTED
    assert res.error == "INSUFFICIENT_MARGIN"
