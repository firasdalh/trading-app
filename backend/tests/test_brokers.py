"""Tests for the broker layer (the dangerous part — covered thoroughly).

The SimPaperBroker is exercised end-to-end; the Alpaca adapter's order-mapping is tested
with a mocked client so no network/keys are needed.
"""
from __future__ import annotations

import pytest

from app.brokers.sim import SimPaperBroker
from app.data.market import SyntheticDataProvider
from app.models.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.models.schemas import OrderRequest


@pytest.fixture
def broker() -> SimPaperBroker:
    return SimPaperBroker(starting_cash=100_000.0, data=SyntheticDataProvider())


def _buy(symbol="AAPL", qty=10.0, **kw) -> OrderRequest:
    return OrderRequest(symbol=symbol, asset_class=AssetClass.STOCK, side=OrderSide.BUY,
                        order_type=OrderType.MARKET, qty=qty, **kw)


def _sell(symbol="AAPL", qty=10.0, **kw) -> OrderRequest:
    return OrderRequest(symbol=symbol, asset_class=AssetClass.STOCK, side=OrderSide.SELL,
                        order_type=OrderType.MARKET, qty=qty, **kw)


def test_sim_is_always_paper(broker):
    assert broker.is_paper is True


def test_quote_and_ohlcv_are_deterministic(broker):
    q1 = broker.get_quote("AAPL")
    q2 = broker.get_quote("AAPL")
    assert q1.price == q2.price > 0
    series = broker.get_ohlcv("AAPL", "1h", limit=50)
    assert len(series.candles) == 50
    assert all(c.high >= c.low for c in series.candles)


def test_buy_opens_long_and_spends_cash(broker):
    start_cash = broker.cash
    res = broker.submit_order(_buy(qty=10))
    assert res.status == OrderStatus.FILLED
    assert res.filled_qty == 10
    positions = broker.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.direction == "long"
    assert pos.qty == 10
    assert broker.cash < start_cash  # cash spent on the buy


def test_round_trip_pnl_reflected_in_cash(broker):
    broker.submit_order(_buy(qty=10))
    cash_after_buy = broker.cash
    broker.submit_order(_sell(qty=10))
    # Position closed; cash returns to ~starting (minus any synthetic drift between fills).
    assert not broker.get_open_positions()
    assert broker.cash != cash_after_buy


def test_partial_close_keeps_position(broker):
    broker.submit_order(_buy(qty=10))
    broker.submit_order(_sell(qty=4))
    positions = broker.get_open_positions()
    assert len(positions) == 1
    assert positions[0].qty == 6
    assert positions[0].direction == "long"


def test_sell_opens_short(broker):
    res = broker.submit_order(_sell(symbol="TSLA", qty=5))
    assert res.status == OrderStatus.FILLED
    pos = broker.get_open_positions()[0]
    assert pos.direction == "short"
    assert pos.qty == 5


def test_flip_through_zero(broker):
    broker.submit_order(_buy(qty=5))
    broker.submit_order(_sell(qty=8))  # close 5 long, open 3 short
    pos = broker.get_open_positions()[0]
    assert pos.direction == "short"
    assert pos.qty == 3


def test_stop_and_take_profit_carry_to_position(broker):
    broker.submit_order(_buy(qty=10, stop_loss=90.0, take_profit=120.0))
    pos = broker.get_open_positions()[0]
    assert pos.stop_loss == 90.0
    assert pos.take_profit == 120.0


def test_reject_nonpositive_qty(broker):
    res = broker.submit_order(_buy(qty=0))
    assert res.status == OrderStatus.REJECTED


def test_close_all_flattens_everything(broker):
    broker.submit_order(_buy(symbol="AAPL", qty=10))
    broker.submit_order(_sell(symbol="TSLA", qty=5))
    assert len(broker.get_open_positions()) == 2
    broker.close_all_positions()
    assert broker.get_open_positions() == []


def test_account_equity_tracks_positions(broker):
    acct0 = broker.get_account()
    assert acct0.equity == pytest.approx(100_000.0, rel=1e-6)
    broker.submit_order(_buy(qty=10))
    acct1 = broker.get_account()
    # Equity is roughly conserved right after a fill (cash out, position in).
    assert acct1.open_positions == 1
    assert acct1.equity == pytest.approx(100_000.0, rel=0.05)


def test_limit_order_fills_at_limit_price(broker):
    req = OrderRequest(symbol="AAPL", asset_class=AssetClass.STOCK, side=OrderSide.BUY,
                       order_type=OrderType.LIMIT, qty=10, limit_price=50.0)
    res = broker.submit_order(req)
    assert res.avg_fill_price == 50.0


# ---- Alpaca adapter: order mapping via a mocked client ----


def test_alpaca_submit_order_maps_status(monkeypatch):
    from app.brokers import alpaca as alpaca_mod

    class _FakeOrder:
        id = "abc-123"
        status = "OrderStatus.FILLED"
        filled_qty = "10"
        filled_avg_price = "101.5"

    class _FakeClient:
        def submit_order(self, req):
            return _FakeOrder()

    # Bypass real construction: build instance without __init__, inject fakes.
    adapter = alpaca_mod.AlpacaBrokerAdapter.__new__(alpaca_mod.AlpacaBrokerAdapter)
    adapter._paper = True
    adapter._trading_client = _FakeClient()
    adapter._client = lambda: adapter._trading_client  # type: ignore[attr-defined]

    res = adapter.submit_order(_buy(qty=10))
    assert res.status == OrderStatus.FILLED
    assert res.broker_order_id == "abc-123"
    assert res.filled_qty == 10
    assert res.avg_fill_price == 101.5
