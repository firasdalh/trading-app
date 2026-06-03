"""Broker + data-provider registry.

Resolves the right adapter for an asset class from ``AppSettings.broker_map`` (e.g.
{"stock": "alpaca", "crypto": "alpaca", "forex": "oanda", ...}). If a mapped broker isn't
available (missing keys, not yet implemented), it falls back to the SimPaperBroker and logs
a warning — the system stays functional and SAFE (sim is always paper) rather than failing.

Adapter instances are cached per process so the sim broker keeps its positions.
"""
from __future__ import annotations

from app.brokers.alpaca import AlpacaBrokerAdapter
from app.brokers.base import BrokerAdapter, BrokerError
from app.brokers.ccxt_adapter import CcxtBrokerAdapter
from app.brokers.mt5_adapter import Mt5BrokerAdapter
from app.brokers.oanda import OandaBrokerAdapter
from app.brokers.sim import SimPaperBroker
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.market import AlpacaDataProvider, MarketDataProvider, SyntheticDataProvider
from app.models.enums import AssetClass

log = get_logger("broker.registry")

_IMPLEMENTED = {"sim", "alpaca", "ccxt", "oanda", "mt5"}

_cache: dict[str, BrokerAdapter] = {}
_data_provider: MarketDataProvider | None = None


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


def get_broker(name: str) -> BrokerAdapter:
    """Return a cached adapter by name, falling back to sim on any problem."""
    if name not in _IMPLEMENTED:
        log.warning("broker not implemented; using sim", extra={"requested": name})
        name = "sim"
    if name in _cache:
        return _cache[name]
    try:
        adapter = _build(name)
    except BrokerError as exc:
        log.warning("broker unavailable; falling back to sim", extra={"requested": name, "error": str(exc)})
        name = "sim"
        adapter = _cache.get("sim") or _build("sim")
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
    _data_provider = None
