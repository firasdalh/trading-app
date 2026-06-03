"""MetaTrader 5 broker adapter — for Exness (and any MT5 broker).

Exness has no public REST API; it runs on MetaTrader 5. This adapter uses the official
``MetaTrader5`` Python package, which connects to a locally-installed MT5 terminal that is
logged into the account. Windows only. The terminal must be running with "Algo Trading"
enabled.

Safety:
- ``is_paper`` is derived from the MT5 account trade mode (demo/contest = paper, real =
  live). The executor's live-confirmation gate therefore applies automatically to a real
  Exness account.
- Risk sizing upstream produces a quantity in *units*; MT5 trades in *lots*, so we convert
  via the symbol's contract size and clamp to the broker's volume step/min/max.
- Stop-loss/take-profit are attached to the order so MT5 enforces them broker-side.

All MetaTrader5 access goes through ``self._mt5`` so the mapping logic is unit-testable with
an injected fake module.
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

log = get_logger("broker.mt5")

# Our timeframe string -> MT5 TIMEFRAME_* attribute name.
_TF_ATTR = {
    "1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30", "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1",
}


def normalize_symbol(symbol: str) -> str:
    """MT5/Exness symbols have no separator: EUR/USD -> EURUSD, XAU/USD -> XAUUSD."""
    return symbol.upper().replace("/", "").replace("_", "").replace("-", "")


class Mt5BrokerAdapter(BrokerAdapter):
    name = "mt5"
    supported_asset_classes = (AssetClass.FOREX, AssetClass.METAL, AssetClass.CRYPTO)

    def __init__(self) -> None:
        from app.brokers.mt5_credentials import resolve_mt5_credentials

        try:
            import MetaTrader5 as mt5  # lazy; Windows-only
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("MetaTrader5 package not installed (pip install MetaTrader5)") from exc
        self._mt5 = mt5

        creds = resolve_mt5_credentials()
        kwargs: dict = {}
        if creds["path"]:
            kwargs["path"] = creds["path"]
        # login==0 -> attach to the account the running terminal is already logged into.
        if creds["login"]:
            kwargs.update(login=creds["login"], password=creds["password"], server=creds["server"])

        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            raise BrokerError(f"MT5 initialize failed: {err}. Is the terminal running and logged in?")

        info = mt5.account_info()
        if info is None:
            raise BrokerError("MT5 connected but no account info — check terminal login")
        # trade_mode: 0 DEMO, 1 CONTEST, 2 REAL
        self._paper = getattr(info, "trade_mode", 2) != getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        log.info("MT5 connected", extra={"login": getattr(info, "login", None),
                                         "server": getattr(info, "server", None), "paper": self._paper})

    @property
    def is_paper(self) -> bool:
        return self._paper

    # ---- helpers ----

    def _tf(self, timeframe: str):
        attr = _TF_ATTR.get(timeframe)
        if attr is None:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        return getattr(self._mt5, attr)

    def _resolve_symbol(self, symbol: str) -> str:
        """Map a generic symbol to the broker's actual name and select it in Market Watch.

        Exness (and other MT5 brokers) sometimes suffix symbols (EURUSDm, XAUUSD.z, ...), so
        if the exact name isn't found we search the broker's symbol list for the closest base
        match. Results are cached per adapter.
        """
        sym = normalize_symbol(symbol)
        cache = getattr(self, "_symbol_cache", None)
        if cache is None:
            cache = {}
            self._symbol_cache = cache
        if sym in cache:
            return cache[sym]

        name = sym if self._mt5.symbol_info(sym) is not None else None
        if name is None:
            all_syms = self._mt5.symbols_get() or []
            candidates = [
                s.name for s in all_syms
                if s.name.upper().replace(".", "").replace("-", "").startswith(sym)
            ]
            if candidates:
                name = sorted(candidates, key=len)[0]  # closest to the base name
        if name is None:
            raise BrokerError(f"symbol {sym} not found on this MT5 account")
        self._mt5.symbol_select(name, True)
        cache[sym] = name
        return name

    def _symbol_info(self, symbol: str):
        info = self._mt5.symbol_info(self._resolve_symbol(symbol))
        if info is None:
            raise BrokerError(f"unknown MT5 symbol: {symbol}")
        return info

    # ---- data ----

    def get_quote(self, symbol: str) -> Quote:
        name = self._resolve_symbol(symbol)
        tick = self._mt5.symbol_info_tick(name)
        bid = ask = 0.0
        ts_epoch = 0
        if tick is not None:
            bid, ask, ts_epoch = float(tick.bid), float(tick.ask), int(getattr(tick, "time", 0))
        if not (bid or ask):
            # Fall back to the symbol's last known bid/ask (e.g. market just (re)opened).
            info = self._mt5.symbol_info(name)
            if info is not None:
                bid, ask = float(getattr(info, "bid", 0)), float(getattr(info, "ask", 0))
        if not (bid or ask):
            raise BrokerError(f"no price for {name} (is the market open / symbol enabled?)")
        mid = (bid + ask) / 2 if bid and ask else (ask or bid)
        ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc) if ts_epoch else datetime.now(timezone.utc)
        return Quote(symbol=name, price=mid, ts=ts)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        name = self._resolve_symbol(symbol)
        rates = self._mt5.copy_rates_from_pos(name, self._tf(timeframe), 0, limit)
        candles = []
        for r in rates if rates is not None else []:
            candles.append(Candle(
                ts=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
                open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                close=float(r["close"]), volume=float(r["tick_volume"]),
            ))
        return OHLCVSeries(symbol=name, timeframe=timeframe, candles=candles)

    # ---- account ----

    def get_account(self) -> AccountState:
        info = self._mt5.account_info()
        if info is None:
            raise BrokerError("MT5 account_info unavailable")
        positions = self._mt5.positions_get() or []
        return AccountState(
            equity=float(info.equity), cash=float(info.balance), open_positions=len(positions),
        )

    # ---- orders ----

    def _units_to_lots(self, info, units: float) -> float:
        contract = float(getattr(info, "trade_contract_size", 1) or 1)
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        vmin = float(getattr(info, "volume_min", step) or step)
        vmax = float(getattr(info, "volume_max", 1e9) or 1e9)
        lots = units / contract if contract else units
        # Floor to the step so we never round risk up; clamp to [min, max].
        lots = (int(lots / step) * step) if step else lots
        lots = max(vmin, min(vmax, lots))
        return round(lots, 2)

    def _filling(self, info):
        mt5 = self._mt5
        mode = getattr(info, "filling_mode", 0)
        # filling_mode is a bitmask; prefer FOK, then IOC, else RETURN.
        if mode & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            return mt5.ORDER_FILLING_FOK
        if mode & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            return mt5.ORDER_FILLING_IOC
        return getattr(mt5, "ORDER_FILLING_RETURN", 2)

    def submit_order(self, request: OrderRequest) -> OrderResult:
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(request.symbol)
            info = self._symbol_info(sym)
            tick = mt5.symbol_info_tick(sym)
            is_buy = request.side == OrderSide.BUY
            price = float(tick.ask if is_buy else tick.bid)
            lots = self._units_to_lots(info, request.qty)

            req: dict = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": lots,
                "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price": price,
                "deviation": 20,
                "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
                "type_filling": self._filling(info),
                "comment": "ai-trading-app",
            }
            if request.stop_loss is not None:
                req["sl"] = float(request.stop_loss)
            if request.take_profit is not None:
                req["tp"] = float(request.take_profit)

            log.info("submitting MT5 order", extra={"symbol": sym, "side": request.side.value,
                                                    "lots": lots, "paper": self._paper})
            result = mt5.order_send(req)
            if result is None:
                return OrderResult(status=OrderStatus.ERROR, error=f"order_send returned None: {mt5.last_error()}")

            done = result.retcode == mt5.TRADE_RETCODE_DONE
            return OrderResult(
                broker_order_id=str(getattr(result, "order", "") or getattr(result, "deal", "")),
                status=OrderStatus.FILLED if done else OrderStatus.REJECTED,
                filled_qty=float(getattr(result, "volume", lots)) if done else 0.0,
                avg_fill_price=float(getattr(result, "price", price)) if done else None,
                raw={"retcode": result.retcode, "comment": getattr(result, "comment", "")},
                error=None if done else getattr(result, "comment", f"retcode={result.retcode}"),
            )
        except Exception as exc:
            log.exception("MT5 submit_order error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def get_open_positions(self) -> list[PositionView]:
        mt5 = self._mt5
        positions = mt5.positions_get() or []
        views: list[PositionView] = []
        for i, p in enumerate(positions):
            is_long = p.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
            info = mt5.symbol_info(p.symbol)
            contract = float(getattr(info, "trade_contract_size", 1) or 1) if info else 1.0
            views.append(PositionView(
                id=i + 1, symbol=p.symbol, asset_class=AssetClass.FOREX.value,
                direction="long" if is_long else "short",
                qty=round(float(p.volume) * contract, 2),  # lots -> units, to match our sizing
                entry_price=float(p.price_open),
                stop_loss=float(p.sl) or None, take_profit=float(p.tp) or None,
                status="open", last_price=float(p.price_current),
                unrealized_pnl=float(p.profit),
            ))
        return views

    def close_position(self, symbol: str) -> OrderResult:
        mt5 = self._mt5
        sym = self._resolve_symbol(symbol)
        positions = [p for p in (mt5.positions_get(symbol=sym) or [])]
        if not positions:
            return OrderResult(status=OrderStatus.REJECTED, error=f"no open MT5 position for {sym}")
        last: OrderResult | None = None
        for p in positions:
            is_long = p.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
            tick = mt5.symbol_info_tick(sym)
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(p.volume),
                "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": float(tick.bid if is_long else tick.ask),
                "deviation": 20, "type_filling": self._filling(mt5.symbol_info(sym)),
                "comment": "ai-trading-app close",
            }
            result = mt5.order_send(req)
            done = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            last = OrderResult(status=OrderStatus.SUBMITTED if done else OrderStatus.ERROR,
                               raw={"retcode": getattr(result, "retcode", None)},
                               error=None if done else "close failed")
        return last or OrderResult(status=OrderStatus.ERROR, error="no close result")

    def set_sl_tp(self, symbol: str, stop_loss: float | None = None,
                  take_profit: float | None = None) -> OrderResult:
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(symbol)
            positions = mt5.positions_get(symbol=sym) or []
            if not positions:
                return OrderResult(status=OrderStatus.REJECTED, error=f"no open MT5 position for {sym}")
            last: OrderResult | None = None
            for p in positions:
                req = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": sym,
                    "position": p.ticket,
                    # 0.0 clears the level; keep the existing one when not provided.
                    "sl": float(stop_loss) if stop_loss is not None else float(getattr(p, "sl", 0) or 0),
                    "tp": float(take_profit) if take_profit is not None else float(getattr(p, "tp", 0) or 0),
                }
                result = mt5.order_send(req)
                done = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
                last = OrderResult(
                    status=OrderStatus.SUBMITTED if done else OrderStatus.ERROR,
                    raw={"retcode": getattr(result, "retcode", None)},
                    error=None if done else (getattr(result, "comment", None) or "set SL/TP failed"),
                )
            return last or OrderResult(status=OrderStatus.ERROR, error="no result")
        except Exception as exc:
            log.exception("MT5 set_sl_tp error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def close_all_positions(self) -> list[OrderResult]:
        symbols = {p.symbol for p in (self._mt5.positions_get() or [])}
        return [self.close_position(s) for s in symbols]

    def get_realized_pnl(self, since) -> float | None:
        """Sum profit + swap + commission of deals closed since ``since`` (terminal truth)."""
        deals = self._mt5.history_deals_get(since, datetime.now(timezone.utc))
        if deals is None:
            return 0.0
        total = 0.0
        for d in deals:
            total += float(getattr(d, "profit", 0) or 0)
            total += float(getattr(d, "swap", 0) or 0)
            total += float(getattr(d, "commission", 0) or 0)
        return round(total, 2)

    # ---- symbol discovery ----

    # Keywords matched (case-insensitive) against the symbol's MT5 path to group by asset class.
    _PATH_KEYWORDS = {
        AssetClass.FOREX: ("forex", "currenc", "fx"),
        AssetClass.METAL: ("metal", "xau", "xag", "gold", "silver"),
        AssetClass.CRYPTO: ("crypto", "coin"),
        AssetClass.STOCK: ("stock", "share", "equit"),
    }

    def list_symbols(self, asset_class: AssetClass | None = None) -> list[str]:
        all_syms = self._mt5.symbols_get() or []
        keywords = self._PATH_KEYWORDS.get(asset_class) if asset_class else None

        def matches(s) -> bool:
            if keywords is None:
                return True
            path = (getattr(s, "path", "") or "").lower()
            name = (getattr(s, "name", "") or "").lower()
            return any(k in path or k in name for k in keywords)

        # Visible (Market Watch) symbols first, then the rest; capped for a usable dropdown.
        selected = sorted(s.name for s in all_syms if matches(s) and getattr(s, "visible", False))
        others = sorted(s.name for s in all_syms if matches(s) and not getattr(s, "visible", False))
        ordered = list(dict.fromkeys(selected + others))
        return ordered[:300]
