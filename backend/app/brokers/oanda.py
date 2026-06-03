"""OANDA broker adapter (forex + metals) via oandapyV20.

Lazy imports so the app boots without the package. Defaults to OANDA's "practice"
(paper) environment; ``is_paper`` is True unless OANDA_ENV is "live".

OANDA instruments use an underscore form ("EUR_USD", "XAU_USD"); we normalise common
inputs ("EUR/USD", "EURUSD", "XAUUSD") to it. Units are signed (buy positive, sell
negative). Stop/target are attached on-fill so OANDA enforces them broker-side; our Monitor
still tracks them too.
"""
from __future__ import annotations

from datetime import datetime, timezone

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

log = get_logger("broker.oanda")

_GRANULARITY = {
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
    "1h": "H1", "4h": "H4", "1d": "D",
}


def normalize_instrument(symbol: str) -> str:
    """Normalise a pair to OANDA's BASE_QUOTE form."""
    s = symbol.upper().replace("/", "_").replace("-", "_")
    if "_" in s:
        return s
    if len(s) == 6:  # e.g. EURUSD, XAUUSD
        return f"{s[:3]}_{s[3:]}"
    return s


class OandaBrokerAdapter(BrokerAdapter):
    name = "oanda"
    supported_asset_classes = (AssetClass.FOREX, AssetClass.METAL)

    def __init__(self) -> None:
        cfg = get_settings()
        if not cfg.oanda_api_key or not cfg.oanda_account_id:
            raise BrokerError("OANDA adapter requires OANDA_API_KEY and OANDA_ACCOUNT_ID")
        try:
            from oandapyV20 import API  # lazy
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("oandapyV20 is not installed") from exc
        self._env = cfg.oanda_env or "practice"
        self._account_id = cfg.oanda_account_id
        self._client = API(access_token=cfg.oanda_api_key, environment=self._env)

    @property
    def is_paper(self) -> bool:
        return self._env != "live"

    # ---- data ----

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        import oandapyV20.endpoints.instruments as instruments

        gran = _GRANULARITY.get(timeframe)
        if gran is None:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        inst = normalize_instrument(symbol)
        req = instruments.InstrumentsCandles(
            instrument=inst, params={"granularity": gran, "count": min(limit, 5000), "price": "M"}
        )
        self._client.request(req)
        candles = []
        for c in req.response.get("candles", []):
            if not c.get("complete", True):
                continue
            mid = c["mid"]
            candles.append(Candle(
                ts=datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
                open=float(mid["o"]), high=float(mid["h"]), low=float(mid["l"]),
                close=float(mid["c"]), volume=float(c.get("volume", 0)),
            ))
        return OHLCVSeries(symbol=inst, timeframe=timeframe, candles=candles[-limit:])

    def get_quote(self, symbol: str) -> Quote:
        import oandapyV20.endpoints.pricing as pricing

        inst = normalize_instrument(symbol)
        req = pricing.PricingInfo(accountID=self._account_id, params={"instruments": inst})
        self._client.request(req)
        prices = req.response.get("prices", [])
        if not prices:
            raise BrokerError(f"no price for {inst}")
        p = prices[0]
        bid = float(p["bids"][0]["price"]) if p.get("bids") else 0.0
        ask = float(p["asks"][0]["price"]) if p.get("asks") else 0.0
        mid = (bid + ask) / 2 if bid and ask else (ask or bid)
        return Quote(symbol=inst, price=mid, ts=datetime.now(timezone.utc))

    # ---- account ----

    def get_account(self) -> AccountState:
        import oandapyV20.endpoints.accounts as accounts

        try:
            req = accounts.AccountSummary(accountID=self._account_id)
            self._client.request(req)
            acct = req.response["account"]
            return AccountState(
                equity=float(acct.get("NAV", acct.get("balance", 0))),
                cash=float(acct.get("balance", 0)),
                open_positions=int(acct.get("openPositionCount", 0)),
            )
        except Exception as exc:
            raise BrokerError(f"oanda AccountSummary failed: {exc}") from exc

    # ---- orders ----

    def submit_order(self, request: OrderRequest) -> OrderResult:
        try:
            import oandapyV20.endpoints.orders as orders

            inst = normalize_instrument(request.symbol)
            units = request.qty if request.side == OrderSide.BUY else -request.qty
            order: dict = {
                "type": "MARKET" if request.order_type == OrderType.MARKET else "LIMIT",
                "instrument": inst,
                "units": str(units),
                "timeInForce": "FOK" if request.order_type == OrderType.MARKET else "GTC",
                "positionFill": "DEFAULT",
            }
            if request.order_type == OrderType.LIMIT and request.limit_price is not None:
                order["price"] = str(request.limit_price)
                order["timeInForce"] = "GTC"
            if request.stop_loss is not None:
                order["stopLossOnFill"] = {"price": str(request.stop_loss)}
            if request.take_profit is not None:
                order["takeProfitOnFill"] = {"price": str(request.take_profit)}

            log.info("submitting oanda order", extra={"instrument": inst, "units": units, "paper": self.is_paper})
            req = orders.OrderCreate(accountID=self._account_id, data={"order": order})
            self._client.request(req)
            resp = req.response
            fill = resp.get("orderFillTransaction")
            if fill:
                return OrderResult(
                    broker_order_id=str(fill.get("orderID") or fill.get("id")),
                    status=OrderStatus.FILLED,
                    filled_qty=abs(float(fill.get("units", units))),
                    avg_fill_price=float(fill["price"]) if fill.get("price") else None,
                    raw={"fill": fill.get("id")},
                )
            create = resp.get("orderCreateTransaction", {})
            cancel = resp.get("orderCancelTransaction")
            status = OrderStatus.REJECTED if cancel else OrderStatus.SUBMITTED
            return OrderResult(broker_order_id=str(create.get("id")), status=status,
                               raw={"create": create.get("id")},
                               error=cancel.get("reason") if cancel else None)
        except Exception as exc:
            log.exception("oanda submit_order error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def get_open_positions(self) -> list[PositionView]:
        import oandapyV20.endpoints.positions as positions

        try:
            req = positions.OpenPositions(accountID=self._account_id)
            self._client.request(req)
        except Exception as exc:
            raise BrokerError(f"oanda OpenPositions failed: {exc}") from exc
        views: list[PositionView] = []
        for i, p in enumerate(req.response.get("positions", [])):
            long_units = float(p["long"]["units"]) if p.get("long") else 0.0
            short_units = float(p["short"]["units"]) if p.get("short") else 0.0
            net = long_units + short_units
            if net == 0:
                continue
            leg = p["long"] if net > 0 else p["short"]
            views.append(PositionView(
                id=i + 1, symbol=p["instrument"], asset_class=AssetClass.FOREX.value,
                direction="long" if net > 0 else "short", qty=abs(net),
                entry_price=float(leg.get("averagePrice", 0) or 0), status="open",
                unrealized_pnl=float(p.get("unrealizedPL", 0) or 0),
            ))
        return views

    def close_position(self, symbol: str) -> OrderResult:
        import oandapyV20.endpoints.positions as positions

        inst = normalize_instrument(symbol)
        try:
            # Close both sides; OANDA ignores the side with no units.
            req = positions.PositionClose(
                accountID=self._account_id, instrument=inst,
                data={"longUnits": "ALL", "shortUnits": "ALL"},
            )
            self._client.request(req)
            return OrderResult(status=OrderStatus.SUBMITTED, raw={"closed": inst})
        except Exception as exc:
            log.exception("oanda close_position error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def close_all_positions(self) -> list[OrderResult]:
        return [self.close_position(p.symbol) for p in self.get_open_positions()]
