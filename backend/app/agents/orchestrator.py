"""Orchestrator / Decision agent.

Acts like a disciplined head trader: requires confluence between the technical and
fundamental reads, sits out unclear or conflicting setups, and refuses to trade into
high-impact news windows the Fundamental Analyst flagged. Output is a ``TradeProposal``
that may be ``NO_TRADE`` — declining is a valid, encouraged result.

LLM-driven when a key is configured; otherwise a deterministic confluence rule runs so the
pipeline works offline. The deterministic path is conservative by design.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import FundamentalRead, TechnicalRead, TradeProposal

log = get_logger("agents.orchestrator")

_SYSTEM = """You are the head trader making the final call. You receive a Technical read and
a Fundamental read. Behave with discipline:
- Require CONFLUENCE: only propose a trade when technical and fundamental signals agree, or
  when one is clearly dominant and the other is neutral (not opposing).
- SIT OUT unclear, conflicting, or low-confidence setups — output direction "no_trade".
- NEVER propose entering during a high-impact event window flagged by the Fundamental
  Analyst (stand_aside_windows). If now falls in such a window, output "no_trade".
- When you do propose a trade, set entry, a protective stop_loss on the correct side, and a
  take_profit with a sensible reward:risk (aim >= 1.5R). Give a clear written rationale.
You do NOT size positions — a deterministic Risk Manager does that downstream. Return strict
JSON matching the schema."""


def _now_in_stand_aside(fundamental: FundamentalRead, now: datetime) -> bool:
    for w in fundamental.stand_aside_windows:
        start = w.start if w.start.tzinfo else w.start.replace(tzinfo=timezone.utc)
        end = w.end if w.end.tzinfo else w.end.replace(tzinfo=timezone.utc)
        if start <= now <= end:
            return True
    return False


def _last_close(technical: TechnicalRead) -> float | None:
    for tf in technical.timeframes:
        if "last_close" in tf.indicators:
            return tf.indicators["last_close"]
    return None


def _deterministic_decision(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead, now: datetime,
) -> TradeProposal:
    base = TradeProposal(
        symbol=symbol, asset_class=asset_class, timeframe=timeframe,
        direction=Direction.NO_TRADE, confidence=0.0,
        technical=technical, fundamental=fundamental,
    )

    if _now_in_stand_aside(fundamental, now):
        base.rationale = "Standing aside: inside a high-impact event window."
        return base

    trend = technical.overall_trend
    bias = fundamental.bias

    # Confluence: trend direction must not be opposed by fundamental bias.
    if trend == "up" and bias != TradingBias.BEARISH:
        direction = Direction.LONG
    elif trend == "down" and bias != TradingBias.BULLISH:
        direction = Direction.SHORT
    else:
        base.rationale = (
            f"No confluence: technical trend={trend}, fundamental bias={bias.value}. Sitting out."
        )
        return base

    entry = _last_close(technical)
    if entry is None or entry <= 0:
        base.rationale = "No usable entry price from technical read; sitting out."
        return base

    # Stop from recent swing level; fall back to a fixed % if missing.
    tf0 = technical.timeframes[0] if technical.timeframes else None
    if direction == Direction.LONG:
        support = tf0.support_levels[0] if tf0 and tf0.support_levels else None
        stop = support if support is not None and support < entry else round(entry * 0.98, 4)
        risk = entry - stop
        take_profit = round(entry + 2 * risk, 4)
    else:
        resistance = tf0.resistance_levels[0] if tf0 and tf0.resistance_levels else None
        stop = resistance if resistance is not None and resistance > entry else round(entry * 1.02, 4)
        risk = stop - entry
        take_profit = round(entry - 2 * risk, 4)

    # Confidence blends technical confidence with a small fundamental contribution.
    confidence = round(min(0.9, 0.5 * technical.confidence + 0.2 + 0.3 * fundamental.confidence), 2)

    base.direction = direction
    base.entry = round(entry, 4)
    base.stop_loss = round(stop, 4)
    base.take_profit = take_profit
    base.confidence = confidence
    base.rationale = (
        f"Confluence {direction.value.upper()}: technical trend={trend}, fundamental bias="
        f"{bias.value}. Entry {base.entry}, stop {base.stop_loss} (recent swing), target "
        f"{take_profit} (~2R). Deterministic decision (no LLM)."
    )
    return base


def run_orchestrator(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead,
    now: datetime | None = None,
) -> TradeProposal:
    now = now or datetime.now(timezone.utc)

    if llm_available():
        user = (
            f"symbol={symbol} asset_class={asset_class.value} primary_timeframe={timeframe} "
            f"now={now.isoformat()}\n\n"
            f"TECHNICAL READ:\n{technical.model_dump_json(indent=2)}\n\n"
            f"FUNDAMENTAL READ:\n{fundamental.model_dump_json(indent=2)}\n\n"
            "Decide. Remember: confluence required, sit out conflicts and event windows, "
            "'no_trade' is acceptable."
        )
        result = analyze(system=_SYSTEM, user=user, schema=TradeProposal, max_tokens=3000)
        if result is not None:
            # Trust the model's decision but attach the reasoning bundle + enforce stand-aside.
            result.symbol = result.symbol or symbol
            result.asset_class = asset_class
            result.timeframe = timeframe
            result.technical = technical
            result.fundamental = fundamental
            if result.direction != Direction.NO_TRADE and _now_in_stand_aside(fundamental, now):
                log.info("overriding LLM proposal -> no_trade (event window)", extra={"symbol": symbol})
                result.direction = Direction.NO_TRADE
                result.rationale = "Overridden to NO_TRADE: inside a flagged high-impact window."
            log.info("orchestrator decision via LLM", extra={"symbol": symbol, "direction": result.direction.value})
            return result

    proposal = _deterministic_decision(symbol, asset_class, timeframe, technical, fundamental, now)
    log.info("orchestrator decision deterministic", extra={"symbol": symbol, "direction": proposal.direction.value})
    return proposal
