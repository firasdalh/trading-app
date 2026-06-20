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

import threading
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


# The MetaTrader5 package talks to the terminal over a SINGLE connection that is NOT safe for
# concurrent use. This app calls it from the scheduler's threads (monitor ~10s, scanner ~20s,
# advisor ~30s, Hybrid ~60s) AND from API request handlers, so without serialization two calls can
# interleave on the one IPC pipe — corrupting replies or wedging the link (lost ticks, a close
# firing against a half-read state). Every terminal call is funnelled through this process-wide
# lock. It is module-level (shared by all Mt5BrokerAdapter instances) because they share the one
# terminal; reentrant so a future method-level wrap can't self-deadlock.
_MT5_LOCK = threading.RLock()


class _SerializedMt5:
    """Proxy over the MetaTrader5 module that runs every terminal CALL under ``_MT5_LOCK``.

    Only callables are wrapped (and the lock is held just for that one call); constant attributes
    (``TIMEFRAME_*``, ``TRADE_ACTION_*``, retcodes, etc.) pass straight through. The wrapped C
    functions never call back into Python, so calls don't nest — the lock is released between
    successive calls, serializing the IPC without otherwise blocking the agents.
    """

    def __init__(self, mt5) -> None:
        # Store in __dict__ directly so __getattr__ (below) never recurses resolving it.
        self.__dict__["_mt5"] = mt5

    def __getattr__(self, name: str):
        attr = getattr(self.__dict__["_mt5"], name)
        if not callable(attr):
            return attr

        def _locked(*args, **kwargs):
            with _MT5_LOCK:
                # Forward positionally when there are no keyword args: MT5's dict-taking calls
                # (order_send / order_check) REJECT an empty kwargs dict with "Unnamed arguments
                # not allowed", which would silently block every order. Passing **{} is not a
                # no-op to these C functions, so only spread kwargs when non-empty.
                return attr(*args, **kwargs) if kwargs else attr(*args)

        return _locked


# MT5 trade-server retcodes surfaced as readable, classifiable errors so callers (e.g. the
# advisor) can distinguish a BENIGN, temporary rejection — market closed, or the stop already at
# the requested level — from a real failure. Values are MT5's fixed TRADE_RETCODE_* numbers.
_RETCODE_MSG = {
    10004: "requote", 10006: "request rejected", 10013: "invalid request",
    10014: "invalid volume", 10015: "invalid price", 10016: "invalid stops",
    10017: "trading disabled", 10018: "market closed", 10019: "not enough money",
    10021: "off quotes (no prices)", 10025: "no changes (already set)",
    10027: "autotrading disabled in terminal",
}


def _retcode_error(result, fallback: str) -> str:
    """Readable error for a non-DONE order_send result: prefer the mapped retcode meaning, then
    the broker comment, else the fallback with the raw code."""
    rc = getattr(result, "retcode", None)
    return _RETCODE_MSG.get(rc) or getattr(result, "comment", None) or f"{fallback} (retcode {rc})"


