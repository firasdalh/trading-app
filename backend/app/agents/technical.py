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
    divergence,
    ema,
    macd,
    macd_signals,
    market_structure,
    reference_levels,
    regression_channel,
    rsi,
    supertrend_series,
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

_RECLAIM_LOOKBACK = 12  # bars used for recent_high/recent_low (failed-break / reclaim detection)

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
    if candles:
        # Last bar's wick extremes — used to detect a rejection at a range edge (high tagged the
        # level but the bar closed back off it) for the ranging mean-reversion confirmation.
        indicators["last_high"] = round(candles[-1].high, 6)
        indicators["last_low"] = round(candles[-1].low, 6)
        # Extremes over a recent window — used to detect a FAILED break (price pierced a level in
        # the recent past then traded back to the original side = a reclaim / bull-or-bear trap), so
        # the engine won't keep arming a break of a level it just whipsawed across.
        recent = candles[-_RECLAIM_LOOKBACK:]
        indicators["recent_high"] = round(max(c.high for c in recent), 6)
        indicators["recent_low"] = round(min(c.low for c in recent), 6)
    # Trend EMAs.
    for p in (10, 20, 50, 200):  # ema10 = the RSI-Over strategy's breakout-confirmation line
        val = ema(closes, p)
        if val is not None:
            indicators[f"ema{p}"] = val
    # EMA20 of highs / lows = a band around price (the SuperTrend-band breakout strategy enters on a
    # close beyond this band in the SuperTrend direction).
    eh, el = ema([c.high for c in candles], 20), ema([c.low for c in candles], 20)
    if eh is not None:
        indicators["ema20_high"] = eh
    if el is not None:
        indicators["ema20_low"] = el
    # SuperTrend (ATR 10 x2.7): trend direction + the line, plus "bars since the last flip" so the
    # breakout strategy can require a FRESH flip (early entry) rather than a late mid-trend break.
    sts = supertrend_series(candles)
    dirs = sts["dir"]
    if dirs and dirs[-1] != 0:
        indicators["supertrend"] = sts["line"][-1]
        indicators["supertrend_dir"] = float(dirs[-1])
        last_flip = None
        for k in range(1, len(dirs)):
            if dirs[k] != 0 and dirs[k - 1] != 0 and dirs[k] != dirs[k - 1]:
                last_flip = k
        if last_flip is not None:
            indicators["supertrend_bars_since_flip"] = float(len(dirs) - 1 - last_flip)
    # Momentum.
    r = rsi(closes)
    if r is not None:
        indicators["rsi14"] = r
        # Previous bar's RSI, so the engine can tell if momentum is TURNING back from an extreme
        # (a rejection cue) rather than just being extreme.
        rp = rsi(closes[:-1]) if len(closes) > 1 else None
        if rp is not None:
            indicators["rsi14_prev"] = rp
    m = macd(closes)
    if m is not None:
        indicators["macd"] = m["macd"]
        indicators["macd_signal"] = m["signal"]
        indicators["macd_hist"] = m["hist"]
    # MACD early-turn signals for the RSI-Over pullback entry (cross + divergence; needs candles).
    ms = macd_signals(candles)
    if ms is not None:
        indicators["macd_cross"] = ms["cross"]        # +1 bullish / -1 bearish / 0
        indicators["macd_div_bull"] = ms["div_bull"]  # 1.0 = bullish divergence
        indicators["macd_div_bear"] = ms["div_bear"]  # 1.0 = bearish divergence
        indicators["macd_hist_prev"] = ms["hist_prev"]  # previous bar's histogram (is momentum expanding?)
    # Volatility (stop sizing) + regime.
    a = atr(candles)
    if a is not None:
        indicators["atr14"] = a
        # Volatility expansion: recent ATR vs a longer baseline. >1 = expanding, <1 = contracting.
        # Self-referential (a ratio), so it flags regime change consistently across instruments.
        a_base = atr(candles, 50)
        if a_base and a_base > 0:
            indicators["vol_atr_ratio"] = round(a / a_base, 3)
    bb = bollinger(closes)
    if bb is not None:
        # Only bb_width is used now (the compression / squeeze read); the range-fade edges moved to
        # the regression price channel (chan_upper/chan_lower), so the BB bands are no longer stored.
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
    # Volume TREND (distinct from the spike ratio above): is participation expanding INTO the current
    # move or fading? Compare the last 3 bars' mean volume to the prior 5. +1 expanding / 0 flat /
    # -1 fading. A breakout on rising volume is real; a move on fading volume is running out of fuel.
    vols = [float(c.volume or 0.0) for c in candles]
    if len(vols) >= 8 and any(v > 0 for v in vols[-8:]):
        recent3 = sum(vols[-3:]) / 3.0
        prior5 = sum(vols[-8:-3]) / 5.0
        if prior5 > 0:
            ratio = recent3 / prior5
            indicators["vol_trend"] = 1.0 if ratio >= 1.15 else (-1.0 if ratio <= 0.85 else 0.0)
    # Market structure (swing highs/lows) — how a chart trader reads trend. Encoded numerically
    # (1=up / -1=down / 0=range) plus the latest swing levels and a change-of-character flag, so
    # the orchestrator can weight structure without a schema change.
    ms = market_structure(candles)
    indicators["structure"] = {"up": 1.0, "down": -1.0, "range": 0.0}[ms["structure"]]
    if ms["swing_high"] is not None:
        indicators["swing_high"] = ms["swing_high"]
    if ms["swing_low"] is not None:
        indicators["swing_low"] = ms["swing_low"]
    indicators["choch"] = 1.0 if ms["choch"] else 0.0

    # Regression CHANNEL (algorithmic diagonal trend line + price channel). Tells the engine WHERE
    # price sits vs dynamic support/resistance — e.g. a long firing right into the upper (resistance)
    # band, where a hand-drawn trend line would reject it. chan_pos: 0=lower/support band, 1=upper/
    # resistance band, >1/<0 = broke out; chan_r2 = how cleanly it's actually channelling.
    ch = regression_channel(candles)
    if ch is not None:
        indicators["chan_pos"] = ch["pos"]
        indicators["chan_slope"] = ch["slope"]
        indicators["chan_r2"] = ch["r2"]
        indicators["chan_upper"] = ch["upper"]
        indicators["chan_lower"] = ch["lower"]
        indicators["chan_mid"] = ch["mid"]

    # RSI divergence (momentum vs price at the last two swings) — encoded as 0/1 flags so the
    # orchestrator can weight it without a schema change.
    dv = divergence(candles)
    for k, v in dv.items():
        indicators[f"div_{k}"] = 1.0 if v else 0.0

    # Institutional reference levels (prior day/week high-low) — only meaningful on the daily TF;
    # the orchestrator reads them off the macro timeframe for entry/stop/target confluence.
    if series.timeframe in ("1d", "1w"):
        for k, v in reference_levels(candles).items():
            indicators[k] = v

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
        comment=f"deterministic read: trend={trend}, structure={ms['structure']}, "
                f"strength={strength} (ADX={adx_val})",
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
