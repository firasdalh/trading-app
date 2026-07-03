"""Shared enums used across agents, risk, brokers, and the API.

Kept in one place so the Pydantic schemas and SQLAlchemy models agree on string values
(we store the enum *value*, not its name, in the DB).
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """str-backed enum so values serialize cleanly to JSON and DB columns."""

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class AssetClass(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"
    FOREX = "forex"
    METAL = "metal"
    ENERGY = "energy"   # oil, natural gas, etc.
    INDEX = "index"     # stock indices: US500, US30, USTEC, DE40, UK100, etc.


class Direction(StrEnum):
    """Direction of a trade idea. ``NO_TRADE`` lets the orchestrator decline."""

    LONG = "long"
    SHORT = "short"
    NO_TRADE = "no_trade"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"           # created locally, not yet sent
    SUBMITTED = "submitted"       # accepted by broker
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"         # broker rejected
    ERROR = "error"               # submission errored; needs reconciliation


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TradingBias(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class ExecutionMode(StrEnum):
    """User-selectable execution mode (stored in AppSettings)."""

    A_PROPOSE_APPROVE = "A_PROPOSE_APPROVE"   # default: AI proposes, user approves
    B_AUTO_PAPER = "B_AUTO_PAPER"             # auto-execute, paper broker only
    C_AUTO_LIVE = "C_AUTO_LIVE"               # auto-execute, live (gated + warned)


class RiskDecisionType(StrEnum):
    APPROVED = "approved"     # accepted as proposed
    RESIZED = "resized"       # accepted but position size reduced
    VETOED = "vetoed"         # rejected outright


class ReviewDecision(StrEnum):
    """The LLM reviewer's verdict on a deterministic setup. It may only confirm or veto."""

    CONFIRM = "confirm"
    VETO = "veto"


class ProposalStatus(StrEnum):
    PENDING_RISK = "pending_risk"           # awaiting Risk Manager
    RISK_VETOED = "risk_vetoed"             # Risk Manager rejected
    PENDING_APPROVAL = "pending_approval"   # risk-approved, awaiting user (Mode A)
    APPROVED = "approved"                   # user (or auto-mode) approved
    REJECTED = "rejected"                   # user rejected
    EXECUTED = "executed"                   # order submitted
    EXPIRED = "expired"                     # went stale before action


class ConditionalOrderType(StrEnum):
    """How a conditional (pending) setup enters once price reaches the trigger level."""

    SELL_STOP = "sell_stop"   # short on a break DOWN through a support level (continuation)
    BUY_STOP = "buy_stop"     # long on a break UP through a resistance level (continuation)
    SELL_LIMIT = "sell_limit" # short on a bounce UP into resistance (better price)
    BUY_LIMIT = "buy_limit"   # long on a dip DOWN into support (better price)


class ConditionalStatus(StrEnum):
    """Lifecycle of an armed conditional setup (a 'wait for the trigger' order)."""

    ARMED = "armed"           # watching for the trigger
    TRIGGERED = "triggered"   # trigger hit + re-check passed -> a proposal/position was created
    REJECTED = "rejected"     # trigger hit but the re-check (double-check) declined it
    EXPIRED = "expired"       # validity window elapsed without a trigger
    CANCELLED = "cancelled"   # cancelled by the user
    INVALIDATED = "invalidated"  # thesis broke BEFORE the trigger (price closed through the stop)
