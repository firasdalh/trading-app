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
from app.models.db import Position
from app.models.enums import AssetClass, PositionStatus
from app.models.schemas import AccountState, RiskDecision, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal

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
