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
from app.models.enums import AssetClass, Direction, ReviewDecision, TradingBias
from app.models.schemas import FundamentalRead, TechnicalRead, TradeProposal, TradeReviewLLM

log = get_logger("agents.orchestrator")

_REVIEW_SYSTEM = """You are a senior, risk-aware trader REVIEWING a trade setup produced by a
deterministic strategy. Your authority is strictly limited:
- You may ONLY confirm or veto. You CANNOT create a trade, change its direction, move the
  stop/target, or increase size. Position sizing and hard risk limits are handled by a
  separate deterministic Risk Manager downstream.
- CONFIRM when the setup is reasonable. Optionally LOWER the confidence (never raise it).
- VETO only with a concrete, risk-based reason: higher-timeframe trend conflict, an imminent
  high-impact event (stand_aside_windows), clear exhaustion / over-extension, poor reward:risk
  versus nearby structure, or thin/illiquid conditions.
Be conservative but not trigger-happy — a vague feeling is not a veto. Return strict JSON:
decision ("confirm"|"veto"), confidence (0-1), rationale, concerns[]."""


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


# Indicator gates for the deterministic decision.
_ADX_MIN = 20.0       # below this the market is ranging -> stand aside
_ADX_STRONG = 25.0
_ATR_STOP_MULT = 1.5  # protective stop = entry +/- 1.5 * ATR
_RR = 2.0             # reward:risk target
_RSI_OB = 75.0        # overbought / oversold caution thresholds
_RSI_OS = 25.0
_STRUCT_IGNORE = 0.5  # ignore overhead structure within 0.5R of entry (breakout zone)
_MIN_RR_TO_STRUCT = 1.0  # need >=1R of room to the next structure to take the trade
_MOM_ATR_FRAC = 0.10  # counter-momentum only "matters" when |MACD hist| >= 10% of ATR (noise gate)
_PULLBACK_ATR = 2.5   # price > this many ATR beyond EMA20 = stretched entry -> down-weight (a
                      # steady trend rides ~2.4 ATR from the lagging EMA, so only flag real spikes)

