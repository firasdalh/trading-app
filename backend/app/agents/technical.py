"""Technical Analyst agent.

Analyzes OHLCV *numbers* (never chart images) across one or more timeframes and produces a
structured ``TechnicalRead``. Uses Claude when a key is configured; otherwise falls back to
a deterministic indicator-based read so the pipeline runs offline.
"""
from __future__ import annotations

from app.agents.indicators import rsi, sma, swing_levels, trend_from_smas
from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.schemas import OHLCVSeries, TechnicalRead, TimeframeRead

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
    closes = [c.close for c in series.candles]
    support, resistance = swing_levels(series.candles, lookback=20)
    indicators: dict[str, float] = {}
    for p in (10, 20, 50):
        val = sma(closes, p)
        if val is not None:
            indicators[f"sma{p}"] = round(val, 4)
    r = rsi(closes)
    if r is not None:
        indicators["rsi14"] = r
    if closes:
        indicators["last_close"] = closes[-1]
    trend = trend_from_smas(closes)
    return TimeframeRead(
        timeframe=series.timeframe,
        trend=trend,
        support_levels=[support] if support is not None else [],
        resistance_levels=[resistance] if resistance is not None else [],
        indicators=indicators,
        patterns=[],
        comment=f"deterministic read: trend={trend}",
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


def run_technical(symbol: str, series_by_tf: list[OHLCVSeries]) -> TechnicalRead:
    """Produce a TechnicalRead, preferring the LLM and falling back to indicators."""
    if llm_available():
        user = "Analyze these timeframes and return a TechnicalRead.\n\n" + "\n\n".join(
            _series_summary(s) for s in series_by_tf
        )
        result = analyze(system=_SYSTEM, user=user, schema=TechnicalRead)
        if result is not None:
            # Ensure the symbol is set even if the model omitted it.
            result.symbol = result.symbol or symbol
            log.info("technical read via LLM", extra={"symbol": symbol, "trend": result.overall_trend})
            return result
    read = _deterministic_read(symbol, series_by_tf)
    log.info("technical read deterministic", extra={"symbol": symbol, "trend": read.overall_trend})
    return read
