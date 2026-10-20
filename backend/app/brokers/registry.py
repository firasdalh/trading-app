"""Broker + data-provider registry.

Resolves the right adapter for an asset class from ``AppSettings.broker_map`` (e.g.
{"stock": "alpaca", "crypto": "alpaca", "forex": "oanda", ...}).

A mapped broker that is NOT IMPLEMENTED falls back to the SimPaperBroker (a config typo, nothing
real behind it). A mapped broker that is implemented but FAILS TO CONNECT (MT5 terminal not
running yet, missing keys) gets an ``UnavailableBroker`` instead: every read raises BrokerError and
every order/close/modify returns ERROR, so all callers fail closed. It used to get the simulator,
silently - and the monitor then priced the REAL account's open positions on the simulator's
synthetic quotes and "closed" them, and current_equity() reported the simulator's $100,000 as the
account balance (so the daily-loss breaker would not trip until a $10,000 loss on a $1,290 account).
The real connection is retried every ``_RETRY_SECONDS``, so the app recovers on its own once the
terminal is up.

Adapter instances are cached per process so the sim broker keeps its positions.
"""
from __future__ import annotations

import threading
import time

from app.brokers.alpaca import AlpacaBrokerAdapter
from app.brokers.base import BrokerAdapter, BrokerError
from app.brokers.ccxt_adapter import CcxtBrokerAdapter
from app.brokers.mt5_adapter import Mt5BrokerAdapter
from app.brokers.oanda import OandaBrokerAdapter
from app.brokers.sim import SimPaperBroker
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.market import AlpacaDataProvider, MarketDataProvider, SyntheticDataProvider
from app.models.enums import AssetClass, OrderStatus
from app.models.schemas import OrderResult

log = get_logger("broker.registry")

_IMPLEMENTED = {"sim", "alpaca", "ccxt", "oanda", "mt5"}
_RETRY_SECONDS = 30.0

_cache: dict[str, BrokerAdapter] = {}
_unavailable: dict[str, tuple[float, "UnavailableBroker"]] = {}   # name -> (failed_at, stand-in)
_build_lock = threading.Lock()
_data_provider: MarketDataProvider | None = None


class UnavailableBroker(BrokerAdapter):
    """Stand-in for a mapped broker that could not connect. It never invents data: reads raise,
    writes return ERROR, the market reads as closed and it never reconciles positions away."""

    reconciles_positions = False

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def _err(self) -> BrokerError:
        return BrokerError(f"{self.name} unavailable: {self.reason}")

    def _fail(self) -> OrderResult:
        return OrderResult(status=OrderStatus.ERROR, error=f"{self.name} unavailable: {self.reason}")

    @property
    def is_paper(self) -> bool:
        return False            # unknown account -> the strictest gates apply

    def get_account(self):
        raise self._err()

    def get_quote(self, symbol: str):
        raise self._err()

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200):
        raise self._err()

    def get_open_positions(self):
        raise self._err()

    def submit_order(self, request) -> OrderResult:
        return self._fail()

    def close_position(self, symbol: str, direction: str | None = None) -> OrderResult:
        return self._fail()

    def close_all_positions(self) -> list[OrderResult]:
        return [self._fail()]

    def set_sl_tp(self, symbol: str, stop_loss: float | None = None,
                  take_profit: float | None = None, direction: str | None = None) -> OrderResult:
        return self._fail()

    def close_partial(self, symbol: str, fraction: float, direction: str | None = None) -> OrderResult:
        return self._fail()

    def can_open(self, symbol: str, direction: str) -> tuple[bool, str | None]:
        return False, f"{self.name} unavailable"

    def market_open(self, symbol: str) -> bool:
        return False

    def reconcile(self) -> dict:
        raise self._err()


def _build(name: str) -> BrokerAdapter:
    cfg = get_settings()
    paper = cfg.broker_env != "live"
    if name == "alpaca":
        return AlpacaBrokerAdapter(paper=paper)
    if name == "ccxt":
        return CcxtBrokerAdapter(paper=paper)
    if name == "oanda":
        return OandaBrokerAdapter()
    if name == "mt5":
        return Mt5BrokerAdapter()
    # default / fallback
    return SimPaperBroker(data=get_data_provider())


def _configured(name: str) -> bool:
    """Whether the user has actually SET UP this broker, so a failure to build it means a real
    account is out of reach (fail closed) rather than "not configured yet" (a fresh install maps
    stocks to Alpaca and forex to OANDA with no keys, and runs on the simulator until they are set).
    MT5 needs no keys - it attaches to the running terminal - so a mapped MT5 is always set up."""
    cfg = get_settings()
    if name == "mt5":
        return True
    if name == "alpaca":
        return bool(cfg.alpaca_api_key and cfg.alpaca_api_secret)
    if name == "oanda":
        return bool(cfg.oanda_api_key and cfg.oanda_account_id)
    if name == "ccxt":
        return bool(cfg.ccxt_api_key and cfg.ccxt_api_secret)
    return False


def get_broker(name: str) -> BrokerAdapter:
    """Return a cached adapter by name. Not implemented, or not configured -> sim. Configured but
    failing to connect -> an UnavailableBroker (never the sim), retried every _RETRY_SECONDS."""
    if name not in _IMPLEMENTED:
        log.warning("broker not implemented; using sim", extra={"requested": name})
        name = "sim"
    if name in _cache:
        return _cache[name]
    down = _unavailable.get(name)
    if down is not None and time.monotonic() - down[0] < _RETRY_SECONDS:
        return down[1]
    # One build at a time: several scheduler threads hitting a down terminal at once would each
    # run MT5's initialize() concurrently, which is exactly what its connection must not see.
    with _build_lock:
        if name in _cache:
            return _cache[name]
        try:
            adapter = _build(name)
        except BrokerError as exc:
            if not _configured(name):
                log.warning("broker not configured; using sim", extra={"requested": name, "error": str(exc)})
                adapter = _cache.get("sim") or _build("sim")
                _cache["sim"] = adapter
                return adapter
            stand_in = UnavailableBroker(name, str(exc))
            _unavailable[name] = (time.monotonic(), stand_in)
            log.error("broker unavailable; refusing to trade until it connects",
                      extra={"requested": name, "error": str(exc)})
            return stand_in
        _unavailable.pop(name, None)
        _cache[name] = adapter
        return adapter


def get_broker_for(asset_class: AssetClass, broker_map: dict[str, str] | None = None) -> BrokerAdapter:
    broker_map = broker_map or {}
    name = broker_map.get(asset_class.value, "sim")
    return get_broker(name)


def get_data_provider() -> MarketDataProvider:
    """Real Alpaca data if keys are set, else the synthetic offline provider."""
    global _data_provider
    if _data_provider is not None:
        return _data_provider
    cfg = get_settings()
    if cfg.alpaca_api_key and cfg.alpaca_api_secret:
        try:
            _data_provider = AlpacaDataProvider(cfg.alpaca_api_key, cfg.alpaca_api_secret)
            log.info("using Alpaca market data")
            return _data_provider
        except Exception as exc:  # pragma: no cover
            log.warning("alpaca data unavailable; using synthetic", extra={"error": str(exc)})
    _data_provider = SyntheticDataProvider()
    log.info("using synthetic market data (offline)")
    return _data_provider


def reset_registry() -> None:
    """Test helper: clear cached adapters/providers."""
    global _data_provider
    _cache.clear()
    _unavailable.clear()
    _data_provider = None
