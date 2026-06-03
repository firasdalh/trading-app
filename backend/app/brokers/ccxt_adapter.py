"""ccxt broker adapter (crypto exchanges).

Wraps a ccxt exchange behind the common BrokerAdapter interface. ccxt is imported lazily in
the constructor so the app boots without it. Uses the exchange's sandbox/testnet when the
app is in paper mode (the default) — ``is_paper`` reflects that.

Protective stop/target are handled by our Monitor (not attached to the exchange order),
which keeps behaviour uniform across brokers.
"""
from __future__ import annotations

from app.brokers.base import BrokerAdapter, BrokerError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.models.schemas import (
    AccountState,
    Candle,
    OHLCVSeries,
    OrderRequest,
    OrderResult,
    PositionView,
    Quote,
)

log = get_logger("broker.ccxt")

# ccxt uses the same timeframe strings we do; validate against this set.
_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}

_STATUS_MAP = {
    "open": OrderStatus.SUBMITTED,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.CANCELED,
}

# Currencies treated as cash/quote when summarising a balance.
_QUOTE_CCYS = ("USDT", "USD", "USDC", "BUSD")


class CcxtBrokerAdapter(BrokerAdapter):
    name = "ccxt"
    supported_asset_classes = (AssetClass.CRYPTO,)

    def __init__(self, *, paper: bool = True) -> None:
        cfg = get_settings()
        try:
            import ccxt  # lazy
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("ccxt is not installed") from exc
        if not hasattr(ccxt, cfg.ccxt_exchange):
            raise BrokerError(f"unknown ccxt exchange: {cfg.ccxt_exchange}")

        self._paper = paper
        klass = getattr(ccxt, cfg.ccxt_exchange)
        self._exchange = klass({
            "apiKey": cfg.ccxt_api_key or None,
            "secret": cfg.ccxt_api_secret or None,
            "enableRateLimit": True,
        })
        if paper:
            # Use the exchange testnet/sandbox where supported. Not all exchanges have one;
            # if not, this raises and we surface it as a config error rather than trading live.
            try:
                self._exchange.set_sandbox_mode(True)
            except Exception as exc:  # noqa: BLE001
                raise BrokerError(
                    f"{cfg.ccxt_exchange} has no sandbox; refusing to default to live"
                ) from exc

    @property
    def is_paper(self) -> bool:
        return self._paper

    # ---- data ----

    def get_quote(self, symbol: str) -> Quote:
        from datetime import datetime, timezone

        ticker = self._exchange.fetch_ticker(symbol)
        ts = ticker.get("timestamp")
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else datetime.now(timezone.utc)
        return Quote(symbol=symbol, price=float(ticker["last"]), ts=when)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        from datetime import datetime, timezone

        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        rows = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        candles = [
            Candle(
                ts=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5] or 0),
            )
            for r in rows
        ]
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=candles)

    # ---- account ----

    def get_account(self) -> AccountState:
        try:
            bal = self._exchange.fetch_balance()
        except Exception as exc:
            raise BrokerError(f"ccxt fetch_balance failed: {exc}") from exc
        total = bal.get("total", {}) or {}
        free = bal.get("free", {}) or {}
        equity = sum(float(total.get(c, 0) or 0) for c in _QUOTE_CCYS)
        cash = sum(float(free.get(c, 0) or 0) for c in _QUOTE_CCYS)
        return AccountState(equity=round(equity, 2), cash=round(cash, 2))

    # ---- orders ----

    def submit_order(self, request: OrderRequest) -> OrderResult:
        try:
            side = "buy" if request.side == OrderSide.BUY else "sell"
            otype = "limit" if request.order_type == OrderType.LIMIT else "market"
            price = request.limit_price if otype == "limit" else None
            log.info("submitting ccxt order", extra={"symbol": request.symbol, "side": side, "qty": request.qty, "paper": self._paper})
            order = self._exchange.create_order(request.symbol, otype, side, request.qty, price)
            status = _STATUS_MAP.get(str(order.get("status", "")).lower(), OrderStatus.SUBMITTED)
            return OrderResult(
                broker_order_id=str(order.get("id")),
                status=status,
                filled_qty=float(order.get("filled") or 0),
                avg_fill_price=float(order["average"]) if order.get("average") else None,
                raw={"id": order.get("id"), "status": order.get("status")},
            )
        except Exception as exc:
            log.exception("ccxt submit_order error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def get_open_positions(self) -> list[PositionView]:
        # Spot exchanges have no positions; derivatives expose fetch_positions.
        if not self._exchange.has.get("fetchPositions"):
            return []
        try:
            raw = self._exchange.fetch_positions()
        except Exception as exc:
            raise BrokerError(f"ccxt fetch_positions failed: {exc}") from exc
        views: list[PositionView] = []
        for i, p in enumerate(raw):
            contracts = float(p.get("contracts") or 0)
            if contracts == 0:
                continue
            views.append(PositionView(
                id=i + 1, symbol=p.get("symbol", ""), asset_class=AssetClass.CRYPTO.value,
                direction="long" if p.get("side") == "long" else "short",
                qty=abs(contracts), entry_price=float(p.get("entryPrice") or 0),
                status="open", last_price=float(p.get("markPrice") or 0),
                unrealized_pnl=float(p.get("unrealizedPnl") or 0),
            ))
        return views

    def close_position(self, symbol: str) -> OrderResult:
        for pos in self.get_open_positions():
            if pos.symbol == symbol:
                side = OrderSide.SELL if pos.direction == "long" else OrderSide.BUY
                return self.submit_order(OrderRequest(
                    symbol=symbol, asset_class=AssetClass.CRYPTO, side=side,
                    order_type=OrderType.MARKET, qty=pos.qty,
                ))
        return OrderResult(status=OrderStatus.REJECTED, error=f"no open position for {symbol}")

    def close_all_positions(self) -> list[OrderResult]:
        return [self.close_position(p.symbol) for p in self.get_open_positions()]

    def list_symbols(self, asset_class: AssetClass | None = None) -> list[str]:
        try:
            self._exchange.load_markets()
            return sorted(self._exchange.symbols or [])[:300]
        except Exception:  # noqa: BLE001
            return []