_TF_RANK = {"1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "4h": 6, "1d": 7}


def _trend_from_indicators(ind: dict, fallback: str = "sideways") -> str:
    """Derive trend from the COMPUTED EMAs (numbers), independent of any LLM text label.

    This keeps the deterministic strategy the source of truth even when an LLM produced the
    technical interpretation. Falls back to the provided label when EMAs aren't available.
    """
    e20, e50, e200 = ind.get("ema20"), ind.get("ema50"), ind.get("ema200")
    if e20 is not None and e50 is not None:
        if e200 is not None:
            if e20 > e50 > e200:
                return "up"
            if e20 < e50 < e200:
                return "down"
        if e20 > e50:
            return "up"
        if e20 < e50:
            return "down"
    return fallback


def _macro_tf(technical: TechnicalRead):
    """The highest-timeframe read available (the dominant context), or None."""
    best, best_rank = None, -1
    for tf in technical.timeframes:
        r = _TF_RANK.get(tf.timeframe, 0)
        if r > best_rank:
            best, best_rank = tf, r
    return best


def _macro_trend(technical: TechnicalRead) -> str:
    """Trend of the highest-timeframe read (the dominant context), from computed indicators."""
    best = _macro_tf(technical)
    if best is None:
        return "sideways"
    return _trend_from_indicators(best.indicators, best.trend)


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

    tf0 = technical.timeframes[0] if technical.timeframes else None
    ind = tf0.indicators if tf0 else {}
    trend = _trend_from_indicators(ind, tf0.trend if tf0 else "sideways")  # from computed EMAs
    macro = _macro_trend(technical)                                       # higher-timeframe context
    bias = fundamental.bias

    # --- ADX chop gate: don't trade a market with no trend ---
    adx_v = ind.get("adx")
    if adx_v is not None and adx_v < _ADX_MIN:
        base.rationale = f"Standing aside: ranging market (ADX {adx_v} < {_ADX_MIN:.0f})."
        return base

    # --- direction from trend + fundamental + MACD momentum + higher-timeframe alignment ---
    macd_hist = ind.get("macd_hist")
    rsi = ind.get("rsi14")
    pdi = ind.get("plus_di")
    mdi = ind.get("minus_di")
    atr_v = ind.get("atr14")
    # Counter-momentum only blocks entry if it's MEANINGFUL (>= 10% of ATR) — trivial noise is
    # ignored so we don't sit out forever on a flat histogram.
    mom_thresh = _MOM_ATR_FRAC * atr_v if atr_v else 0.0
    if trend == "up" and bias != TradingBias.BEARISH:
        if macd_hist is not None and macd_hist < -mom_thresh:
            # Trend up but momentum meaningfully down = pullback. Wait for the long trigger.
            base.watch = True
            base.rationale = (
                f"Uptrend pullback — momentum still down (MACD hist {macd_hist}, RSI {rsi}, "
                f"−DI {mdi} > +DI {pdi}). Waiting for momentum to turn back up before going long."
            )
            return base
        if macro == "down":
            base.rationale = "No confluence: higher-timeframe trend is DOWN — not buying into it."
            return base
        direction = Direction.LONG
    elif trend == "down" and bias != TradingBias.BULLISH:
        if macd_hist is not None and macd_hist > mom_thresh:
            base.watch = True
            base.rationale = (
                f"Downtrend pullback — momentum turning up (MACD hist {macd_hist}, RSI {rsi}, "
                f"+DI {pdi} > −DI {mdi}). Waiting for momentum to roll back down before going short."
            )
            return base
        if macro == "up":
            base.rationale = "No confluence: higher-timeframe trend is UP — not selling into it."
            return base
        direction = Direction.SHORT
    else:
        base.rationale = (
            f"No confluence: trend={trend}, fundamental bias={bias.value}. Sitting out."
        )
        return base

    entry = ind.get("last_close") or _last_close(technical)
    if entry is None or entry <= 0:
        base.rationale = "No usable entry price from technical read; sitting out."
        return base

    # --- overextension: entering far from the mean (EMA20) invites a mean-reversion bounce into
    # the stop. We DON'T hard-block (a healthy trend always rides above/below the lagging EMA, so
    # blocking would skip every trend) — instead we down-weight a stretched entry below. ---
    ema20 = ind.get("ema20")
    overextended = bool(ema20 and atr_v and (
        (direction == Direction.LONG and entry > ema20 + _PULLBACK_ATR * atr_v) or
        (direction == Direction.SHORT and entry < ema20 - _PULLBACK_ATR * atr_v)
    ))

    support = tf0.support_levels[0] if tf0 and tf0.support_levels else None
    resistance = tf0.resistance_levels[0] if tf0 and tf0.resistance_levels else None

    # --- ATR stop (tightened to structure when sensible) ---
    if direction == Direction.LONG:
        atr_stop = entry - _ATR_STOP_MULT * atr_v if atr_v else None
        stop = atr_stop if atr_stop is not None else (support if (support and support < entry) else entry * 0.98)
        if support is not None and atr_stop is not None and atr_stop < support < entry:
            stop = support
        risk = entry - stop
    else:
        atr_stop = entry + _ATR_STOP_MULT * atr_v if atr_v else None
        stop = atr_stop if atr_stop is not None else (resistance if (resistance and resistance > entry) else entry * 1.02)
        if resistance is not None and atr_stop is not None and entry < resistance < atr_stop:
            stop = resistance
        risk = stop - entry

    if risk <= 0:
        base.rationale = "Computed risk is non-positive; sitting out."
        return base

    # --- structure-aware target: don't aim through overhead structure ---
    # In a STRONG trend the nearest swing is the breakout level, not a barrier — let the move
    # run to a 2R ATR target. Only respect immediate structure in moderate/ranging trends.
    raw_target = entry + _RR * risk if direction == Direction.LONG else entry - _RR * risk
    target = raw_target
    struct_note = f"~{_RR:.0f}R"
    respect_structure = not (adx_v is not None and adx_v >= _ADX_STRONG)
    if respect_structure and direction == Direction.LONG and resistance is not None:
        # Only treat resistance as a ceiling if it's beyond the breakout zone but below 2R.
        if entry + _STRUCT_IGNORE * risk < resistance < raw_target:
            if (resistance - entry) < _MIN_RR_TO_STRUCT * risk:
                base.rationale = (
                    f"Too little room: resistance {round(resistance,5)} is < "
                    f"{_MIN_RR_TO_STRUCT:.0f}R above entry. Sitting out."
                )
                return base
            target = resistance
            struct_note = f"capped at resistance {round(resistance, 5)}"
    elif respect_structure and direction == Direction.SHORT and support is not None:
        if raw_target < support < entry - _STRUCT_IGNORE * risk:
            if (entry - support) < _MIN_RR_TO_STRUCT * risk:
                base.rationale = (
                    f"Too little room: support {round(support,5)} is < "
                    f"{_MIN_RR_TO_STRUCT:.0f}R below entry. Sitting out."
                )
                return base
            target = support
            struct_note = f"capped at support {round(support, 5)}"

    # --- confidence from multi-factor confluence ---
    conf = 0.3 + 0.2 * technical.confidence + 0.15 * fundamental.confidence
    if macro == trend:
        conf += 0.15  # higher-timeframe agrees
    if adx_v is not None and adx_v >= _ADX_STRONG:
        conf += 0.1
    vr = ind.get("vol_ratio")
    if vr is not None and vr > 1.2:
        conf += 0.1
    if macd_hist is not None and ((direction == Direction.LONG) == (macd_hist > 0)):
        conf += 0.05
    # Cross-timeframe momentum conflict: the higher-TF MACD pushing AGAINST the trade is a
    # lower-conviction signal (the XAU short was taken with 1h vs 4h MACD disagreeing).
    macro_tf = _macro_tf(technical)
    macro_macd = macro_tf.indicators.get("macd_hist") if macro_tf else None
    macro_conflict = macro_macd is not None and (
        (direction == Direction.LONG and macro_macd < 0) or
        (direction == Direction.SHORT and macro_macd > 0)
    )
    if macro_conflict:
        conf -= 0.1
    if overextended:
        conf -= 0.1  # stretched entry (mean-reversion bounce risk)
    rsi = ind.get("rsi14")
    if rsi is not None and ((direction == Direction.LONG and rsi >= _RSI_OB)
                            or (direction == Direction.SHORT and rsi <= _RSI_OS)):
        conf -= 0.1  # entering when already stretched
    e200 = ind.get("ema200")
    if e200:
        regime_ok = (direction == Direction.LONG and entry >= e200) or \
                    (direction == Direction.SHORT and entry <= e200)
        conf += 0.05 if regime_ok else -0.05
    confidence = round(max(0.05, min(0.95, conf)), 2)

    stop_basis = "ATR" if atr_v else "structure"
    base.direction = direction
    base.entry = round(entry, 6)
    base.stop_loss = round(stop, 6)
    base.take_profit = round(target, 6)
    base.confidence = confidence
    base.rationale = (
        f"Confluence {direction.value.upper()}: entry-TF trend={trend}, macro={macro}, "
        f"ADX {adx_v}, MACD hist={macd_hist}, RSI {rsi}, bias={bias.value}"
        f"{' (cross-TF momentum conflict)' if macro_conflict else ''}"
        f"{' (stretched entry)' if overextended else ''}. "
        f"Entry {base.entry}, stop {base.stop_loss} ({stop_basis} ~{_ATR_STOP_MULT}xATR), "
        f"target {base.take_profit} ({struct_note}). Deterministic (no LLM)."
    )
    return base


def run_orchestrator(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead,
    now: datetime | None = None, use_llm: bool = True,
) -> TradeProposal:
    """Deterministic engine decides; the LLM may only CONFIRM or VETO (never widen).

    1. The deterministic strategy (regime/MTF/ADX/momentum/ATR/structure gates) is the source
       of truth — it picks direction, entry, stop, target, and can say NO_TRADE.
    2. If it declined, we return NO_TRADE — the LLM cannot create a trade the rules reject.
    3. If it proposed a trade and the LLM is enabled, the LLM reviews it as a risk-aware
       second opinion: confirm (optionally lowering confidence) or veto with reasons. It can
       never change direction/levels or raise risk. The Risk Manager remains final downstream.
    """
    now = now or datetime.now(timezone.utc)

    proposal = _deterministic_decision(symbol, asset_class, timeframe, technical, fundamental, now)

    if proposal.direction == Direction.NO_TRADE or not use_llm or not llm_available():
        log.info("orchestrator decision (deterministic)",
                 extra={"symbol": symbol, "direction": proposal.direction.value})
        return proposal

    # --- LLM review of the deterministic setup (confirm / veto only) ---
    user = (
        f"PROPOSED SETUP (from the deterministic strategy — you may only confirm or veto):\n"
        f"  symbol={symbol} timeframe={timeframe} direction={proposal.direction.value}\n"
        f"  entry={proposal.entry} stop={proposal.stop_loss} target={proposal.take_profit} "
        f"confidence={proposal.confidence}\n  rationale={proposal.rationale}\n\n"
        f"TECHNICAL READ:\n{technical.model_dump_json(indent=2)}\n\n"
        f"FUNDAMENTAL READ:\n{fundamental.model_dump_json(indent=2)}\n\n"
        "Confirm if this is a reasonable setup; veto only with a concrete risk reason "
        "(higher-timeframe conflict, imminent high-impact news, exhaustion/over-extension, "
        "poor reward:risk vs structure, illiquid conditions)."
    )
    review = analyze(system=_REVIEW_SYSTEM, user=user, schema=TradeReviewLLM, max_tokens=2000)
    if review is None:
        return proposal  # LLM unavailable/failed -> trust the deterministic setup

    if review.decision == ReviewDecision.VETO:
        concerns = ("; ".join(review.concerns)) if review.concerns else review.rationale
        log.info("LLM vetoed deterministic setup", extra={"symbol": symbol, "reason": concerns[:120]})
        proposal.rationale = (
            f"{proposal.direction.value.upper()} setup VETOED by AI review: {review.rationale}"
            f"{' | concerns: ' + concerns if review.concerns else ''}"
        )
        proposal.direction = Direction.NO_TRADE
        proposal.entry = proposal.stop_loss = proposal.take_profit = None
        proposal.confidence = 0.0
        proposal.review_decision = "veto"
        return proposal

    # Confirmed: keep deterministic levels; the LLM may only LOWER confidence.
    proposal.confidence = round(min(proposal.confidence, review.confidence or proposal.confidence), 2)
    proposal.rationale = f"{proposal.rationale} | AI review CONFIRMED: {review.rationale}"
    proposal.review_decision = "confirm"
    log.info("LLM confirmed deterministic setup",
             extra={"symbol": symbol, "direction": proposal.direction.value, "confidence": proposal.confidence})
    return proposal
