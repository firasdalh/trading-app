"""Glue between the pure Risk Manager and live state (DB + broker).

Keeps the manager itself pure: this layer gathers the account snapshot, active limits,
exposure, and per-pair cooldown, then calls ``evaluate_proposal``. Nothing here weakens a
limit — it only assembles inputs.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.base import BrokerAdapter
from app.brokers.registry import get_broker_for
from app.core.config import get_settings
from app.core.state import get_or_create_daily_state, get_or_create_risk_config, get_or_create_settings
from app.core.logging import get_logger
from app.models.db import Position
from app.models.enums import AssetClass, PositionStatus
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

    risk = get_or_create_risk_config(session)
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
    )


def build_account_state(session: Session, broker: BrokerAdapter) -> AccountState:
    acct = broker.get_account()
    # Aggregate risk-at-entry across locally-tracked OPEN positions for exposure accounting.
    open_positions = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
    ).all()
    total_risk = sum((p.risk_amount or 0.0) for p in open_positions)

    daily = get_or_create_daily_state(session, starting_equity=acct.equity)
    if daily.starting_equity is None:
        daily.starting_equity = acct.equity
        session.commit()

    return AccountState(
        equity=acct.equity,
        cash=acct.cash,
        open_positions=acct.open_positions,
        total_risk_amount=total_risk,
        daily_realized_pnl=daily.realized_pnl,
        trading_paused=daily.trading_paused,
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


def assess(session: Session, proposal: TradeProposal) -> RiskDecision:
    """Run a proposal through the deterministic Risk Manager against live state."""
    settings = get_or_create_settings(session)
    broker = get_broker_for(proposal.asset_class, settings.broker_map)
    limits = build_limits(session)
    account = build_account_state(session, broker)
    return evaluate_proposal(
        proposal,
        account,
        limits,
        now=datetime.now(timezone.utc),
        last_pair_close_at=last_pair_close_at(session, proposal.symbol),
        qty_step=_QTY_STEP.get(proposal.asset_class),
    )
