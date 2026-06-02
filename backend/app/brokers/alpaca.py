"""Alpaca broker adapter (paper by default).

Uses alpaca-py. All alpaca imports are lazy (inside methods) so the app boots even if the
package isn't installed; the adapter only requires it when actually used.

Defaults to Alpaca's PAPER trading environment. ``is_paper`` reflects the configured base
URL — a live URL flips it to False, and the execution layer gates live submission.
"""
from __future__ import annotations

from app.brokers.base import BrokerAdapter, BrokerError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.market import AlpacaDataProvider
from app.models.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.models.schemas import (
    AccountState,
    OHLCVSeries,
    OrderRequest,
    OrderResult,
    PositionView,
    Quote,
)

log = get_logger("broker.alpaca")

_STATUS_MAP = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.CANCELED,
}


class AlpacaBrokerAdapter(BrokerAdapter):
    name = "alpaca"
    supported_asset_classes = (AssetClass.STOCK, AssetClass.CRYPTO)

    def __init__(self, *, paper: bool = True) -> None:
        cfg = get_settings()
        if not cfg.alpaca_api_key or not cfg.alpaca_api_secret:
            raise BrokerError("Alpaca adapter requires ALPACA_API_KEY and ALPACA_API_SECRET")
        self._key = cfg.alpaca_api_key
        self._secret = cfg.alpaca_api_secret
        self._paper = paper
        self._trading_client = None
        self._data = AlpacaDataProvider(self._key, self._secret)

    @property
    def is_paper(self) -> bool:
        return self._paper

    def _client(self):
        if self._trading_client is None:
            from alpaca.trading.client import TradingClient

            self._trading_client = TradingClient(self._key, self._secret, paper=self._paper)
        return self._trading_client

    # ---- data (delegated) ----

    def get_quote(self, symbol: str) -> Quote:
        return self._data.get_quote(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        return self._data.get_ohlcv(symbol, timeframe, limit)

    # ---- account ----

    def get_account(self) -> AccountState:
        try:
            acct = self._client().get_account()
            positions = self._client().get_all_positions()
            return AccountState(
                equity=float(acct.equity),
                cash=float(acct.cash),
                open_positions=len(positions),
            )
        except Exception as exc:
            raise BrokerError(f"alpaca get_account failed: {exc}") from exc

    # ---- orders ----

    def submit_order(self, request: OrderRequest) -> OrderResult:
        try:
            from alpaca.trading.enums import OrderSide as AlpacaSide
            from alpaca.trading.enums import TimeInForce
            from alpaca.trading.requests import (
                LimitOrderRequest,
                MarketOrderRequest,
            )

            side = AlpacaSide.BUY if request.side == OrderSide.BUY else AlpacaSide.SELL
            # Crypto trades 24/7 (GTC); equities use DAY.
            tif = TimeInForce.GTC if request.asset_class == AssetClass.CRYPTO else TimeInForce.DAY

            if request.order_type == OrderType.LIMIT:
                if request.limit_price is None:
                    return OrderResult(status=OrderStatus.REJECTED, error="limit order needs limit_price")
                order_req = LimitOrderRequest(
                    symbol=request.symbol, qty=request.qty, side=side,
                    time_in_force=tif, limit_price=request.limit_price,
                )
            else:
                order_req = MarketOrderRequest(
                    symbol=request.symbol, qty=request.qty, side=side, time_in_force=tif,
                )

            log.info("submitting alpaca order", extra={"symbol": request.symbol, "side": request.side.value, "qty": request.qty, "paper": self._paper})
            order = self._client().submit_order(order_req)
            status = _STATUS_MAP.get(str(order.status).lower().split(".")[-1], OrderStatus.SUBMITTED)
            return OrderResult(
                broker_order_id=str(order.id),
                status=status,
                filled_qty=float(order.filled_qty or 0),
                avg_fill_price=float(order.filled_avg_price) if order.filled_avg_price else None,
                raw={"id": str(order.id), "status": str(order.status)},
            )
        except Exception as exc:
            # Ordinary failures are returned, not raised, so the order is never "unknown".
            log.exception("alpaca submit_order error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def get_open_positions(self) -> list[PositionView]:
        try:
            positions = self._client().get_all_positions()
        except Exception as exc:
            raise BrokerError(f"alpaca get_all_positions failed: {exc}") from exc
        views: list[PositionView] = []
        for i, p in enumerate(positions):
            qty = float(p.qty)
            views.append(
                PositionView(
                    id=i + 1,
                    symbol=p.symbol,
                    asset_class=(AssetClass.CRYPTO.value if "/" in p.symbol else AssetClass.STOCK.value),
                    direction="long" if qty >= 0 else "short",
                    qty=abs(qty),
                    entry_price=float(p.avg_entry_price),
                    status="open",
                    last_price=float(p.current_price) if p.current_price else None,
                    unrealized_pnl=float(p.unrealized_pl or 0),
                )
            )
        return views

    def close_position(self, symbol: str) -> OrderResult:
        try:
            order = self._client().close_position(symbol)
            return OrderResult(broker_order_id=str(order.id), status=OrderStatus.SUBMITTED, raw={"id": str(order.id)})
        except Exception as exc:
            log.exception("alpaca close_position error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def close_all_positions(self) -> list[OrderResult]:
        try:
            self._client().close_all_positions(cancel_orders=True)
            log.warning("alpaca close_all_positions requested")
            return [OrderResult(status=OrderStatus.SUBMITTED, raw={"close_all": True})]
        except Exception as exc:
            log.exception("alpaca close_all_positions error")
            return [OrderResult(status=OrderStatus.ERROR, error=str(exc))]

    def reconcile(self) -> dict:
        """Reconcile against broker: report open positions and any open (unfilled) orders."""
        info = super().reconcile()
        try:
            open_orders = self._client().get_orders()
            info["open_orders"] = len(open_orders)
        except Exception as exc:
            info["reconcile_error"] = str(exc)
        return info
