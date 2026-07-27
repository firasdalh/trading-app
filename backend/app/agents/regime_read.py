"""AI REGIME classifier — used ONLY at the deterministic engine's ambiguous regime boundary.

The engine reads regime deterministically from ADX + volatility (``orchestrator._regime``): a strong
ADX is "trending", a weak ADX is "ranging". The grey zone in between is "moderate" — a mild trend that
backtests show is a net drag as a whole, yet contains BOTH early trends (worth taking) and chop (worth
skipping). A fixed ADX threshold can't tell them apart. This module classifies that boundary case into
one of three textures so the deterministic engine can decide what to do:

- It returns ONE of {emerging_trend, choppy_range, transition} + the evidence it used + a confidence.
  It NEVER picks a direction/entry, NEVER recalculates a signal, and NEVER says buy/sell/wait — the
  deterministic engine decides (promote to a trend / demote to a range / stay aside) from the label.
- Anchored to the deterministic indicator FACTS (ADX/DI/channel/EMA-stack/volatility/structure),
  structured + temp-0, so the label is low-variance. Degrades to None (deterministic fallback) when no
  LLM is configured or the call fails.
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger

log = get_logger("agents.regime_read")

_CACHE: dict[tuple, tuple[float, "RegimeRead"]] = {}
_TTL_SEC = 90  # a regime read is stable for a short window; cache to avoid duplicate calls in a scan


class RegimeRead(BaseModel):
    category: Literal["emerging_trend", "choppy_range", "transition"] = Field(
        description="the texture of the ambiguous (moderate-ADX) regime")
    evidence: str = Field(description="the specific indicator values you used to justify the category")
    confidence: float = Field(description="0..1 — how confident the classification is")


_SYSTEM = (
    "You are a MARKET-REGIME CLASSIFIER for a trading system, not a trader. You are given already-"
    "computed indicator FACTS for a symbol whose trend strength (ADX) sits in the AMBIGUOUS middle zone "
    "— neither a clean trend nor a clear range. Your ONLY job is to classify the texture into exactly "
    "one category:\n"
    "- emerging_trend: a trend is forming/strengthening — directional structure (EMA stack lining up, "
    "one DI clearly leading, a cleanly sloped channel with decent fit) that a fixed ADX cutoff hasn't "
    "confirmed yet, but is worth trading as a trend.\n"
    "- choppy_range: no real direction — flat/tangled EMAs, DI balanced, low channel fit; price is "
    "chopping and a trend entry would get whipsawed.\n"
    "- transition: genuinely in-between / regime is changing (e.g. volatility expanding without "
    "direction, structure conflicting); the safe read is to wait.\n"
    "Rules: classify ONLY. Return the specific evidence (the given numbers) you used and a confidence "
    "0-1. NEVER invent numbers, NEVER pick a direction or an entry/stop/target, NEVER recalculate a "
    "signal, and NEVER say buy/sell/hold/wait/enter — deciding what to do is the deterministic engine's "
    "job, not yours."
)


def _stack(t) -> str:
    """The EMA20/50/200 alignment for a timeframe read (a trend forms when they line up in order)."""
    if t is None:
        return "?"
    e20, e50, e200 = t.indicators.get("ema20"), t.indicators.get("ema50"), t.indicators.get("ema200")
    if None in (e20, e50, e200):
        return "?"
    if e20 > e50 > e200:
        return "bullish (20>50>200)"
    if e20 < e50 < e200:
        return "bearish (20<50<200)"
    return "tangled"


def _snapshot(symbol: str, ind: dict, technical, tf: str) -> str:
    tfs = technical.timeframes if technical else []
    entry = next((t for t in tfs if t.timeframe == tf), None)
    higher = next((t for t in tfs if t.timeframe != tf), None)
    return (
        f"SYMBOL {symbol} ({tf}) — ambiguous (moderate) trend strength\n"
        f"ADX: {ind.get('adx')}  +DI: {ind.get('plus_di')}  -DI: {ind.get('minus_di')}\n"
        f"EMA stack (entry TF): {_stack(entry)}\n"
        f"EMA stack (higher TF {getattr(higher, 'timeframe', '?')}): {_stack(higher)}\n"
        f"Regression channel: slope {ind.get('chan_slope')}, fit r2 {ind.get('chan_r2')}, "
        f"price position {ind.get('chan_pos')} (0=lower band, 1=upper band)\n"
        f"Volatility (recent ATR / baseline): {ind.get('vol_atr_ratio')}\n"
        f"Swing structure: {ind.get('structure')} (1=up, -1=down, 0=range), change-of-character {ind.get('choch')}\n"
        f"Volume trend: {ind.get('vol_trend')} (1=expanding, -1=fading)\n"
        "Classify the texture of this ambiguous regime."
    )


def interpret_regime(symbol: str, ind: dict, technical, tf: str) -> "RegimeRead | None":
    """Classify the ambiguous-regime texture (category + evidence + confidence). Returns None when no
    LLM is configured/available or the call fails — the caller then keeps the deterministic regime.
    Cached briefly per (symbol, tf) so a scan doesn't re-ask within the same window."""
    if not llm_available():
        return None
    key = (symbol, tf)
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit and (now - hit[0]) < _TTL_SEC:
        return hit[1]
    read = analyze(system=_SYSTEM, user=_snapshot(symbol, ind, technical, tf),
                   schema=RegimeRead, max_tokens=500)
    if read is None:
        return None
    read.confidence = max(0.0, min(1.0, float(read.confidence)))
    _CACHE[key] = (now, read)
    log.info("regime classified", extra={"symbol": symbol, "tf": tf,
                                         "category": read.category, "confidence": read.confidence})
    return read
