"""Fundamental Analyst agent.

STUBBED for Milestone 4: returns a neutral read with no stand-aside windows so the
orchestrator can run end-to-end. Milestone 6 wires in real news / economic-calendar /
sentiment providers and the LLM analysis (weighing surprise vs. expectation).
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.models.enums import TradingBias
from app.models.schemas import FundamentalRead

log = get_logger("agents.fundamental")


def run_fundamental(symbol: str) -> FundamentalRead:
    """M4 stub: neutral bias, no high-impact windows. Real implementation in M6."""
    log.info("fundamental stub (neutral)", extra={"symbol": symbol})
    return FundamentalRead(
        symbol=symbol,
        bias=TradingBias.NEUTRAL,
        key_drivers=[],
        surprise_assessment="stubbed — no news/calendar wired yet (M6)",
        stand_aside_windows=[],
        confidence=0.0,
        notes="Fundamental analysis not yet enabled; treat as neutral.",
    )
