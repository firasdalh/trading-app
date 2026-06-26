"""Glue between the pure Risk Manager and live state (DB + broker).

Keeps the manager itself pure: this layer gathers the account snapshot, active limits,
exposure, and per-pair cooldown, then calls ``evaluate_proposal``. Nothing here weakens a
limit — it only assembles inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.base import BrokerAdapter
from app.brokers.registry import get_broker_for
from app.core.config import get_settings
from app.core.state import get_or_create_daily_state, get_or_create_risk_config, get_or_create_settings
from app.core.logging import get_logger
from app.models.db import Position
from app.models.enums import AssetClass, PositionStatus, RiskDecisionType
from app.models.schemas import AccountState, PositionView, RiskDecision, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal

_log = get_logger("risk.service")


def live_broker_positions(session: Session) -> list[PositionView]:
    """Aggregate the brokers' real open positions across the configured broker map.

    One call per distinct broker (MT5 returns all its positions at once). Includes trades
    opened directly in the terminal, not just app-opened ones.
    """
    settings = get_or_create_settings(session)
    bm = settings.broker_map or {}
    seen: set[str] = set()
    out: list[PositionView] = []
    for ac in AssetClass:
        name = bm.get(ac.value, "sim")
        if name in seen:
            continue
        seen.add(name)
        try:
            out.extend(get_broker_for(ac, bm).get_open_positions())
        except Exception as exc:  # noqa: BLE001
            _log.warning("live broker positions failed", extra={"broker": name, "error": str(exc)})
    return out


def total_unrealized(session: Session) -> float:
    return round(sum(p.unrealized_pnl for p in live_broker_positions(session)), 2)


def broker_closed_trades(session: Session, lookback_days: int = 120) -> list[PositionView] | None:
    """Closed trades from the brokers' own deal history (broker truth, matches Exness).

    Aggregates across the distinct configured brokers. Returns None if no configured broker can
    report history — the caller then falls back to app-tracked closed positions.
    """
    from datetime import timedelta

    settings = get_or_create_settings(session)
    bm = settings.broker_map or {}
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    seen: set[str] = set()
    out: list[PositionView] = []
    supported = False
    for ac in AssetClass:
        name = bm.get(ac.value, "sim")
        if name in seen:
            continue
        seen.add(name)
        try:
            res = get_broker_for(ac, bm).get_closed_trades(since)
        except Exception as exc:  # noqa: BLE001
            _log.warning("broker closed trades failed", extra={"broker": name, "error": str(exc)})
            continue
        if res is not None:
            supported = True
            out.extend(res)
    if not supported:
        return None
    out.sort(
        key=lambda v: v.closed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )
    return out


def broker_realized_today(session: Session) -> float | None:
    """Realized P&L today from the broker's own deal history (the account truth, including
    trades closed directly in the terminal). Returns None if no configured broker supports
    it — the caller then falls back to app-tracked realized P&L."""
    from datetime import datetime, timezone

    settings = get_or_create_settings(session)
    bm = settings.broker_map or {}
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    seen: set[str] = set()
    total = 0.0
    supported = False
    for ac in AssetClass:
        name = bm.get(ac.value, "sim")
        if name in seen:
            continue
        seen.add(name)
        try:
            val = get_broker_for(ac, bm).get_realized_pnl(day_start)
        except Exception as exc:  # noqa: BLE001
            _log.warning("broker realized lookup failed", extra={"broker": name, "error": str(exc)})
            continue
        if val is not None:
            supported = True
            total += val
    return round(total, 2) if supported else None


def realized_today(session: Session) -> float:
    """Realized P&L today: broker truth if available, else app-tracked."""
    b = broker_realized_today(session)
    if b is not None:
        return b
    return get_or_create_daily_state(session).realized_pnl


def current_equity(session: Session) -> float | None:
    """Equity of the live trading account(s). Prefers REAL brokers over the sim fallback so a
    simulator's fake $100k balance never pollutes the real-account baseline. Sums across
    distinct real brokers; if only the sim is configured, returns the sim equity."""
    settings = get_or_create_settings(session)
    bm = settings.broker_map or {}
    seen: set[str] = set()
    equities: dict[str, float] = {}
    for ac in AssetClass:
        name = bm.get(ac.value, "sim")
        if name in seen:
            continue
        seen.add(name)
        try:
            broker = get_broker_for(ac, bm)
            equities[broker.name] = broker.get_account().equity
        except Exception as exc:  # noqa: BLE001
            _log.warning("equity lookup failed", extra={"broker": name, "error": str(exc)})
    if not equities:
        return None
    real = {k: v for k, v in equities.items() if k != "sim"}
    use = real or equities
    return round(sum(use.values()), 2)


def evaluate_daily_pause(session: Session) -> bool:
    """Pause NEW trades for the day if the account's loss (realized today + floating) reaches
    the RISK.md max-daily-loss limit. Uses broker truth, so it protects the live account no
    matter where trades were placed. Never auto-unpauses (a new UTC day resets it).
    """
    risk = get_or_create_risk_config(session)
    # Breaker disabled (e.g. demo-account testing): never auto-pause on daily loss.
    if not risk.daily_loss_breaker_enabled:
        return False

    daily = get_or_create_daily_state(session)
    if daily.trading_paused:
        return True

    if daily.starting_equity is None:
        eq = current_equity(session)
        if eq is not None:
            daily.starting_equity = eq
            session.add(daily)
            session.commit()

    equity = daily.starting_equity
    if not equity or equity <= 0:
        return False

    # The daily-loss breaker fires on REALIZED losses (closed trades), like a pro desk —
    # not on open-position floating P&L, which swings and recovers (open trades are managed
    # by their stops, not this circuit breaker).
    realized = realized_today(session)
    drawdown = -realized  # positive when realized losses exceed gains today
    limit = equity * risk.max_daily_loss
    if drawdown >= limit:
        daily.trading_paused = True
        daily.pause_reason = (
            f"daily loss limit reached: realized {realized} "
            f"(>= {limit:.2f} = {risk.max_daily_loss*100:.0f}% of {equity})"
        )
        session.add(daily)
        session.commit()
        _log.warning("daily loss limit hit (realized) — trading paused",
                     extra={"realized": realized, "limit": round(limit, 2)})
        return True
    return False

# Per-asset-class default tradable increment used for position sizing.
_QTY_STEP = {
    AssetClass.STOCK: 1.0,     # whole shares
    AssetClass.CRYPTO: None,   # fractional
    AssetClass.FOREX: None,
    AssetClass.METAL: None,
}


def build_limits(session: Session) -> RiskLimits:
    cfg = get_settings()
    rc = get_or_create_risk_config(session)
    return RiskLimits(
        risk_per_trade=rc.risk_per_trade,
        max_open_positions=rc.max_open_positions,
        max_daily_loss=rc.max_daily_loss,
        max_total_exposure=rc.max_total_exposure,
        per_pair_cooldown_minutes=rc.per_pair_cooldown_minutes,
        risk_per_trade_ceiling=cfg.risk_per_trade_ceiling,
        daily_loss_breaker_enabled=rc.daily_loss_breaker_enabled,
    )


@dataclass
class ScanCache:
    """Per-scan memo so a watchlist/Hybrid sweep makes ONE broker round-trip for the open book and
    one ``get_account`` per broker — not N. The open book and account state don't change between
    symbols within a single read-only ranking pass, so caching them turns O(N) broker I/O (slow
    over MT5's synchronous terminal IPC) into O(1).

    Create one per scan and discard it. Do NOT reuse across scans or pass it to the real open path
    (``analyze_symbol``), which must see fresh broker truth before opening a trade.
    """
    open_book: list[tuple[str, str]] | None = None
    accounts: dict[str, AccountState] = field(default_factory=dict)


def _scan_open_book(session: Session, cache: ScanCache | None) -> list[tuple[str, str]]:
    """Broker open book as (symbol, direction) tuples; memoized on ``cache`` for one scan."""
    if cache is not None and cache.open_book is not None:
        return cache.open_book
    try:
        book = [(p.symbol, p.direction) for p in live_broker_positions(session)]
    except Exception:  # noqa: BLE001 - never let a broker hiccup block sizing
        book = []
    if cache is not None:
        cache.open_book = book
    return book


def build_account_state(session: Session, broker: BrokerAdapter,
                        cache: ScanCache | None = None) -> AccountState:
    # The broker round-trip (get_account) is the expensive part; memoize it per broker for a scan.
    if cache is not None and broker.name in cache.accounts:
        acct = cache.accounts[broker.name]
    else:
        acct = broker.get_account()
        if cache is not None:
            cache.accounts[broker.name] = acct
    # Aggregate risk-at-entry across locally-tracked OPEN positions for exposure accounting.
    open_positions = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()
    total_risk = sum((p.risk_amount or 0.0) for p in open_positions)

    daily = get_or_create_daily_state(session, starting_equity=acct.equity)
    if daily.starting_equity is None:
        daily.starting_equity = acct.equity
        session.commit()

    # The daily-loss pause only COUNTS while the breaker is armed. With it OFF (demo/testing) a pause
    # set earlier no longer blocks — consistent with the manager skipping the daily-loss veto.
    breaker_on = get_or_create_risk_config(session).daily_loss_breaker_enabled
    return AccountState(
        equity=acct.equity,
        cash=acct.cash,
        open_positions=acct.open_positions,
        total_risk_amount=total_risk,
        daily_realized_pnl=daily.realized_pnl,
        trading_paused=daily.trading_paused and breaker_on,
    )


def last_pair_close_at(session: Session, symbol: str) -> datetime | None:
    row = session.scalars(
        select(Position)
        .where(Position.symbol == symbol, Position.status == PositionStatus.CLOSED.value)
        .order_by(Position.closed_at.desc())
    ).first()
    if row and row.closed_at:
        closed = row.closed_at
        return closed if closed.tzinfo else closed.replace(tzinfo=timezone.utc)
    return None


def last_dir_loss_at(session: Session, symbol: str, direction: str) -> datetime | None:
    """Close time of the MOST RECENT closed trade on this symbol+direction, but only if it was a
    LOSS — for the loss-aware cooldown. Returns None if the last such trade won or there was none
    (a win resets the cooldown — the setup worked, so re-entry is fine). A loss = realized_pnl < 0,
    or, when P&L wasn't recorded, the exit went against the trade (stopped out)."""
    if direction not in ("long", "short"):
        return None
    row = session.scalars(
        select(Position)
        .where(Position.symbol == symbol, Position.direction == direction,
               Position.status == PositionStatus.CLOSED.value)
        .order_by(Position.closed_at.desc())
    ).first()
    if not row or not row.closed_at:
        return None
    if row.realized_pnl is not None:
        lost = row.realized_pnl < 0
    elif row.last_price is not None and row.entry_price is not None:
        lost = (row.last_price >= row.entry_price) if direction == "short" else (row.last_price <= row.entry_price)
    else:
        return None
    if not lost:
        return None
    closed = row.closed_at
    return closed if closed.tzinfo else closed.replace(tzinfo=timezone.utc)


def _norm_symbol(s: str) -> str:
    """Loose symbol key for matching across formats (BTC/USD, BTCUSDm, BTCUSD)."""
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def broker_has_open_same_direction(session: Session, symbol: str, direction: str) -> bool:
    """True if the BROKER already has an open position in this symbol + direction (anti-stacking).

    Broker truth, so it also catches trades opened directly in the terminal. Used as the final
    gate in the executor right before a new order is submitted.
    """
    norm = _norm_symbol(symbol)
    try:
        positions = live_broker_positions(session)
    except Exception:  # noqa: BLE001
        return False
    return any(_norm_symbol(p.symbol) == norm and p.direction == direction for p in positions)


def _lot_specs(broker, asset_class, symbol: str) -> tuple[float | None, float]:
    """The broker's (lot_step, min_lot) for sizing — real broker increments when available, else
    the generic per-asset-class step and a 0.0 minimum (no broker-min known)."""
    step = _QTY_STEP.get(asset_class)
    min_lot = 0.0
    try:
        b_step, b_min = broker.lot_constraints(symbol)
        if b_step and b_step > 0:
            step = b_step
        if b_min and b_min > 0:
            min_lot = b_min
    except Exception:  # noqa: BLE001 - never let a broker hiccup block sizing
        pass
    return step, min_lot


def _risk_per_lot(broker, symbol: str, entry: float | None, stop: float | None) -> float | None:
    """Account-currency risk of 1 lot over the stop — the broker's value if available (currency-
    correct via order_calc_profit), else ``|entry-stop| × contract_size`` (correct only when the
    quote currency IS the account currency, e.g. the sim/USD path)."""
    if not entry or not stop or entry == stop:
        return None
    try:
        rpl = broker.risk_per_lot(symbol, entry, stop)
        if rpl and rpl > 0:
            return rpl
    except Exception:  # noqa: BLE001
        pass
    try:
        contract = broker.contract_size(symbol) or 1.0
    except Exception:  # noqa: BLE001
        contract = 1.0
    return abs(entry - stop) * contract


def assess(
    session: Session,
    proposal: TradeProposal,
    override_risk_fraction: float | None = None,
    cache: ScanCache | None = None,
) -> RiskDecision:
    """Run a proposal through the deterministic Risk Manager against live state.

    ``override_risk_fraction`` (Mode A manual size) is re-clamped to the 3% ceiling inside the
    manager, so a user-chosen size can never exceed the hard per-trade cap. ``cache`` (a
    ``ScanCache``) memoizes the broker open book + account for a multi-symbol scan; omit it for
    single-symbol assessments and the real open path so they always read fresh broker truth.
    """
    from app.risk.correlation import correlated_concentration

    settings = get_or_create_settings(session)
    broker = get_broker_for(proposal.asset_class, settings.broker_map)
    limits = build_limits(session)
    account = build_account_state(session, broker, cache=cache)
    # Open book from broker truth (so manual/terminal trades count too). Used for BOTH the
    # anti-stacking and correlated-concentration checks, which keeps the preview consistent with
    # the executor's final broker-truth gate — a stale app-DB row can no longer "phantom-block" a
    # symbol that isn't actually open at the broker. (Refuses e.g. a 2nd same-direction trade in
    # an open symbol, or a 3rd position piling onto one risk factor like "long USD" x3.)
    open_book = _scan_open_book(session, cache)
    norm = _norm_symbol(proposal.symbol)
    same_dir = any(_norm_symbol(sym) == norm and d == proposal.direction.value for sym, d in open_book)
    correlated = correlated_concentration(open_book, proposal.symbol, proposal.direction.value)
    # Broker tradeability: don't approve a setup the broker won't let us OPEN (instrument disabled
    # / close-only, or the wrong side of a long-only/short-only symbol). Caught here so it shows as
    # "not tradeable", not a tempting "approved · Open" that bounces at order time.
    not_tradeable = None
    if proposal.direction.value in ("long", "short"):
        try:
            ok_open, why = broker.can_open(proposal.symbol, proposal.direction.value)
            if not ok_open:
                not_tradeable = why
        except Exception:  # noqa: BLE001 - never let a tradeability lookup block sizing
            pass
    qty_step, min_lot = _lot_specs(broker, proposal.asset_class, proposal.symbol)
    return evaluate_proposal(
        proposal,
        account,
        limits,
        now=datetime.now(timezone.utc),
        last_pair_close_at=last_pair_close_at(session, proposal.symbol),
        last_dir_loss_at=last_dir_loss_at(session, proposal.symbol, proposal.direction.value),
        qty_step=qty_step,
        min_qty=min_lot,
        override_risk_fraction=override_risk_fraction,
        has_open_same_direction=same_dir,
        correlated_exposure=correlated,
        not_tradeable_reason=not_tradeable,
        risk_per_lot=_risk_per_lot(broker, proposal.symbol, proposal.entry, proposal.stop_loss),
    )


def proposal_from_record(record) -> TradeProposal:
    """Rebuild a minimal TradeProposal from a stored record, enough to re-run the Risk Manager."""
    from app.models.enums import Direction

    return TradeProposal(
        symbol=record.symbol,
        asset_class=AssetClass(record.asset_class),
        timeframe=record.timeframe,
        direction=Direction(record.direction),
        entry=record.entry,
        stop_loss=record.stop_loss,
        take_profit=record.take_profit,
        confidence=record.confidence or 0.0,
    )


def _risk_fraction_for_lots(*, lots: float, risk_per_lot: float | None, equity: float) -> float | None:
    """The per-trade risk fraction that a desired LOT size implies (lots × account-ccy risk-per-lot
    ÷ equity). None if not computable."""
    if lots <= 0 or not risk_per_lot or risk_per_lot <= 0 or equity <= 0:
        return None
    return (lots * risk_per_lot) / equity


def size_preview(session: Session, record, desired_lots: float | None = None) -> dict:
    """Risk verdict + cost/leverage economics for a proposal at a chosen lot size.

    ``desired_lots=None`` uses the AI's default (risk_per_trade) sizing. Any desired size is
    clamped to the 3% per-trade ceiling by the Risk Manager. Economics are computed on the
    resulting size so the user sees exactly what they'd spend and at what leverage.
    """
    from app.risk.manager import size_position

    settings = get_or_create_settings(session)
    proposal = proposal_from_record(record)
    broker = get_broker_for(proposal.asset_class, settings.broker_map)
    limits = build_limits(session)
    account = build_account_state(session, broker)
    equity = account.equity
    entry = record.entry or 0.0
    stop = record.stop_loss or 0.0
    rpl = _risk_per_lot(broker, record.symbol, entry, stop)   # account-ccy risk of 1 lot

    override_rf = None
    if desired_lots is not None:
        override_rf = _risk_fraction_for_lots(lots=desired_lots, risk_per_lot=rpl, equity=equity)

    decision = assess(session, proposal, override_risk_fraction=override_rf)

    qty_step, min_lot = _lot_specs(broker, proposal.asset_class, record.symbol)
    ceiling = limits.risk_per_trade_ceiling

    # Size used for the economics: the approved LOTS when allowed; otherwise the would-be sized lots
    # (so the cost still shows even when a gate would block it). Both honour the clamped risk
    # fraction + exposure budget, sized off the account-currency risk-per-lot.
    if decision.approved and decision.approved_qty > 0:
        lots = decision.approved_qty
    else:
        rf = min(override_rf if override_rf is not None else limits.risk_per_trade, ceiling)
        lots, risk_amt = size_position(
            equity=equity, risk_fraction=rf, entry=entry, stop_loss=stop,
            qty_step=qty_step, risk_per_lot=rpl,
        )
        exposure_remaining = equity * limits.max_total_exposure - account.total_risk_amount
        if rpl and risk_amt > exposure_remaining and exposure_remaining > 0:
            from app.risk.manager import _floor_to_step

            lots = _floor_to_step(exposure_remaining / rpl, qty_step)
        if min_lot and lots < min_lot:
            lots = min_lot   # can't trade smaller than the broker minimum

    # The per-trade ceiling expressed in lots, so the UI can show/limit the cap. Floored to the
    # broker minimum so the UI shows the size that would ACTUALLY open (never a sub-min figure).
    max_lots = None
    if equity > 0:
        ceiling_lots, _ = size_position(
            equity=equity, risk_fraction=ceiling, entry=entry, stop_loss=stop,
            qty_step=qty_step, risk_per_lot=rpl,
        )
        if min_lot and ceiling_lots < min_lot:
            ceiling_lots = min_lot
        max_lots = round(ceiling_lots, 4)

    # Was the user's requested size reduced?
    capped = decision.decision == RiskDecisionType.RESIZED or (
        override_rf is not None and override_rf > ceiling + 1e-9
    )

    is_long = record.direction == "long"
    econ = broker.quote_economics(record.symbol, lots, entry, is_long) or {}
    economics = {
        "lots": econ.get("lots"),
        "qty_units": round(lots, 4) if lots else 0.0,
        "margin_usd": econ.get("margin_usd"),
        "notional_usd": econ.get("notional_usd"),
        "leverage": econ.get("leverage"),
        "pct_of_equity": (
            round(econ["notional_usd"] / equity, 4)
            if econ.get("notional_usd") and equity > 0
            else None
        ),
    }
    return {"risk": decision, "economics": economics, "capped": capped, "max_lots": max_lots,
            "min_lot_floored": decision.min_lot_floored}
