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

from app.models.enums import AssetClass, OrderStatus
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

    #: Whether this broker tracks open positions DURABLY (a real account that survives app
    #: restarts, e.g. MT5/Exness). The monitor only treats such a broker's open book as
    #: authoritative when reconciling away app positions the broker no longer holds. The sim
    #: broker keeps positions in memory and forgets them on restart, so it must stay False —
    #: otherwise a restart would wrongly reconcile (close) every open paper position. Other real
    #: brokers (Alpaca/OANDA/ccxt-derivatives) may opt in by setting this True, but only after
    #: verifying their ``get_open_positions`` returns the COMPLETE open book (and doesn't return an
    #: empty list on a transient disconnect, which would wrongly close live positions).
    reconciles_positions: bool = False

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

    def set_sl_tp(self, symbol: str, stop_loss: float | None = None,
                  take_profit: float | None = None) -> OrderResult:
        """Attach/modify the stop-loss and/or take-profit on an existing open position.

        Default: not supported. Brokers that can modify positions (MT5, sim) override this.
        """
        return OrderResult(status=OrderStatus.REJECTED, error="set_sl_tp not supported by this broker")

    def close_partial(self, symbol: str, fraction: float) -> OrderResult:
        """Close ``fraction`` (0..1) of the open position — a partial profit-take; the rest stays
        open. Default: not supported. MT5/sim override it. Brokers that can't partial-close return
        REJECTED, and the caller simply leaves the position whole.
        """
        return OrderResult(status=OrderStatus.REJECTED, error="close_partial not supported by this broker")

    # ---- lifecycle ----

    def get_realized_pnl(self, since) -> float | None:
        """Realized P&L booked at the broker since ``since`` (a tz-aware datetime).

        Default None = the broker can't report it (we fall back to app-tracked realized).
        Brokers with a closed-trade/deal history (MT5) override this so realized P&L is
        accurate even for trades closed directly in the terminal.
        """
        return None

    def list_symbols(self, asset_class: AssetClass | None = None) -> list[str]:
        """Available tradable symbols for an asset class. Default: none (free-text entry).

        Brokers that can enumerate their instruments (MT5, ccxt) override this so the UI can
        offer a dropdown.
        """
        return []

    def describe_symbols(self, asset_class: AssetClass | None = None) -> dict[str, str]:
        """Human-readable name per symbol (e.g. {"EURUSDm": "Euro vs US Dollar"}). Default: none.

        Brokers that expose instrument descriptions (MT5) override this so the UI can show the
        real name next to each ticker.
        """
        return {}

    def get_closed_trades(self, since) -> list[PositionView] | None:
        """Closed trades from the broker's own history since ``since`` (a tz-aware datetime).

        Default None = the broker can't report it (the caller then falls back to app-tracked
        closed positions). MT5 overrides this by reconstructing closed positions from its deal
        history, so the journal matches the broker (Exness) exactly — real fills, profit, and
        close times, including trades closed directly in the terminal.
        """
        return None

    def quote_economics(self, symbol: str, qty_units: float, price: float, is_long: bool) -> dict | None:
        """Cost (margin) and leverage of a hypothetical order, in the account currency.

        Default None = the broker can't compute it (the UI then shows '—'). MT5 overrides this
        with ``order_calc_margin``/``order_calc_profit`` so spend + leverage are exact.
        """
        return None

    def contract_size(self, symbol: str) -> float:
        """Units of the underlying per 1.0 of stored qty, for P&L (price_diff × qty × contract).

        Default 1.0 — qty is already in underlying units (sim shares, ccxt coins). MT5 stores
        qty in LOTS, so it overrides this with the symbol's contract size (e.g. 100 oz for gold,
        100000 base units for FX) — otherwise booked P&L is off by that factor.
        """
        return 1.0

    def reconcile(self) -> dict:
        """Reconcile local state against the broker on startup. Default: report positions.

        Adapters that can detect orphaned/unknown orders should override this.
        """
        positions = self.get_open_positions()
        return {"broker": self.name, "open_positions": len(positions)}
