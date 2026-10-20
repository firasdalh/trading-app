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
import time
from datetime import datetime, timezone

from app.brokers.base import BrokerAdapter, BrokerError
from app.core.logging import get_logger
from app.models.enums import AssetClass, OrderSide, OrderStatus
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

# No new tick for this many seconds = the session is CLOSED (weekend / holiday / out-of-hours). Well
# above any liquid symbol's tick gap in-session, well below a weekend, so a live market never trips it.
_MARKET_STALE_SECONDS = 600
_PAPER_RECHECK_S = 5.0      # how stale the demo/real reading may be (see is_paper)
_CLOSED_MEMORY_S = 1800.0   # how long a 'market closed' refusal marks the symbol closed
_CLOSED_REFUSALS: dict[str, float] = {}   # symbol -> when the broker last said 'market closed'


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

        with _MT5_LOCK:                 # never initialize while another thread is mid-call
            ok = mt5.initialize(**kwargs)
        if not ok:
            err = mt5.last_error()
            raise BrokerError(f"MT5 initialize failed: {err}. Is the terminal running and logged in?")

        with _MT5_LOCK:
            info = mt5.account_info()
        if info is None:
            raise BrokerError("MT5 connected but no account info — check terminal login")
        # trade_mode: 0 DEMO, 1 CONTEST, 2 REAL
        self._paper = getattr(info, "trade_mode", 2) != getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        self._paper_checked = time.monotonic()
        self._login = getattr(info, "login", None)
        log.info("MT5 connected", extra={"login": self._login,
                                         "server": getattr(info, "server", None), "paper": self._paper})
        try:
            self._bind_account(info)
        except Exception as exc:  # noqa: BLE001 - never let bookkeeping stop the connection
            log.warning("MT5 account bookkeeping failed", extra={"error": str(exc)})

    def _bind_account(self, info) -> None:
        """Remember which account the app trades, across restarts, and handle a change of account.

        The open app trades, today's loss baseline and the journal all belong to ONE account. A
        different login used to be adopted silently: the old account's open rows went missing from
        the book and were phantom-closed, and today's daily-loss limit stayed computed from the
        demo's equity (full review 2026-10-20, API-3; real-money audit 2026-10-21).
          - same account (or the first one seen): just remembered;
          - a new account while app trades of the old one are still OPEN: refused - every order,
            close and stop change says why, until the terminal is back on the old account or those
            trades are closed;
          - a new account with nothing open: adopted, and today's loss baseline restarts from ITS
            equity (an audit row records the switch).
        """
        from sqlalchemy import func, or_, select

        from app.core import database as _db
        from app.core.state import get_or_create_daily_state
        from app.models.db import AgentRun, AppSettings, Position

        login = getattr(info, "login", None)
        if login is None:
            return
        with _db.SessionLocal() as s:
            st = s.get(AppSettings, 1)
            if st is None:
                return
            prev = st.mt5_account_login
            if prev is None or prev == login:
                if prev is None:
                    st.mt5_account_login = login
                    s.commit()
                return
            open_n = s.scalar(select(func.count()).select_from(Position).where(
                Position.status == "open", or_(Position.broker == "mt5", Position.broker.is_(None)))) or 0
            if open_n:
                self._login = prev
                self._bind_block = (
                    f"the MT5 terminal is on account #{login}, but {open_n} open app trade(s) belong to "
                    f"account #{prev}. Put the terminal back on #{prev} and close them (or let them "
                    "finish) before switching accounts")
                s.add(AgentRun(agent="broker", event="account_switch_blocked", level="ERROR",
                               detail={"from": prev, "to": login, "open_trades": open_n}))
                s.commit()
                log.error("MT5 account switch refused: open trades on the old account",
                          extra={"from": prev, "to": login, "open": open_n})
                return
            st.mt5_account_login = login
            daily = get_or_create_daily_state(s)
            old_base = daily.starting_equity
            daily.starting_equity = float(getattr(info, "equity", 0.0) or 0.0) or None
            s.add(AgentRun(agent="broker", event="account_switched", level="WARNING",
                           detail={"from": prev, "to": login, "paper": self._paper,
                                   "server": getattr(info, "server", None),
                                   "daily_baseline_before": old_base,
                                   "daily_baseline_now": daily.starting_equity}))
            s.commit()
            log.warning("MT5 account switched; today's loss baseline re-based",
                        extra={"from": prev, "to": login, "equity": daily.starting_equity})

    @property
    def is_paper(self) -> bool:
        """Whether the terminal is on a DEMO account RIGHT NOW (re-read at most every 5 s).

        It used to be read once at connect. Exness puts a demo and a real account in the same
        terminal, and switching the terminal to the real one left this saying "paper" for the life
        of the process - so every real-money gate (live confirmation, paper-only Mode B, paper-only
        auto-trade) waved real orders through (full review 2026-10-20). An unreadable account
        counts as REAL: the strict gates apply until it can be read.
        """
        now = time.monotonic()
        if now - getattr(self, "_paper_checked", 0.0) < _PAPER_RECHECK_S:
            return self._paper
        try:
            info = self._mt5.account_info()
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            return False
        self._paper = getattr(info, "trade_mode", 2) != getattr(self._mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        self._paper_checked = now
        return self._paper

    def _account_switched(self) -> str | None:
        """A reason to refuse, when the terminal is now logged into a different account than the one
        this process connected to (its book, equity and risk state all belong to the old one)."""
        try:
            info = self._mt5.account_info()
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            return "MT5 account unreadable - not sending"
        login = getattr(info, "login", None)
        start = getattr(self, "_login", None)
        if start is not None and login is not None and login != start:
            return getattr(self, "_bind_block", None) or (
                f"the MT5 terminal is now on account #{login}, not #{start} that the app connected to - "
                "reconnect in Settings > MT5 (or restart the app) before trading")
        return None

    def account_block(self) -> str | None:
        """Why this account may not be traded right now (None = fine). For the status display."""
        return self._account_switched()

    # ---- helpers ----

    def _tf(self, timeframe: str):
        attr = _TF_ATTR.get(timeframe)
        if attr is None:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        return getattr(self._mt5, attr)

    def _resolve_symbol(self, symbol: str) -> str:
        """Map a symbol to the broker's actual name and select it in Market Watch.

        Order matters: try the EXACT name first — ``list_symbols()`` returns real broker names
        (e.g. ``US30_x10m``, ``XAUUSD.z``) that ``normalize_symbol`` would MANGLE by stripping
        ``_`` / ``-``. Only when the exact name misses do we normalize a generic user symbol
        (EUR/USD -> EURUSD) and search the broker list for the closest suffixed match. Cached per
        adapter, keyed by the input symbol.
        """
        cache = getattr(self, "_symbol_cache", None)
        if cache is None:
            cache = {}
            self._symbol_cache = cache
        if symbol in cache:
            return cache[symbol]

        name = None
        if self._mt5.symbol_info(symbol) is not None:          # 1) exact broker name — wins
            name = symbol
        else:
            sym = normalize_symbol(symbol)                     # 2) generic name -> normalized
            if self._mt5.symbol_info(sym) is not None:
                name = sym
            else:                                              # 3) closest suffixed/base match
                all_syms = self._mt5.symbols_get() or []
                candidates = [
                    s.name for s in all_syms
                    if s.name.upper().replace(".", "").replace("-", "").replace("_", "").startswith(sym)
                ]
                if candidates:
                    name = sorted(candidates, key=len)[0]      # closest to the base name
        if name is None:
            raise BrokerError(f"symbol {symbol} not found on this MT5 account")
        self._mt5.symbol_select(name, True)
        cache[symbol] = name
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

    def _last_error(self):
        try:
            return self._mt5.last_error()
        except Exception:  # noqa: BLE001 - only used to enrich an error message
            return None

    def _positions(self, **kwargs):
        """``positions_get`` that RAISES when the terminal call fails. MT5 returns None on an error
        (IPC down, terminal waking from sleep) and an empty tuple when nothing is open — treating
        None as "nothing open" once made the monitor mark every open trade CLOSED (19 phantom
        closes) and let the openers see free room. An unknown book must never look empty."""
        # Every reader AND every close / partial / stop change goes through here, so this one check
        # keeps all of them off an account the app did not connect to: after a switch in the
        # terminal, closing "the UK100 long" by symbol could close a REAL ticket for a demo row, and
        # the reconciler would phantom-close the old account's rows (real-money audit 2026-10-21).
        switched = self._account_switched() if getattr(self, "_login", None) is not None else None
        if switched:
            raise BrokerError(switched)
        positions = self._mt5.positions_get(**kwargs)
        if positions is None:
            raise BrokerError(f"MT5 positions_get failed: {self._last_error()}")
        return positions

    def get_account(self) -> AccountState:
        info = self._mt5.account_info()
        if info is None:
            raise BrokerError("MT5 account_info unavailable")
        positions = self._positions()
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

    def spread(self, symbol: str) -> float | None:
        """Live ask-bid in price units from the current tick. None when either side is missing or
        the spread is non-positive (a crossed/stale book) so the gate fails OPEN rather than
        blocking every trade on a bad read."""
        try:
            name = self._resolve_symbol(symbol)
            tick = self._mt5.symbol_info_tick(name)
            bid = float(getattr(tick, "bid", 0) or 0) if tick is not None else 0.0
            ask = float(getattr(tick, "ask", 0) or 0) if tick is not None else 0.0
            if not (bid and ask):
                info = self._mt5.symbol_info(name)
                if info is not None:
                    bid = float(getattr(info, "bid", 0) or 0)
                    ask = float(getattr(info, "ask", 0) or 0)
            if not (bid and ask):
                return None
            sp = ask - bid
            return sp if sp > 0 else None
        except Exception:  # noqa: BLE001 - best-effort; a failed read must not block trading
            return None

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

    def position_profit(self, symbol: str, direction: str, lots: float, entry: float,
                        exit_price: float) -> float | None:
        """Signed account-currency P&L via order_calc_profit with the position's order type (MT5
        converts the quote ccy, so a JPY pair / HKD index is reported in the USD account). The
        SELL order type makes a price drop a profit, so the sign is already correct. None on
        failure -> the monitor falls back to naive price-distance × contract scaling."""
        mt5 = self._mt5
        try:
            if not lots or not entry or not exit_price:
                return None
            sym = self._resolve_symbol(symbol)
            order_type = (getattr(mt5, "ORDER_TYPE_BUY", 0) if direction == "long"
                          else getattr(mt5, "ORDER_TYPE_SELL", 1))
            profit = mt5.order_calc_profit(order_type, sym, float(lots), float(entry), float(exit_price))
            return float(profit) if profit is not None else None
        except Exception:  # noqa: BLE001 - best-effort; caller falls back to naive P&L
            return None

    def _asset_class_for(self, symbol: str, info=None) -> str:
        """Best-effort asset class for a broker symbol, from its MT5 path/name (the same keyword map
        used for symbol discovery), so open/closed positions aren't all mislabelled 'forex'. Falls
        back to forex when nothing matches or the lookup fails. Cached per symbol."""
        cache = getattr(self, "_ac_cache", None)
        if cache is None:
            cache = {}
            self._ac_cache = cache
        key = symbol.upper()
        if key in cache:
            return cache[key]
        path = name = ""
        try:
            si = info if info is not None else self._mt5.symbol_info(symbol)
            if si is not None:
                path = (getattr(si, "path", "") or "").lower()
                name = (getattr(si, "name", "") or "").lower()
        except Exception:  # noqa: BLE001
            pass
        ac = AssetClass.FOREX.value  # default/fallback
        for asset_class, keywords in self._PATH_KEYWORDS.items():
            if asset_class == AssetClass.FOREX:
                continue  # checked implicitly as the default, so a stray 'fx' can't grab a CFD
            if any(k in path or k in name for k in keywords):
                ac = asset_class.value
                break
        cache[key] = ac
        return ac

    def _filling(self, info):
        mt5 = self._mt5
        mode = getattr(info, "filling_mode", 0)
        # filling_mode is a bitmask; prefer FOK, then IOC, else RETURN.
        if mode & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            return mt5.ORDER_FILLING_FOK
        if mode & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            return mt5.ORDER_FILLING_IOC
        return getattr(mt5, "ORDER_FILLING_RETURN", 2)

    def algo_trading_allowed(self) -> tuple[bool, str | None]:
        """Will MT5 accept an automated order right now? Returns (allowed, reason-if-not).

        Two independent switches can block it, and the connection stays perfectly healthy either way:
        the terminal's "Algo Trading" toolbar button (``terminal_info().trade_allowed``) and the
        account-level expert permission (``account_info().trade_expert``). With either off, every
        order is refused with "AutoTrading disabled by client" -- on 2026-09-09 seven orders were
        refused in two minutes while the status panel said "connected".

        Fails OPEN on a read error: a flaky info call must never block trading on its own.
        """
        mt5 = self._mt5
        try:
            term = mt5.terminal_info()
            if term is not None and not getattr(term, "trade_allowed", True):
                return False, ("MT5 'Algo Trading' is switched OFF in the terminal -- turn on the "
                               "Algo Trading button in the MT5 toolbar")
            acct = mt5.account_info()
            if acct is not None and not getattr(acct, "trade_expert", True):
                return False, "this MT5 account does not permit automated (expert) trading"
        except Exception:  # noqa: BLE001
            return True, None
        return True, None

    def submit_order(self, request: OrderRequest) -> OrderResult:
        mt5 = self._mt5
        # Check before sending: an order MT5 is guaranteed to refuse still costs a round trip and
        # reports a vague "disabled by client". Saying which switch is off tells you what to click.
        allowed, why = self.algo_trading_allowed()
        if not allowed:
            log.warning("order blocked: algo trading disabled", extra={"symbol": request.symbol, "reason": why})
            return OrderResult(status=OrderStatus.REJECTED, error=why)
        switched = self._account_switched()
        if switched:
            log.error("order blocked: account changed", extra={"symbol": request.symbol, "reason": switched})
            return OrderResult(status=OrderStatus.REJECTED, error=switched)
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
            # DONE_PARTIAL (10010) leaves a REAL position at the broker for the filled volume. It was
            # reported as REJECTED with nothing filled, so the app kept no row for a live trade.
            partial = result.retcode == getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)
            if result.retcode == getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018) or                     "closed" in str(getattr(result, "comment", "")).lower():
                _CLOSED_REFUSALS[sym] = time.monotonic()        # see market_open
            filled = done or partial
            return OrderResult(
                broker_order_id=str(getattr(result, "order", "") or getattr(result, "deal", "")),
                status=(OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED if partial
                        else OrderStatus.REJECTED),
                filled_qty=float(getattr(result, "volume", lots)) if filled else 0.0,
                avg_fill_price=float(getattr(result, "price", price)) if filled else None,
                raw={"retcode": result.retcode, "comment": getattr(result, "comment", "")},
                error=None if filled else _retcode_error(result, "order rejected"),
            )
        except Exception as exc:
            log.exception("MT5 submit_order error")
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))

    def get_open_positions(self) -> list[PositionView]:
        mt5 = self._mt5
        positions = self._positions()
        views: list[PositionView] = []
        for i, p in enumerate(positions):
            is_long = p.type == getattr(mt5, "POSITION_TYPE_BUY", 0)
            views.append(PositionView(
                id=i + 1, symbol=p.symbol, asset_class=self._asset_class_for(p.symbol),
                direction="long" if is_long else "short",
                qty=round(float(p.volume), 2),  # LOTS (qty is in lots everywhere now)
                entry_price=float(p.price_open),
                stop_loss=float(p.sl) or None, take_profit=float(p.tp) or None,
                status="open", last_price=float(p.price_current),
                # PRICE P&L only - the UI derives $ per price unit from it (P&L / price move), so
                # swap must not be mixed in. Accrued swap travels separately and the daily-loss
                # breaker adds it (it was invisible until the close).
                unrealized_pnl=float(p.profit),
                swap=float(getattr(p, "swap", 0.0) or 0.0),
                cost_usd=self._position_margin(p),
                # WHEN the position was opened, straight from the broker. Broker-truth positions are
                # the ones the chart draws from, so without this the UI can't mark the entry CANDLE —
                # only the price level. Taking it from MT5 (rather than the app's own row) also means
                # trades opened directly in the terminal get a timestamp too.
                opened_at=self._position_opened_at(p),
            ))
        return views

    @staticmethod
    def _position_opened_at(p) -> datetime | None:
        """Open time as an aware UTC datetime. MT5 reports epoch seconds in ``time`` (and
        ``time_msc`` in milliseconds); prefer the millisecond field when present. None on anything
        unexpected — a missing timestamp only costs a chart marker, so it must never raise."""
        try:
            msc = getattr(p, "time_msc", 0)
            if msc:
                return datetime.fromtimestamp(int(msc) / 1000, tz=timezone.utc)
            secs = getattr(p, "time", 0)
            if secs:
                return datetime.fromtimestamp(int(secs), tz=timezone.utc)
        except Exception:  # noqa: BLE001 - best-effort; never break the position list
            pass
        return None

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
            sym = getattr(first_in, "symbol", "")
            out.append(PositionView(
                id=pid,
                symbol=sym,
                asset_class=self._asset_class_for(sym) if sym else AssetClass.FOREX.value,
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

    def _side(self, positions, direction: str | None) -> list:
        """Only the tickets on ``direction``'s side (None = all). The callers mean ONE trade: acting on
        every ticket of the symbol also closed / re-stopped an opposite hedge or a trade placed from
        the terminal or phone - the DB shows long+short pairs closed in the same microsecond
        (full review 2026-10-20)."""
        positions = list(positions)
        if direction is None:
            return positions
        buy = getattr(self._mt5, "POSITION_TYPE_BUY", 0)
        want_long = direction == "long"
        return [p for p in positions if (p.type == buy) == want_long]

    def close_position(self, symbol: str, direction: str | None = None) -> OrderResult:
        mt5 = self._mt5
        sym = self._resolve_symbol(symbol)
        try:
            positions = self._side(self._positions(symbol=sym), direction)
        except BrokerError as exc:  # a failed read is NOT "already closed" — callers keep it open
            return OrderResult(status=OrderStatus.ERROR, error=str(exc))
        if not positions:
            return OrderResult(status=OrderStatus.REJECTED, error=f"no open MT5 position for {sym}")
        allowed, why = self.algo_trading_allowed()
        if not allowed:
            return OrderResult(status=OrderStatus.ERROR, error=why)
        # Every ticket must close for the result to say "closed". Reporting only the LAST ticket's
        # result once recorded a position as closed while an earlier ticket's close had failed.
        failures: list[str] = []
        codes: list = []
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
            codes.append(getattr(result, "retcode", None))
            if not (result is not None and result.retcode == mt5.TRADE_RETCODE_DONE):
                failures.append(_retcode_error(result, "close failed") if result is not None
                                else f"close failed: {self._last_error()}")
        if failures:
            return OrderResult(status=OrderStatus.ERROR, raw={"retcodes": codes},
                               error="; ".join(dict.fromkeys(failures)))
        return OrderResult(status=OrderStatus.SUBMITTED, raw={"retcodes": codes})

    def close_partial(self, symbol: str, fraction: float, direction: str | None = None) -> OrderResult:
        """Close ``fraction`` of the open position (partial profit-take). The remainder keeps the
        same ticket and stops. Volume is floored to the lot step; a position already at the broker
        minimum can't be split, so we reject (the caller then leaves it whole)."""
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(symbol)
            positions = self._side(self._positions(symbol=sym), direction)   # BrokerError -> ERROR below
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
                  take_profit: float | None = None, direction: str | None = None) -> OrderResult:
        mt5 = self._mt5
        try:
            sym = self._resolve_symbol(symbol)
            positions = self._side(self._positions(symbol=sym), direction)   # BrokerError -> ERROR below
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
        symbols = {p.symbol for p in self._positions()}
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

    def market_open(self, symbol: str) -> bool:
        """Session-open check: the instrument isn't disabled/close-only AND its last tick is RECENT.
        A stale last tick (no new prices for ``_MARKET_STALE_SECONDS``) = the market is closed — a
        weekend / holiday / out-of-hours gap. Crypto (24/7) keeps ticking so it reads open; FX / indices
        / metals / energy go stale over the weekend. Relies on the broker server clock being at or ahead
        of UTC (Exness is), so a live tick is never mis-read as old, while a closed session is hours/days
        stale — far past the threshold. Advisory: the executor's order attempt is still the final gate."""
        name = self._resolve_symbol(symbol)
        # The broker just refused an order here with "market closed": index and oil CFDs keep
        # QUOTING through their daily break, so the tick-age test reads them open and the Hybrid /
        # arms kept walking into "session closed" rejections (18 Hybrid ticks lost, arms consumed;
        # full review 2026-10-20). Trust the refusal for _CLOSED_MEMORY_S.
        closed_at = _CLOSED_REFUSALS.get(name)
        if closed_at is not None and time.monotonic() - closed_at < _CLOSED_MEMORY_S:
            return False
        try:
            mode = getattr(self._symbol_info(name), "trade_mode", 4)
            if mode in (0, 3):                       # DISABLED / CLOSEONLY -> not tradeable now
                return False
            tick = self._mt5.symbol_info_tick(name)
            t = int(getattr(tick, "time", 0)) if tick is not None else 0
            if t <= 0:
                return True                          # no tick time -> can't tell; don't block
            age = datetime.now(timezone.utc).timestamp() - t
            return age <= _MARKET_STALE_SECONDS
        except Exception:  # noqa: BLE001 - on a lookup failure, don't block (executor is the final gate)
            return True

    def contract_size(self, symbol: str) -> float:
        """The symbol's MT5 contract size (qty is stored in lots, so P&L must scale by this)."""
        try:
            info = self._mt5.symbol_info(self._resolve_symbol(symbol))
            return float(getattr(info, "trade_contract_size", 1) or 1)
        except Exception:  # noqa: BLE001
            return 1.0

    def lot_constraints(self, symbol: str) -> tuple[float | None, float | None]:
        """The symbol's (volume_step, volume_min) from MT5 — the broker's real lot increments, so
        the Risk Manager sizes in them and floors up to the true minimum instead of approving a
        sub-min size that order_send would clamp up (silently raising risk)."""
        try:
            info = self._mt5.symbol_info(self._resolve_symbol(symbol))
            if info is None:
                return None, None
            step = float(getattr(info, "volume_step", 0) or 0) or None
            vmin = float(getattr(info, "volume_min", 0) or 0) or None
            return step, vmin
        except Exception:  # noqa: BLE001
            return None, None

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
            # A FAILED history read is not "nothing realized today": returning 0.0 here un-latched
            # the daily-loss breaker mid-outage (2026-09-25). Raise; the breaker keeps its state.
            raise BrokerError(f"MT5 deal history unavailable: {self._last_error()}")
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