class Mt5BrokerAdapter(BrokerAdapter):
    name = "mt5"
    supported_asset_classes = (AssetClass.FOREX, AssetClass.METAL, AssetClass.CRYPTO,
                               AssetClass.STOCK, AssetClass.ENERGY, AssetClass.INDEX)
    # Real Exness account: positions persist across app restarts, so its open book is the
    # authoritative source the monitor uses to reconcile away stale app-tracked positions.
    reconciles_positions = True

    def __init__(self) -> None:
        from app.brokers.mt5_credentials import resolve_mt5_credentials

        try:
            import MetaTrader5 as mt5  # lazy; Windows-only
        except ImportError as exc:  # pragma: no cover
            raise BrokerError("MetaTrader5 package not installed (pip install MetaTrader5)") from exc
        # Wrap so every terminal call made through self._mt5 is serialized (single-connection
        # safety). __init__ below uses the raw local `mt5` — it runs once at startup, single-threaded.
        self._mt5 = _SerializedMt5(mt5)

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

    def _clamp_lots(self, info, lots: float) -> float:
        """Clamp a LOT size to the symbol's step/min/max. Qty is in LOTS everywhere now (matching
        what MT5 accepts), so there is no units->lots conversion here."""
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        vmin = float(getattr(info, "volume_min", step) or step)
        vmax = float(getattr(info, "volume_max", 1e9) or 1e9)
        # Floor to the step (never round risk up); +1e-9 first so a float artifact like
        # 0.6/0.1 == 5.999999999999999 doesn't drop a whole step (-> 0.5 instead of 0.6).
        lots = (int(lots / step + 1e-9) * step) if step else lots
        lots = max(vmin, min(vmax, lots))
        ndp = len(str(step).split(".")[1]) if "." in str(step) else 0
        return round(lots, ndp or 2)

    def risk_per_lot(self, symbol: str, entry: float, stop: float) -> float | None:
        """Account-currency loss of 1 lot moving entry->stop, via order_calc_profit (MT5 converts
        FX, so a JPY pair / HKD index is reported in the USD account). None on failure."""
        mt5 = self._mt5
        try:
            if not entry or not stop or entry == stop:
                return None
            sym = self._resolve_symbol(symbol)
            order_type = getattr(mt5, "ORDER_TYPE_BUY", 0)
            profit = mt5.order_calc_profit(order_type, sym, 1.0, float(entry), float(stop))
            if profit is None:
                return None
            loss = abs(float(profit))
            return loss if loss > 0 else None
        except Exception:  # noqa: BLE001 - best-effort; caller falls back to price-distance sizing
            return None

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
            lots = self._clamp_lots(info, request.qty)   # request.qty is in LOTS

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
            views.append(PositionView(
                id=i + 1, symbol=p.symbol, asset_class=AssetClass.FOREX.value,
                direction="long" if is_long else "short",
                qty=round(float(p.volume), 2),  # LOTS (qty is in lots everywhere now)
                entry_price=float(p.price_open),
                stop_loss=float(p.sl) or None, take_profit=float(p.tp) or None,
                status="open", last_price=float(p.price_current),
                unrealized_pnl=float(p.profit),
                cost_usd=self._position_margin(p),
            ))
        return views

    def _position_margin(self, p) -> float | None:
        """Margin to hold this position in the account currency (USD) — the 'cost to open'.

        Uses ``order_calc_margin`` so MT5 does all the currency conversion (e.g. an HKD-quoted
        index back to the USD account). ``p.volume`` is in LOTS (what MT5 expects). Never raises.
        """
        mt5 = self._mt5
        try:
            order_type = (
                getattr(mt5, "ORDER_TYPE_BUY", 0)
                if p.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
                else getattr(mt5, "ORDER_TYPE_SELL", 1)
            )
            margin = mt5.order_calc_margin(order_type, p.symbol, float(p.volume), float(p.price_open))
            return round(float(margin), 2) if margin is not None else None
        except Exception:  # noqa: BLE001 - margin is best-effort; never break the positions list
            return None

    def quote_economics(self, symbol: str, lots: float, price: float, is_long: bool) -> dict | None:
        """Cost/leverage of a HYPOTHETICAL order of ``lots`` at ``price`` (pre-trade).

        Returns {lots, margin_usd, notional_usd, leverage}. MT5 does all currency conversion
        (``order_calc_margin``/``order_calc_profit``), so an HKD index is reported in the USD
        account currency. Never raises.
        """
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(symbol)
            info = self._symbol_info(sym)
            lots = self._clamp_lots(info, lots)
            order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if is_long else getattr(mt5, "ORDER_TYPE_SELL", 1)
            margin = mt5.order_calc_margin(order_type, sym, lots, float(price))
            margin = float(margin) if margin is not None else None
            # Notional (account ccy) via the profit of a +1% price move: profit ≈ notional × 1%,
            # so notional ≈ profit / 0.01. MT5 handles the FX conversion inside order_calc_profit.
            notional = None
            if price:
                profit = mt5.order_calc_profit(order_type, sym, lots, float(price), float(price) * 1.01)
                if profit is not None:
                    notional = abs(float(profit)) / 0.01
            leverage = (notional / margin) if (notional and margin) else None
            return {
                "lots": round(lots, 2),
                "margin_usd": round(margin, 2) if margin is not None else None,
                "notional_usd": round(notional, 2) if notional is not None else None,
                "leverage": round(leverage, 1) if leverage else None,
            }
        except Exception:  # noqa: BLE001 - economics are best-effort; never break the flow
            return None

    def get_closed_trades(self, since) -> list[PositionView] | None:
        """Reconstruct CLOSED trades from MT5 deal history so the journal matches Exness exactly.

        A closed position = its IN deal (entry price/time/direction) + OUT deal(s) (exit
        price/time). Realized P&L is profit + swap + commission across the position's deals —
        the true money booked, matching the terminal. Includes trades closed directly in MT5.
        """
        mt5 = self._mt5
        try:
            deals = mt5.history_deals_get(since, datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            return None
        if deals is None:
            return []

        entry_in = getattr(mt5, "DEAL_ENTRY_IN", 0)
        entry_out = getattr(mt5, "DEAL_ENTRY_OUT", 1)
        deal_buy = getattr(mt5, "DEAL_TYPE_BUY", 0)

        groups: dict[int, list] = {}
        for d in deals:
            pid = getattr(d, "position_id", 0)
            if pid:
                groups.setdefault(pid, []).append(d)

        out: list[PositionView] = []
        for pid, ds in groups.items():
            ds.sort(key=lambda x: getattr(x, "time", 0))
            ins = [d for d in ds if getattr(d, "entry", None) == entry_in]
            outs = [d for d in ds if getattr(d, "entry", None) == entry_out]
            if not ins or not outs:
                continue  # position still open or only partially filled — skip
            first_in, last_out = ins[0], outs[-1]
            is_long = getattr(first_in, "type", 0) == deal_buy
            vol = round(sum(float(getattr(d, "volume", 0) or 0) for d in outs), 2)
            net = sum(
                float(getattr(d, "profit", 0) or 0)
                + float(getattr(d, "swap", 0) or 0)
                + float(getattr(d, "commission", 0) or 0)
                for d in ds
            )
            close_ts = int(getattr(last_out, "time", 0) or 0)
            out.append(PositionView(
                id=pid,
                symbol=getattr(first_in, "symbol", ""),
                asset_class=AssetClass.FOREX.value,  # not surfaced for closed trades
                direction="long" if is_long else "short",
                qty=vol,
                entry_price=float(getattr(first_in, "price", 0) or 0),
                status="closed",
                last_price=float(getattr(last_out, "price", 0) or 0),  # shown as Exit
                unrealized_pnl=0.0,
                realized_pnl=round(net, 2),
                closed_at=datetime.fromtimestamp(close_ts, tz=timezone.utc) if close_ts else None,
            ))
        out.sort(key=lambda v: v.closed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

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

    def close_partial(self, symbol: str, fraction: float) -> OrderResult:
        """Close ``fraction`` of the open position (partial profit-take). The remainder keeps the
        same ticket and stops. Volume is floored to the lot step; a position already at the broker
        minimum can't be split, so we reject (the caller then leaves it whole)."""
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(symbol)
            positions = [p for p in (mt5.positions_get(symbol=sym) or [])]
            if not positions:
                return OrderResult(status=OrderStatus.REJECTED, error=f"no open MT5 position for {sym}")
            info = mt5.symbol_info(sym)
            step = float(getattr(info, "volume_step", 0.01) or 0.01)
            vmin = float(getattr(info, "volume_min", step) or step)
            last: OrderResult | None = None
            for p in positions:
                part = (int((p.volume * fraction) / step) * step) if step else p.volume * fraction
                # Round to the lot STEP's precision (0.01 -> 2dp, 0.001 -> 3dp), not a fixed 2dp,
                # so fine-lot instruments aren't snapped to an invalid/zero volume.
                s = f"{step:.10f}".rstrip("0")
                ndp = len(s.split(".")[1]) if "." in s else 0
                part = round(max(vmin, min(part, p.volume)), ndp)
                if part >= p.volume:  # can't take a partial without dropping below the min lot
                    return OrderResult(status=OrderStatus.REJECTED,
                                       error="position too small to partial-close (min lot)")
                is_long = p.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
                tick = mt5.symbol_info_tick(sym)
                req = {
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(part),
                    "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
                    "position": p.ticket,
                    "price": float(tick.bid if is_long else tick.ask),
                    "deviation": 20, "type_filling": self._filling(info),
                    "comment": "ai-trading-app partial",
                }
                result = mt5.order_send(req)
                done = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
                last = OrderResult(status=OrderStatus.SUBMITTED if done else OrderStatus.ERROR,
                                   filled_qty=float(part) if done else 0.0,
                                   raw={"retcode": getattr(result, "retcode", None)},
                                   error=None if done else "partial close failed")
            return last or OrderResult(status=OrderStatus.ERROR, error="no partial-close result")
        except Exception as exc:  # noqa: BLE001
            log.exception("MT5 close_partial error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def set_sl_tp(self, symbol: str, stop_loss: float | None = None,
                  take_profit: float | None = None) -> OrderResult:
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(symbol)
            positions = mt5.positions_get(symbol=sym) or []
            if not positions:
                return OrderResult(status=OrderStatus.REJECTED, error=f"no open MT5 position for {sym}")

            # Normalize prices to the symbol's tick precision — MT5 rejects ("Invalid stops")
            # levels with more decimals than the instrument allows (e.g. an auto-computed
            # trailing stop of 4473.54231 on XAUUSD, which has 2-3 digits).
            info = mt5.symbol_info(sym)
            digits = getattr(info, "digits", None)
            point = getattr(info, "point", None)
            stops_level = getattr(info, "trade_stops_level", None)
            # MT5 also rejects stops placed CLOSER than this many points to the current price.
            min_dist = (stops_level * point) if (stops_level and point) else None
            try:
                tick = mt5.symbol_info_tick(sym)
            except Exception:  # noqa: BLE001
                tick = None

            def _norm(v: float | None, current) -> float:
                v = float(v) if v is not None else float(current or 0)
                return round(v, digits) if (v and digits is not None) else v

            def _clamp(value: float, p, is_sl: bool) -> float:
                """Push a too-close level just past MT5's minimum stop distance so the modify
                isn't rejected. Only ever moves a level that would otherwise be invalid."""
                if not value or not min_dist or tick is None:
                    return value
                bid, ask = getattr(tick, "bid", None), getattr(tick, "ask", None)
                if bid is None or ask is None:
                    return value
                is_long = getattr(p, "type", None) == mt5.POSITION_TYPE_BUY
                # Stop sits opposite the trade direction; target sits with it.
                below = (is_long and is_sl) or (not is_long and not is_sl)
                if below:  # must be at least min_dist BELOW the close price
                    limit = bid - min_dist
                    value = min(value, limit)
                else:      # must be at least min_dist ABOVE the close price
                    limit = ask + min_dist
                    value = max(value, limit)
                return round(value, digits) if digits is not None else value

            last: OrderResult | None = None
            for p in positions:
                req = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "symbol": sym,
                    "position": p.ticket,
                    # 0.0 clears the level; keep the existing one when not provided.
                    "sl": _clamp(_norm(stop_loss, getattr(p, "sl", 0)), p, is_sl=True),
                    "tp": _clamp(_norm(take_profit, getattr(p, "tp", 0)), p, is_sl=False),
                }
                result = mt5.order_send(req)
                done = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
                last = OrderResult(
                    status=OrderStatus.SUBMITTED if done else OrderStatus.ERROR,
                    raw={"retcode": getattr(result, "retcode", None)},
                    error=None if done else _retcode_error(result, "set SL/TP failed"),
                )
            return last or OrderResult(status=OrderStatus.ERROR, error="no result")
        except Exception as exc:
            log.exception("MT5 set_sl_tp error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def close_all_positions(self) -> list[OrderResult]:
        symbols = {p.symbol for p in (self._mt5.positions_get() or [])}
        return [self.close_position(s) for s in symbols]

    def can_open(self, symbol: str, direction: str) -> tuple[bool, str | None]:
        """Whether a NEW position can be OPENED in this symbol + direction, per the instrument's
        MT5 trade_mode. Refuses a disabled / close-only symbol, and the wrong side of a long-only /
        short-only one — so the risk layer doesn't approve a trade the terminal would bounce with
        'Trade disabled' (e.g. Exness has India 50 disabled). (trade_mode: 0 DISABLED, 1 LONGONLY,
        2 SHORTONLY, 3 CLOSEONLY, 4 FULL.)"""
        try:
            mode = getattr(self._symbol_info(self._resolve_symbol(symbol)), "trade_mode", None)
        except Exception:  # noqa: BLE001 - on a lookup failure, don't block; the executor is the final gate
            return True, None
        if mode is None or mode == 4:               # FULL (or unknown) -> allow
            return True, None
        if mode == 0:                               # DISABLED
            return False, "broker has this instrument disabled"
        if mode == 3:                               # CLOSEONLY
            return False, "broker allows close-only on this instrument"
        if mode == 1 and direction == "short":      # LONGONLY
            return False, "broker allows long-only on this instrument"
        if mode == 2 and direction == "long":       # SHORTONLY
            return False, "broker allows short-only on this instrument"
        return True, None

    def contract_size(self, symbol: str) -> float:
        """The symbol's MT5 contract size (qty is stored in lots, so P&L must scale by this)."""
        try:
            info = self._mt5.symbol_info(self._resolve_symbol(symbol))
            return float(getattr(info, "trade_contract_size", 1) or 1)
        except Exception:  # noqa: BLE001
            return 1.0

    def get_realized_pnl(self, since) -> float | None:
        """Sum profit + swap + commission of TRADE deals closed since ``since`` (terminal truth).

        Excludes non-trade BALANCE operations — deposits, withdrawals, credit/bonus changes,
        corrections — which carry a P&L figure but no symbol (and no position_id). Counting them
        would distort realized trading P&L AND can falsely trip the daily-loss circuit breaker:
        a withdrawal would look like a trading loss and pause trading. (Trade deals always carry a
        symbol; balance ops never do.)
        """
        deals = self._mt5.history_deals_get(since, datetime.now(timezone.utc))
        if deals is None:
            return 0.0
        total = 0.0
        for d in deals:
            if not getattr(d, "symbol", ""):  # balance / credit / correction / charge — not trading
                continue
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
        AssetClass.ENERGY: ("energ", "oil", "brent", "wti", "ngas", "gas",
                            "xbr", "xti", "xng"),
        AssetClass.INDEX: ("indices", "index", "us500", "us30", "us2000", "ustec",
                           "nas100", "ndx", "spx", "dow", "ger30", "ger40", "de30",
                           "de40", "dax", "uk100", "ftse", "fra40", "fr40", "cac",
                           "jp225", "jpn225", "nikkei", "aus200", "hk50", "hsi",
                           "stoxx", "euro50", "es35", "nifty"),
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

    def describe_symbols(self, asset_class: AssetClass | None = None) -> dict[str, str]:
        """The MT5 instrument description for each symbol in this asset class (e.g. 'Apple Inc')."""
        all_syms = self._mt5.symbols_get() or []
        keywords = self._PATH_KEYWORDS.get(asset_class) if asset_class else None
        out: dict[str, str] = {}
        for s in all_syms:
            name = getattr(s, "name", "")
            if keywords is not None:
                path = (getattr(s, "path", "") or "").lower()
                if not any(k in path or k in name.lower() for k in keywords):
                    continue
            desc = (getattr(s, "description", "") or "").strip()
            if name and desc:
                out[name] = desc
        return out
