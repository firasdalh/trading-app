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

    def can_open(self, symbol: str, direction: str) -> tuple[bool, str | None]:
        """Whether a NEW position can be OPENED in this symbol + ``direction`` at the broker right
        now. Returns ``(ok, reason_if_not)``.

        Default: yes — brokers that can't report per-instrument trade permissions assume tradeable.
        MT5 overrides this to honor the symbol's trade_mode (disabled / close-only / long-only /
        short-only), so the risk layer refuses a setup the broker would bounce at order time
        (e.g. Exness has some index CFDs disabled), instead of approving a trade that can't open.
        """
        return True, None

    def market_open(self, symbol: str) -> bool:
        """Whether ``symbol``'s session is OPEN right now (tradeable + live prices).

        Default: True — brokers that can't report session state assume open (the simulator is always
        open; a crypto venue is 24/7). MT5 overrides this to catch a CLOSED session (weekend / holiday
        / out-of-hours) via a stale last-tick, so the auto-traders don't run and the UI can flag that
        the quote is frozen. Advisory only — the executor's order attempt remains the final gate.
        """
        return True

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
        """Units of the underlying per 1.0 LOT, for P&L (price_diff × lots × contract).

        Default 1.0 — for the sim broker 1 lot == 1 unit. MT5 overrides this with the symbol's
        contract size (e.g. 100 oz for gold, 100000 base units for FX) so booked P&L is correct.
        """
        return 1.0

    def lot_constraints(self, symbol: str) -> tuple[float | None, float | None]:
        """The broker's (volume_step, volume_min) for a symbol, so the Risk Manager sizes in the
        broker's real increments and knows the smallest tradable lot. ``(None, None)`` when the
        broker can't report them (the sizer then uses generic defaults). Used to floor a too-small
        risk-budget size UP to the broker minimum (you can't trade smaller) rather than approving a
        sub-minimum size the broker would silently clamp up at order time."""
        return None, None

    def spread(self, symbol: str) -> float | None:
        """Current raw bid/ask spread in PRICE units, or None when the broker can't report it.

        This is the unavoidable round-trip execution cost of a trade: you enter at the far side of
        the book and exit at the near side, so a position starts ``spread`` behind. Compared against
        the stop distance by the Risk Manager's spread gate — a spread that is a large fraction of R
        makes a setup unwinnable no matter how good the signal (natural gas at 0.012 on a 0.029 stop
        is 41% of R gone before the thesis is even tested).

        Returned RAW (not annualised, not normalised): the caller divides by |entry - stop|. Must be
        read LIVE at entry time, never cached across sessions — the same symbol can quote 0.0066 in
        its liquid window and 0.024 at the daily rollover.
        """
        return None

    def risk_per_lot(self, symbol: str, entry: float, stop: float) -> float | None:
        """Account-currency loss of ONE lot if price moves entry->stop — the basis for currency-
        correct sizing (lots = risk budget ÷ risk_per_lot). None when the broker can't compute it;
        the caller then falls back to ``|entry-stop| × contract_size`` (correct when the quote
        currency IS the account currency, e.g. the sim/USD path)."""
        return None

    def position_profit(self, symbol: str, direction: str, lots: float, entry: float,
                        exit_price: float) -> float | None:
        """SIGNED account-currency P&L of ``lots`` of ``direction`` opened at ``entry`` and valued
        at ``exit_price`` (current price for unrealized, fill for realized). Currency-correct when
        the broker can compute it (MT5 ``order_calc_profit`` converts e.g. a JPY/HKD quote into the
        USD account); None otherwise, so the caller falls back to ``±lots × contract × price_diff``
        (correct only when the quote currency IS the account currency, e.g. the sim/USD path)."""
        return None

    def reconcile(self) -> dict:
        """Reconcile local state against the broker on startup. Default: report positions.

        Adapters that can detect orphaned/unknown orders should override this.
        """
        positions = self.get_open_positions()
        return {"broker": self.name, "open_positions": len(positions)}
