"""SimPaperBroker — a self-contained simulated paper broker.

Lets the entire pipeline run with no external account: fills market/limit orders at the
current price from a ``MarketDataProvider`` and marks positions to market. State is
in-process (the DB holds the durable order/position audit trail in the execution layer).

This is ALWAYS paper. It exists for offline dev, tests, and as a safe default when no real
broker keys are configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.brokers.base import BrokerAdapter
from app.core.logging import get_logger
from app.data.market import MarketDataProvider, SyntheticDataProvider
from app.models.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.models.schemas import (
    AccountState,
    OHLCVSeries,
    OrderRequest,
    OrderResult,
    PositionView,
    Quote,
)

log = get_logger("broker.sim")


@dataclass
class _SimPosition:
    symbol: str
    asset_class: str
    net_qty: float = 0.0      # signed: long > 0, short < 0
    avg_price: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None


@dataclass
class SimPaperBroker(BrokerAdapter):
    name: str = "sim"
    starting_cash: float = 100_000.0
    data: MarketDataProvider = field(default_factory=SyntheticDataProvider)
    supported_asset_classes: tuple[AssetClass, ...] = (
        AssetClass.STOCK,
        AssetClass.CRYPTO,
        AssetClass.FOREX,
        AssetClass.METAL,
    )

    def __post_init__(self) -> None:
        self.cash: float = self.starting_cash
        self._positions: dict[str, _SimPosition] = {}
        self._order_seq = 0

    @property
    def is_paper(self) -> bool:
        return True

    # ---- data ----

    def get_quote(self, symbol: str) -> Quote:
        return self.data.get_quote(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        return self.data.get_ohlcv(symbol, timeframe, limit)

    # ---- account ----

    def get_account(self) -> AccountState:
        market_value = 0.0
        for pos in self._positions.values():
            if pos.net_qty == 0:
                continue
            price = self.data.get_quote(pos.symbol).price
            market_value += pos.net_qty * price
        equity = self.cash + market_value
        open_positions = sum(1 for p in self._positions.values() if p.net_qty != 0)
        return AccountState(equity=round(equity, 2), cash=round(self.cash, 2), open_positions=open_positions)

    # ---- orders ----

    def _fill_price(self, request: OrderRequest) -> float:
        if request.order_type == OrderType.LIMIT and request.limit_price:
            return request.limit_price
        return self.data.get_quote(request.symbol).price

    def submit_order(self, request: OrderRequest) -> OrderResult:
        try:
            if request.qty <= 0:
                return OrderResult(status=OrderStatus.REJECTED, error="qty must be > 0")
            fill = self._fill_price(request)
            signed = request.qty if request.side == OrderSide.BUY else -request.qty
            pos = self._positions.setdefault(
                request.symbol, _SimPosition(symbol=request.symbol, asset_class=request.asset_class.value)
            )
            old_qty = pos.net_qty
            new_qty = old_qty + signed

            if old_qty == 0 or (old_qty > 0) == (signed > 0):
                # opening or increasing in the same direction -> weighted avg entry
                total = abs(old_qty) + abs(signed)
                pos.avg_price = (abs(old_qty) * pos.avg_price + abs(signed) * fill) / total if total else fill
            elif (new_qty > 0) == (old_qty > 0) or new_qty == 0:
                # reducing/closing same direction -> avg entry unchanged
                pass
            else:
                # flipped through zero -> residual takes the new fill as its entry
                pos.avg_price = fill

            pos.net_qty = new_qty
            # Cash: buying spends, selling receives (mark-to-market identity).
            self.cash -= signed * fill
            # Carry protective levels for the resulting position.
            if request.stop_loss is not None:
                pos.stop_loss = request.stop_loss
            if request.take_profit is not None:
                pos.take_profit = request.take_profit
            if pos.net_qty == 0:
                self._positions.pop(request.symbol, None)

            self._order_seq += 1
            order_id = f"sim-{self._order_seq}"
            log.info(
                "sim order filled",
                extra={"symbol": request.symbol, "side": request.side.value, "qty": request.qty, "fill": fill},
            )
            return OrderResult(
                broker_order_id=order_id,
                status=OrderStatus.FILLED,
                filled_qty=request.qty,
                avg_fill_price=round(fill, 4),
                raw={"sim": True, "net_qty": pos.net_qty if request.symbol in self._positions else 0.0},
            )
        except Exception as exc:  # never leave caller without a definite outcome
            log.exception("sim submit_order error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def get_open_positions(self) -> list[PositionView]:
        views: list[PositionView] = []
        for i, pos in enumerate(p for p in self._positions.values() if p.net_qty != 0):
            price = self.data.get_quote(pos.symbol).price
            unrealized = pos.net_qty * (price - pos.avg_price)
            views.append(
                PositionView(
                    id=i + 1,
                    symbol=pos.symbol,
                    asset_class=pos.asset_class,
                    direction="long" if pos.net_qty > 0 else "short",
                    qty=abs(pos.net_qty),
                    entry_price=round(pos.avg_price, 4),
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    status="open",
                    last_price=round(price, 4),
                    unrealized_pnl=round(unrealized, 2),
                )
            )
        return views

    def close_position(self, symbol: str) -> OrderResult:
        pos = self._positions.get(symbol)
        if not pos or pos.net_qty == 0:
            return OrderResult(status=OrderStatus.REJECTED, error=f"no open position for {symbol}")
        side = OrderSide.SELL if pos.net_qty > 0 else OrderSide.BUY
        return self.submit_order(
            OrderRequest(
                symbol=symbol,
                asset_class=AssetClass(pos.asset_class),
                side=side,
                order_type=OrderType.MARKET,
                qty=abs(pos.net_qty),
            )
        )

    def set_sl_tp(self, symbol: str, stop_loss: float | None = None,
                  take_profit: float | None = None) -> OrderResult:
        pos = self._positions.get(symbol)
        if not pos or pos.net_qty == 0:
            return OrderResult(status=OrderStatus.REJECTED, error=f"no open position for {symbol}")
        if stop_loss is not None:
            pos.stop_loss = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit
        return OrderResult(status=OrderStatus.SUBMITTED, raw={"sim_sltp": True})

    def close_all_positions(self) -> list[OrderResult]:
        results = [self.close_position(sym) for sym in list(self._positions.keys())]
        log.warning("sim close_all_positions", extra={"count": len(results)})
        return results
