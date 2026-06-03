"""Technical Analyst agent.

Analyzes OHLCV *numbers* (never chart images) across one or more timeframes and produces a
structured ``TechnicalRead``. Uses Claude when a key is configured; otherwise falls back to
a deterministic indicator-based read so the pipeline runs offline.
"""
from __future__ import annotations

from app.agents.indicators import (
    adx,
    atr,
    bollinger,
    ema,
    macd,
    rsi,
    swing_levels,
    trend_from_emas,
    volume_ratio,
)
from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.schemas import (
    OHLCVSeries,
    TechnicalRead,
    TechnicalReadLLM,
    TimeframeRead,
)

log = get_logger("agents.technical")

_SYSTEM = """You are a disciplined Technical Analyst on a trading desk. You receive OHLCV
numbers across one or more timeframes (never images). For each timeframe, identify the
trend, key support/resistance levels, relevant indicator readings, and notable price
patterns. Be objective and quantitative. Do not invent levels not supported by the data.
Express uncertainty honestly via the confidence field (0-1). Return strict JSON matching
the schema."""


def _series_summary(series: OHLCVSeries, max_rows: int = 60) -> str:
    rows = series.candles[-max_rows:]
    lines = [f"timeframe={series.timeframe} symbol={series.symbol} bars={len(rows)}"]
    for c in rows:
        lines.append(
            f"{c.ts.isoformat()} O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume}"
        )
    return "\n".join(lines)


def _deterministic_timeframe(series: OHLCVSeries) -> TimeframeRead:
    candles = series.candles
    closes = [c.close for c in candles]
    support, resistance = swing_levels(candles, lookback=20)
    indicators: dict[str, float] = {}

    if closes:
        indicators["last_close"] = round(closes[-1], 6)
    # Trend EMAs.
    for p in (20, 50, 200):
        val = ema(closes, p)
        if val is not None:
            indicators[f"ema{p}"] = val
    # Momentum.
    r = rsi(closes)
    if r is not None:
        indicators["rsi14"] = r
    m = macd(closes)
    if m is not None:
        indicators["macd"] = m["macd"]
        indicators["macd_signal"] = m["signal"]
        indicators["macd_hist"] = m["hist"]
    # Volatility (stop sizing) + regime.
    a = atr(candles)
    if a is not None:
        indicators["atr14"] = a
    bb = bollinger(closes)
    if bb is not None:
        indicators["bb_upper"] = bb["upper"]
        indicators["bb_mid"] = bb["mid"]
        indicators["bb_lower"] = bb["lower"]
        indicators["bb_width"] = bb["width"]
    # Trend strength (the chop gate).
    adx_v = adx(candles)
    if adx_v is not None:
        indicators["adx"] = adx_v["adx"]
        indicators["plus_di"] = adx_v["plus_di"]
        indicators["minus_di"] = adx_v["minus_di"]
    # Participation.
    vr = volume_ratio(candles)
    if vr is not None:
        indicators["vol_ratio"] = vr

    trend = trend_from_emas(closes)
    adx_val = indicators.get("adx")
    strength = "strong" if (adx_val and adx_val >= 25) else "weak" if (adx_val and adx_val < 20) else "moderate"
    return TimeframeRead(
        timeframe=series.timeframe,
        trend=trend,
        support_levels=[support] if support is not None else [],
        resistance_levels=[resistance] if resistance is not None else [],
        indicators=indicators,
        patterns=[],
        comment=f"deterministic read: trend={trend}, strength={strength} (ADX={adx_val})",
    )


def _deterministic_read(symbol: str, series_by_tf: list[OHLCVSeries]) -> TechnicalRead:
    tf_reads = [_deterministic_timeframe(s) for s in series_by_tf]
    # Overall trend = majority vote across timeframes (ties -> sideways).
    votes = {"up": 0, "down": 0, "sideways": 0}
    for tr in tf_reads:
        votes[tr.trend] = votes.get(tr.trend, 0) + 1
    overall = max(votes, key=votes.get) if tf_reads else "sideways"
    if list(votes.values()).count(votes[overall]) > 1 and votes[overall] == votes.get("sideways", 0):
        overall = "sideways"
    # Confidence scales with timeframe agreement.
    agree = votes.get(overall, 0) / len(tf_reads) if tf_reads else 0.0
    confidence = round(0.4 + 0.5 * agree, 2) if overall != "sideways" else round(0.3 * agree, 2)
    return TechnicalRead(
        symbol=symbol, timeframes=tf_reads, overall_trend=overall,
        confidence=confidence, notes="deterministic indicator-based analysis (no LLM)",
    )


def run_technical(symbol: str, series_by_tf: list[OHLCVSeries], use_llm: bool = True) -> TechnicalRead:
    """Produce a TechnicalRead, preferring the LLM and falling back to indicators.

    The LLM returns interpretation only (trend/levels/patterns/comment) via a Gemini-safe
    schema; we always attach the deterministically-computed indicators so downstream sizing
    (ATR), the ADX gate, and the chart have real numbers regardless of provider.
    """
    # Always compute indicators deterministically (cheap, accurate).
    det = _deterministic_read(symbol, series_by_tf)
    indicators_by_tf = {tf.timeframe: tf.indicators for tf in det.timeframes}

    if use_llm and llm_available():
        user = "Analyze these timeframes and return the interpretation.\n\n" + "\n\n".join(
            _series_summary(s) for s in series_by_tf
        )
        llm = analyze(system=_SYSTEM, user=user, schema=TechnicalReadLLM, max_tokens=4000)
        if llm is not None:
            timeframes = [
                TimeframeRead(
                    timeframe=tf.timeframe, trend=tf.trend,
                    support_levels=tf.support_levels, resistance_levels=tf.resistance_levels,
                    indicators=indicators_by_tf.get(tf.timeframe, {}),  # our computed numbers
                    patterns=tf.patterns, comment=tf.comment,
                )
                for tf in llm.timeframes
            ] or det.timeframes  # if the model returned none, keep the deterministic ones
            read = TechnicalRead(
                symbol=symbol, timeframes=timeframes, overall_trend=llm.overall_trend,
                confidence=llm.confidence, notes=llm.notes or "LLM interpretation + computed indicators",
            )
            log.info("technical read via LLM", extra={"symbol": symbol, "trend": read.overall_trend})
            return read

    log.info("technical read deterministic", extra={"symbol": symbol, "trend": det.overall_trend})
    return det
