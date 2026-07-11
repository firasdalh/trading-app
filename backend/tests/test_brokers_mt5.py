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
        if "/" in sym:  # real MT5/Exness symbols have no separator -> a generic name misses here
            return None
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
        # One entry deal (profit 0) + one closing deal with profit/swap/commission. Trade deals
        # carry a symbol (balance/credit ops don't — see test_get_realized_pnl_excludes_*).
        return [
            SimpleNamespace(symbol="EURUSD", profit=0.0, swap=0.0, commission=0.0),
            SimpleNamespace(symbol="EURUSD", profit=150.0, swap=-2.0, commission=-1.0),
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


def test_clamp_lots_to_step_and_min():
    # Qty is in LOTS everywhere now; the adapter only clamps to the symbol's step/min/max.
    a = _adapter()
    info = a._mt5.symbol_info("EURUSD")
    assert a._clamp_lots(info, 1.0) == 1.0
    assert a._clamp_lots(info, 1.5) == 1.5
    assert a._clamp_lots(info, 0.005) == 0.01        # clamped up to the broker minimum lot


def test_account():
    acct = _adapter().get_account()
    assert acct.equity == 10000.0 and acct.cash == 9500.0 and acct.open_positions == 1


def test_submit_order_filled_buy():
    a = _adapter()
    res = a.submit_order(OrderRequest(symbol="EUR/USD", asset_class=AssetClass.FOREX,
                                      side=OrderSide.BUY, order_type=OrderType.MARKET,
                                      qty=1.0, stop_loss=1.095, take_profit=1.110))   # qty in LOTS
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


def test_set_sl_tp_rounds_to_symbol_digits():
    # MT5 rejects over-precise stops; the adapter must normalize to the symbol's digits.
    class _Fake(FakeMt5):
        def __init__(self):
            super().__init__()
            self.sent = None

        def symbol_info(self, sym):
            return SimpleNamespace(digits=2)

        def order_send(self, req):
            self.sent = req
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done")

    fake = _Fake()
    _adapter(fake).set_sl_tp("XAUUSDm", stop_loss=4473.54231, take_profit=4397.48999)
    assert fake.sent["sl"] == 4473.54 and fake.sent["tp"] == 4397.49


def test_set_sl_tp_clamps_to_min_stop_distance():
    # A short whose breakeven SL lands too close to the ask must be pushed to ask + min_dist.
    class _Fake(FakeMt5):
        def __init__(self):
            super().__init__()
            self.sent = None

        def symbol_info(self, sym):
            # digits=2, point=0.01, stops_level=50 -> min distance = 0.50
            return SimpleNamespace(digits=2, point=0.01, trade_stops_level=50)

        def symbol_info_tick(self, sym):
            return SimpleNamespace(bid=4434.80, ask=4434.90)

        def positions_get(self, symbol=None):
            return [SimpleNamespace(symbol="XAUUSDm", type=self.POSITION_TYPE_SELL, volume=1.0,
                                    price_open=4449.19, sl=4473.54, tp=4397.48, ticket=777)]

        def order_send(self, req):
            self.sent = req
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done")

    fake = _Fake()
    # Requested breakeven SL (4434.95) is only 0.05 above the ask — closer than the 0.50 min.
    _adapter(fake).set_sl_tp("XAUUSDm", stop_loss=4434.95)
    assert fake.sent["sl"] == round(4434.90 + 0.50, 2)  # pushed to ask + min_dist = 4435.40


def test_open_positions_returns_lots():
    views = _adapter().get_open_positions()
    assert len(views) == 1
    assert views[0].direction == "long"
    assert views[0].qty == 1.0  # LOTS (qty is in lots everywhere now)


def test_serialized_mt5_passes_constants_and_serializes_calls():
    """The proxy wrapping the MetaTrader5 module must pass constants through unlocked and hold the
    shared lock for the duration of every call, so concurrent threads can't interleave on the
    single terminal connection."""
    import threading

    from app.brokers.mt5_adapter import _MT5_LOCK, _SerializedMt5

    started, release = threading.Event(), threading.Event()

    class _Fake:
        TIMEFRAME_H1 = 16385  # a constant

        def hold(self):
            started.set()
            release.wait(timeout=2)  # keep the call (and thus the lock) in flight
            return "ok"

    proxy = _SerializedMt5(_Fake())
    assert proxy.TIMEFRAME_H1 == 16385  # constant passes straight through (not wrapped)

    worker = threading.Thread(target=proxy.hold)
    worker.start()
    assert started.wait(timeout=2)  # worker is now inside the call, holding _MT5_LOCK
    # A different thread (this one) must NOT be able to grab the lock while the call is in flight.
    locked_out = not _MT5_LOCK.acquire(blocking=False)
    if not locked_out:
        _MT5_LOCK.release()
    release.set()
    worker.join(timeout=2)
    assert locked_out is True


def test_get_realized_pnl_excludes_balance_operations():
    """A withdrawal / credit deal (no symbol) must NOT count as trading P&L — otherwise it
    distorts realized and can falsely trip the daily-loss breaker (the real-account bug:
    trading was +159.17 but a -818.79 withdrawal made it look like a -659.62 'loss')."""
    from datetime import datetime, timezone

    class _Deals:
        def history_deals_get(self, a, b):
            return [
                SimpleNamespace(symbol="XAUUSDm", profit=159.17, swap=0.0, commission=0.0),
                SimpleNamespace(symbol="", profit=-818.79, swap=0.0, commission=0.0),  # withdrawal/credit
            ]

    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._mt5 = _Deals()
    assert a.get_realized_pnl(datetime(2026, 6, 10, tzinfo=timezone.utc)) == 159.17


def test_can_open_honors_trade_mode():
    """can_open() must mirror the broker's per-instrument trade_mode so the risk layer never
    approves a trade the terminal would reject (0 DISABLED, 1 LONGONLY, 2 SHORTONLY, 3 CLOSEONLY,
    4 FULL)."""
    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._resolve_symbol = lambda s: s

    def with_mode(mode):
        a._symbol_info = lambda s: SimpleNamespace(trade_mode=mode)
        return a

    with_mode(4)  # FULL
    assert a.can_open("X", "long") == (True, None) and a.can_open("X", "short") == (True, None)
    with_mode(0)  # DISABLED -> neither side
    assert a.can_open("IN50m", "long")[0] is False and a.can_open("IN50m", "short")[0] is False
    with_mode(3)  # CLOSEONLY -> neither side
    assert a.can_open("X", "long")[0] is False and a.can_open("X", "short")[0] is False
    with_mode(1)  # LONGONLY -> short refused, long ok
    assert a.can_open("X", "short")[0] is False and a.can_open("X", "long") == (True, None)
    with_mode(2)  # SHORTONLY -> long refused, short ok
    assert a.can_open("X", "long")[0] is False and a.can_open("X", "short") == (True, None)
