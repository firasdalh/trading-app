"""Tests for the MetaTrader 5 (Exness) adapter using an injected fake `mt5` module.

The real adapter connects to a running MT5 terminal in its constructor; we bypass that with
``__new__`` and inject a duck-typed fake to test the unit<->lot conversion, request building,
and response mapping deterministically.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.brokers.mt5_adapter import Mt5BrokerAdapter, normalize_symbol
from app.models.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.models.schemas import OrderRequest


class FakeMt5:
    # constants
    TIMEFRAME_M1 = 1; TIMEFRAME_M5 = 5; TIMEFRAME_M15 = 15; TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385; TIMEFRAME_H4 = 16388; TIMEFRAME_D1 = 16408
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0; ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0; ORDER_FILLING_IOC = 1; ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1; SYMBOL_FILLING_IOC = 2
    POSITION_TYPE_BUY = 0; POSITION_TYPE_SELL = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_ACTION_SLTP = 6

    def __init__(self, reject=False):
        self._reject = reject

    def symbol_select(self, sym, on=True):
        return True

    def symbol_info(self, sym):
        return SimpleNamespace(trade_contract_size=100000.0, volume_step=0.01,
                               volume_min=0.01, volume_max=100.0, filling_mode=self.SYMBOL_FILLING_FOK)

    def symbol_info_tick(self, sym):
        return SimpleNamespace(bid=1.1000, ask=1.1002, time=1_700_000_000)

    def copy_rates_from_pos(self, sym, tf, start, count):
        base = 1_700_000_000
        return [
            {"time": base + i * 3600, "open": 1.10 + i / 1000, "high": 1.11 + i / 1000,
             "low": 1.09 + i / 1000, "close": 1.105 + i / 1000, "tick_volume": 100 + i}
            for i in range(count)
        ]

    def account_info(self):
        return SimpleNamespace(equity=10000.0, balance=9500.0, trade_mode=0, login=123, server="Exness-Demo")

    def positions_get(self, symbol=None):
        return [SimpleNamespace(symbol="EURUSD", type=self.POSITION_TYPE_BUY, volume=1.0,
                                price_open=1.1000, sl=1.0950, tp=1.1100, price_current=1.1050,
                                profit=50.0, ticket=555)]

    def order_send(self, req):
        if self._reject:
            return SimpleNamespace(retcode=10004, order=0, deal=0, volume=0.0, price=0.0, comment="requote")
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=999, deal=888,
                               volume=req.get("volume", 0.0), price=req.get("price", 0.0), comment="done")

    def last_error(self):
        return (0, "ok")

    def history_deals_get(self, frm, to):
        # One entry deal (profit 0) + one closing deal with profit/swap/commission.
        return [
            SimpleNamespace(profit=0.0, swap=0.0, commission=0.0),
            SimpleNamespace(profit=150.0, swap=-2.0, commission=-1.0),
        ]


def _adapter(fake=None) -> Mt5BrokerAdapter:
    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._mt5 = fake or FakeMt5()
    a._paper = True
    return a


def test_normalize_symbol():
    assert normalize_symbol("EUR/USD") == "EURUSD"
    assert normalize_symbol("XAU_USD") == "XAUUSD"
    assert normalize_symbol("eurusd") == "EURUSD"


def test_quote_mid():
    q = _adapter().get_quote("EUR/USD")
    assert q.symbol == "EURUSD"
    assert abs(q.price - 1.1001) < 1e-9


def test_ohlcv_mapping():
    s = _adapter().get_ohlcv("EURUSD", "1h", limit=20)
    assert len(s.candles) == 20
    assert s.candles[0].close == 1.105


def test_units_to_lots_conversion():
    a = _adapter()
    info = a._mt5.symbol_info("EURUSD")
    assert a._units_to_lots(info, 100000) == 1.0     # 1 standard lot
    assert a._units_to_lots(info, 150000) == 1.5
    assert a._units_to_lots(info, 1000) == 0.01      # floored to volume_step, clamped to min


def test_account():
    acct = _adapter().get_account()
    assert acct.equity == 10000.0 and acct.cash == 9500.0 and acct.open_positions == 1


def test_submit_order_filled_buy():
    a = _adapter()
    res = a.submit_order(OrderRequest(symbol="EUR/USD", asset_class=AssetClass.FOREX,
                                      side=OrderSide.BUY, order_type=OrderType.MARKET,
                                      qty=100000, stop_loss=1.095, take_profit=1.110))
    assert res.status == OrderStatus.FILLED
    assert res.avg_fill_price == 1.1002      # filled at ask for a buy
    assert res.filled_qty == 1.0             # lots


def test_submit_order_rejected():
    a = _adapter(FakeMt5(reject=True))
    res = a.submit_order(OrderRequest(symbol="EURUSD", asset_class=AssetClass.FOREX,
                                      side=OrderSide.SELL, order_type=OrderType.MARKET, qty=100000))
    assert res.status == OrderStatus.REJECTED
    assert res.error == "requote"


def test_realized_pnl_sums_deal_history():
    from datetime import datetime, timezone
    pnl = _adapter().get_realized_pnl(datetime.now(timezone.utc))
    assert pnl == 147.0  # 150 profit - 2 swap - 1 commission


def test_set_sl_tp():
    res = _adapter().set_sl_tp("XAUUSDm", stop_loss=4400.0, take_profit=4600.0)
    assert res.status == OrderStatus.SUBMITTED


def test_open_positions_lots_to_units():
    views = _adapter().get_open_positions()
    assert len(views) == 1
    assert views[0].direction == "long"
    assert views[0].qty == 100000.0  # 1.0 lot * contract_size 100000
