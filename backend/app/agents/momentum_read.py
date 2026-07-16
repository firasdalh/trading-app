"""AI MOMENTUM classifier — used at the deterministic engine's ambiguous-momentum forks.

When the entry-timeframe MACD is rolling over against the trend (or RSI is stretched), the
deterministic engine used to apply a FIXED rule: arm a resumption and wait. This module replaces
that blunt rule with a CLASSIFICATION of *why* momentum disagrees, so the engine can decide
enter / wait / reject / arm from a richer read. It is a CLASSIFIER, not a trader:

- It returns ONE of {healthy_pullback, weak_momentum, probable_reversal} + the evidence it used +
  a confidence score. It NEVER picks a direction/entry, NEVER recalculates a signal, and NEVER says
  buy/sell/wait — the deterministic engine decides what to do from the label.
- Anchored to the deterministic indicator FACTS (MACD/RSI/ADX/EMA/higher-TF), structured + temp-0
  (provider default), so the label is low-variance. Degrades to None (deterministic fallback) when
  no LLM is configured or the call fails.
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger

log = get_logger("agents.momentum_read")

# The immediate higher timeframe for the HTF-alignment fact (mirrors the engine's laddered context).
_HIGHER_TF = {"1m": "5m", "5m": "15m", "15m": "1h", "30m": "1h", "1h": "4h", "4h": "1d", "1d": "1w"}
_CACHE: dict[tuple, tuple[float, "MomentumRead"]] = {}
_TTL_SEC = 90  # a momentum read is stable for a short window; cache to avoid duplicate calls in a scan


class MomentumRead(BaseModel):
    category: Literal["healthy_pullback", "weak_momentum", "probable_reversal"] = Field(
        description="the classification of WHY momentum currently disagrees with the trend")
    evidence: str = Field(description="the specific indicator values you used to justify the category")
    confidence: float = Field(description="0..1 — how confident the classification is")


_SYSTEM = (
    "You are a MOMENTUM CLASSIFIER for a trading system, not a trader. You are given already-computed "
    "indicator FACTS for a setup whose direction the deterministic engine has ALREADY chosen. Your ONLY "
    "job is to classify WHY the entry-timeframe momentum currently disagrees with that direction, into "
    "exactly one category:\n"
    "- healthy_pullback: a normal counter-trend dip/bounce that the HIGHER-timeframe momentum and trend "
    "still support; the move is likely to resume.\n"
    "- weak_momentum: momentum has stalled/flattened and the picture is unclear; it needs confirmation "
    "before committing.\n"
    "- probable_reversal: momentum AND structure argue the move is turning against the setup.\n"
    "Rules: classify ONLY. Return the specific evidence (the given numbers) you used and a confidence "
    "0-1. NEVER invent numbers, NEVER pick a direction or an entry/stop/target, NEVER recalculate a "
    "signal, and NEVER say buy/sell/hold/wait/enter — deciding what to do is the deterministic engine's "
    "job, not yours."
)


def _higher(technical, tf):
    name = _HIGHER_TF.get(tf)
    if not name:
        return None
    return next((t for t in technical.timeframes if t.timeframe == name), None)


def _snapshot(symbol, direction, ind: dict, technical, tf: str) -> str:
    macd, macdp = ind.get("macd_hist"), ind.get("macd_hist_prev")
    atr = ind.get("atr14")
    rolling = ("weakening" if (macd is not None and macdp is not None and abs(macd) < abs(macdp))
               else "building" if (macd is not None and macdp is not None) else "?")
    hi = _higher(technical, tf)
    hi_macd = hi.indicators.get("macd_hist") if hi else None
    hi_trend = hi.trend if hi else "?"
    ema20, px = ind.get("ema20"), ind.get("last_close")
    ema_dist = round((px - ema20) / atr, 2) if (px and ema20 and atr) else None
    side = ("above" if (ema_dist is not None and ema_dist >= 0) else "below") if ema_dist is not None else "?"
    dv = getattr(direction, "value", direction)
    return (
        f"SYMBOL {symbol} ({tf}); engine's chosen direction: {dv}\n"
        f"Entry-TF MACD hist: {macd} (prev {macdp}) -> magnitude {rolling}\n"
        f"Higher-TF ({_HIGHER_TF.get(tf, '?')}) trend: {hi_trend}, MACD hist: {hi_macd}\n"
        f"RSI(14): {ind.get('rsi14')}\n"
        f"ADX: {ind.get('adx')}  +DI: {ind.get('plus_di')}  -DI: {ind.get('minus_di')}\n"
        f"EMA20 distance: {ema_dist} ATR (price {side} value)\n"
        f"ATR: {atr}\n"
        "Classify WHY momentum currently disagrees with the chosen direction."
    )


def interpret_momentum(symbol, direction, ind: dict, technical, tf: str) -> "MomentumRead | None":
    """Classify the momentum disagreement (category + evidence + confidence). Returns None when no LLM
    is configured/available or the call fails — the caller then keeps its deterministic fallback.
    Cached briefly per (symbol, tf, direction) so a scan doesn't re-ask within the same window."""
    if not llm_available():
        return None
    key = (symbol, tf, getattr(direction, "value", str(direction)))
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit and (now - hit[0]) < _TTL_SEC:
        return hit[1]
    read = analyze(system=_SYSTEM, user=_snapshot(symbol, direction, ind, technical, tf),
                   schema=MomentumRead, max_tokens=500)
    if read is None:
        return None
    read.confidence = max(0.0, min(1.0, float(read.confidence)))
    _CACHE[key] = (now, read)
    log.info("momentum classified", extra={"symbol": symbol, "tf": tf,
                                            "category": read.category, "confidence": read.confidence})
    return read
