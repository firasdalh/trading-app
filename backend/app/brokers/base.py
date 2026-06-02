"""The single abstract broker interface every provider must implement.

Asset class -> adapter mapping lives in config / AppSettings.broker_map; the registry
(``app.brokers.registry``) wires concrete adapters to that map. Brokers are swappable
because nothing above this layer imports a concrete adapter directly.

Safety notes:
- ``is_paper`` must be truthful. The execution layer refuses live submission unless the
  app is in a live-confirmed mode.
- ``close_all_positions`` exists so the kill-switch can flatten everything, even mid-cycle.
- Adapters must wrap provider/network calls and surface errors as ``OrderResult(error=...)``
  or raise ``BrokerError`` — they must never leave an order in an unknown state silently.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.enums import AssetClass
from app.models.schemas import (
    AccountState,
    OHLCVSeries,
    OrderRequest,
    OrderResult,
    PositionView,
    Quote,
)


class BrokerError(Exception):
    """Raised for unrecoverable broker errors (auth, config, fatal API failure)."""


class BrokerAdapter(ABC):
    """Abstract broker. One concrete subclass per provider (Alpaca, ccxt, OANDA, sim)."""

    #: Short stable identifier used in config + DB (e.g. "alpaca", "sim", "oanda").
    name: str = "abstract"

    #: Asset classes this adapter can serve.
    supported_asset_classes: tuple[AssetClass, ...] = ()

    @property
    @abstractmethod
    def is_paper(self) -> bool:
        """True if this adapter targets a paper/sandbox environment."""

    # ---- account / data ----

    @abstractmethod
    def get_account(self) -> AccountState:
        """Return current account equity/cash and open-position count."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Latest price for a symbol."""

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        """Historical candles (most recent ``limit``), oldest-first."""

    # ---- orders / positions ----

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order. Must never raise for ordinary rejections — return an
        OrderResult with status REJECTED/ERROR and an ``error`` message instead."""

    @abstractmethod
    def get_open_positions(self) -> list[PositionView]:
        ...

    @abstractmethod
    def close_position(self, symbol: str) -> OrderResult:
        ...

    @abstractmethod
    def close_all_positions(self) -> list[OrderResult]:
        """Flatten everything. Used by the kill-switch."""

    # ---- lifecycle ----

    def reconcile(self) -> dict:
        """Reconcile local state against the broker on startup. Default: report positions.

        Adapters that can detect orphaned/unknown orders should override this.
        """
        positions = self.get_open_positions()
        return {"broker": self.name, "open_positions": len(positions)}
