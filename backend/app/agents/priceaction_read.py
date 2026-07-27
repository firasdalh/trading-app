"""AI PRICE-ACTION classifier — used at the deterministic engine's "opposing level in the way" fork.

When the engine wants a trend entry but a major higher-timeframe level sits right in the path
(resistance overhead for a long / support below for a short), it applies a FIXED rule: stand aside and
wait for a break. That's blunt — some of those levels are about to break, others will cleanly reject.
This module classifies HOW price is behaving at that level so the deterministic engine can decide:

- It returns ONE of {likely_reject, likely_break, indecision} + the evidence it used + a confidence.
  It NEVER picks a direction/entry, NEVER recalculates a signal, and NEVER says buy/sell/wait — the
  deterministic engine decides (wait / take the trade through / keep waiting) from the label.
- Anchored to the deterministic FACTS (distance to the level in ATR, momentum toward it, RSI turn,
  channel position/fit, volume trend, swing structure), structured + temp-0, so the label is
  low-variance. Degrades to None (deterministic fallback = keep waiting) when no LLM / the call fails.
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger

log = get_logger("agents.priceaction_read")

_CACHE: dict[tuple, tuple[float, "PriceActionRead"]] = {}
_TTL_SEC = 90  # a level read is stable for a short window; cache to avoid duplicate calls in a scan


class PriceActionRead(BaseModel):
    category: Literal["likely_reject", "likely_break", "indecision"] = Field(
        description="how price is likely to resolve at the opposing level")
    evidence: str = Field(description="the specific indicator values you used to justify the category")
    confidence: float = Field(description="0..1 — how confident the classification is")


_SYSTEM = (
    "You are a PRICE-ACTION CLASSIFIER for a trading system, not a trader. The deterministic engine has "
    "ALREADY chosen a trade direction, and a major level (resistance for a long, support for a short) "
    "sits in the path. You are given already-computed FACTS about how price is approaching that level. "
    "Your ONLY job is to classify how it is likely to resolve, into exactly one category:\n"
    "- likely_break: momentum and participation are driving THROUGH the level (strong aligned MACD, "
    "expanding volume, price pressing into/through the channel band) — the level is giving way.\n"
    "- likely_reject: the level is holding — momentum fading into it, RSI turning back from an extreme, "
    "price stalling at the band — a bounce/rejection off the level is likely.\n"
    "- indecision: mixed/unclear — no decisive push or clear rejection yet.\n"
    "Rules: classify ONLY. Return the specific evidence (the given numbers) you used and a confidence "
    "0-1. NEVER invent numbers, NEVER pick a direction or an entry/stop/target, NEVER recalculate a "
    "signal, and NEVER say buy/sell/hold/wait/enter — deciding what to do is the deterministic engine's "
    "job, not yours."
)


def _snapshot(symbol: str, direction, level: float, ind: dict, tf: str) -> str:
    px, atr = ind.get("last_close"), ind.get("atr14")
    dist = round(abs(level - px) / atr, 2) if (px and atr) else None
    dv = getattr(direction, "value", direction)
    side = "resistance overhead" if dv == "long" else "support below"
    macd, macdp = ind.get("macd_hist"), ind.get("macd_hist_prev")
    mom = ("building" if (macd is not None and macdp is not None and abs(macd) > abs(macdp))
           else "fading" if (macd is not None and macdp is not None) else "?")
    rsi, rsip = ind.get("rsi14"), ind.get("rsi14_prev")
    rsi_turn = ("turning down" if (rsi is not None and rsip is not None and rsi < rsip)
                else "turning up" if (rsi is not None and rsip is not None) else "?")
    return (
        f"SYMBOL {symbol} ({tf}); engine's chosen direction: {dv}\n"
        f"Opposing level ({side}): {round(level, 6)}; distance from price: {dist} ATR\n"
        f"MACD hist: {macd} (prev {macdp}) -> momentum toward the level {mom}\n"
        f"RSI(14): {rsi} (prev {rsip}) -> {rsi_turn}\n"
        f"ADX: {ind.get('adx')}  +DI: {ind.get('plus_di')}  -DI: {ind.get('minus_di')}\n"
        f"Regression channel: fit r2 {ind.get('chan_r2')}, price position {ind.get('chan_pos')} "
        f"(0=lower band, 1=upper band, >1/<0 = broken out)\n"
        f"Volume trend: {ind.get('vol_trend')} (1=expanding into the move, -1=fading)\n"
        f"Swing structure: {ind.get('structure')} (1=up, -1=down), change-of-character {ind.get('choch')}\n"
        "Classify how price is likely to resolve AT this level."
    )


def interpret_price_action(symbol: str, direction, level: float, ind: dict, tf: str) -> "PriceActionRead | None":
    """Classify how price will resolve at the opposing level (category + evidence + confidence). Returns
    None when no LLM is configured/available or the call fails — the caller then keeps waiting (the
    deterministic fallback). Cached briefly per (symbol, tf, direction, level)."""
    if not llm_available():
        return None
    key = (symbol, tf, getattr(direction, "value", str(direction)), round(level, 6))
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit and (now - hit[0]) < _TTL_SEC:
        return hit[1]
    read = analyze(system=_SYSTEM, user=_snapshot(symbol, direction, level, ind, tf),
                   schema=PriceActionRead, max_tokens=500)
    if read is None:
        return None
    read.confidence = max(0.0, min(1.0, float(read.confidence)))
    _CACHE[key] = (now, read)
    log.info("price action classified", extra={"symbol": symbol, "tf": tf,
                                                "category": read.category, "confidence": read.confidence})
    return read
