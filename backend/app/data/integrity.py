"""Task 10 — data-feed integrity checks.

Sanity-checks a candle series BEFORE it reaches the funnel, so corrupted input (bad ticks, feed
gaps, stale prices) can't silently poison ATR-based stop sizing or swing/structure detection. Four
checks:
  - ``ohlc_invalid`` / ``nonpositive`` — a bar that isn't internally consistent (H<L, close outside
    [L,H]) or has a non-positive price.
  - ``anomalous_range`` — a bar whose high-low range is a wild multiple of recent ATR (a spike / bad
    tick that would blow up an ATR stop).
  - ``stale`` — a run of identical consecutive closes (a frozen / stale feed).
  - ``gap`` — an irregular time gap vs the series' own cadence (missing bars), excluding normal
    weekend/session gaps.

Self-contained (no agent imports) so the data layer doesn't depend on the strategy layer. Default
use is LOG-ONLY (observational) — it never drops data on its own; hard-rejection is a separate,
flagged live-path decision.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.core.logging import get_logger
from app.models.schemas import Candle, OHLCVSeries

log = get_logger("data.integrity")

# Thresholds (deliberately loose — flag the clearly-broken, not normal volatility).
_ATR_PERIOD = 14
_MAX_RANGE_ATR = 6.0      # a single bar wider than 6x ATR is almost certainly a bad tick
_STALE_RUN = 4            # >= 4 identical consecutive closes = frozen feed
_GAP_FACTOR = 2.5         # a gap > 2.5x the median cadence = missing bars...
_WEEKEND_SECONDS = 2 * 24 * 3600  # ...unless it's a normal weekend/holiday-sized gap


@dataclass
class DataIssue:
    kind: str      # ohlc_invalid | nonpositive | anomalous_range | stale | gap
    index: int     # bar index in the series (0 = oldest)
    detail: str


def _atr(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def check_candles(candles: list[Candle], *, max_range_atr: float = _MAX_RANGE_ATR,
                  stale_run: int = _STALE_RUN, gap_factor: float = _GAP_FACTOR) -> list[DataIssue]:
    """Return the list of integrity issues found (empty = clean). Pure function, no side effects."""
    issues: list[DataIssue] = []
    n = len(candles)
    if n == 0:
        return issues

    # 1. per-bar validity
    for i, c in enumerate(candles):
        if min(c.open, c.high, c.low, c.close) <= 0:
            issues.append(DataIssue("nonpositive", i, f"non-positive OHLC at {c.ts}"))
        elif c.high < c.low or not (c.low <= c.open <= c.high) or not (c.low <= c.close <= c.high):
            issues.append(DataIssue("ohlc_invalid", i,
                                    f"inconsistent OHLC at {c.ts} (H{c.high} L{c.low} O{c.open} C{c.close})"))

    # 2. anomalous range vs ATR
    atr = _atr(candles, _ATR_PERIOD)
    if atr and atr > 0:
        for i, c in enumerate(candles):
            rng = c.high - c.low
            if rng > max_range_atr * atr:
                issues.append(DataIssue("anomalous_range", i,
                                        f"bar range {rng:.6g} > {max_range_atr:g}x ATR({atr:.6g}) at "
                                        f"{c.ts} — possible spike/bad tick"))

    # 3. stale prices — a run of identical consecutive closes
    run = 1
    for i in range(1, n):
        if candles[i].close == candles[i - 1].close:
            run += 1
            if run == stale_run:
                issues.append(DataIssue("stale", i, f"{stale_run} identical closes ending {candles[i].ts} "
                                        "— stale/frozen feed"))
        else:
            run = 1

    # 4. feed gaps — irregular spacing vs the series' own cadence (skip weekend-sized gaps)
    if n >= 3:
        deltas = [(candles[i].ts - candles[i - 1].ts).total_seconds() for i in range(1, n)]
        pos = sorted(d for d in deltas if d > 0)
        med = pos[len(pos) // 2] if pos else 0.0
        if med > 0:
            for i, d in enumerate(deltas, start=1):
                if med * gap_factor < d < _WEEKEND_SECONDS:
                    issues.append(DataIssue("gap", i, f"time gap {d/med:.1f}x the ~{med/60:.0f}min "
                                            f"cadence at {candles[i].ts} — missing bars"))
    return issues


def sanitize_candles(candles: list[Candle], *, max_range_atr: float = _MAX_RANGE_ATR,
                     ) -> tuple[list[Candle], list[DataIssue]]:
    """Return a REPAIRED copy of the series with the CLEAREST corruption neutralized, plus the list
    of what was repaired. Repairs only:
      - non-positive / inconsistent OHLC -> a flat bar at the prior close (garbage in, neutralized),
      - a single-bar SPIKE (range > max_range_atr x ATR) -> wicks clamped toward the real body.
    Soft issues (gaps, stale runs) are NOT touched — dropping/inventing bars there is riskier than the
    problem. Never adds or removes bars, so time alignment is preserved.
    """
    if not candles:
        return list(candles), []
    atr = _atr(candles, _ATR_PERIOD)
    # Robust "normal range" for the clamp TARGET — the median bar range is unmoved by the spike
    # itself, so we clamp back to a normal-looking bar (clamping to the ATR-based threshold wouldn't,
    # since one spike inflates ATR). Detection still uses ATR to match check_candles.
    ranges = sorted(c.high - c.low for c in candles if c.high >= c.low)
    med_range = ranges[len(ranges) // 2] if ranges else 0.0
    out = list(candles)
    repaired: list[DataIssue] = []
    for i, c in enumerate(out):
        invalid = (min(c.open, c.high, c.low, c.close) <= 0 or c.high < c.low
                   or not (c.low <= c.open <= c.high) or not (c.low <= c.close <= c.high))
        if invalid:
            ref = out[i - 1].close if i > 0 else (c.close if c.close > 0 else None)
            if ref and ref > 0:
                out[i] = Candle(ts=c.ts, open=ref, high=ref, low=ref, close=ref, volume=0.0)
                kind = "nonpositive" if min(c.open, c.high, c.low, c.close) <= 0 else "ohlc_invalid"
                repaired.append(DataIssue(kind, i, f"repaired to flat bar @ {ref} at {c.ts}"))
                continue
        if atr and atr > 0 and (c.high - c.low) > max_range_atr * atr:
            target = 2.5 * med_range if med_range > 0 else max_range_atr * atr
            body_hi, body_lo = max(c.open, c.close), min(c.open, c.close)
            slack = max(0.0, (target - (body_hi - body_lo)) / 2.0)
            nh = min(c.high, body_hi + slack)
            nl = max(c.low, body_lo - slack)
            out[i] = Candle(ts=c.ts, open=c.open, high=nh, low=nl, close=c.close, volume=c.volume)
            repaired.append(DataIssue("anomalous_range", i, f"clamped spike wicks at {c.ts}"))
    return out, repaired


def repair_and_log(symbol: str, timeframe: str, series: OHLCVSeries) -> OHLCVSeries:
    """Repair the clearest corruption on a fresh series and log it; return the series to actually use.
    Soft issues that can't be safely repaired (gaps/stale) are logged but left intact."""
    try:
        fixed_candles, repaired = sanitize_candles(list(series.candles))
    except Exception as exc:  # noqa: BLE001 - never break a fetch
        log.warning("integrity repair failed", extra={"symbol": symbol, "tf": timeframe, "error": str(exc)})
        return series
    if repaired:
        log.warning("data-feed REPAIRED before funnel", extra={"symbol": symbol, "timeframe": timeframe,
                    "repaired": dict(Counter(r.kind for r in repaired)), "sample": repaired[0].detail})
        series = OHLCVSeries(symbol=series.symbol, timeframe=series.timeframe, candles=fixed_candles)
    soft = [i for i in check_candles(list(series.candles)) if i.kind in ("gap", "stale")]
    if soft:
        log.warning("data-feed integrity (soft, not repaired)", extra={"symbol": symbol,
                    "timeframe": timeframe, "counts": dict(Counter(i.kind for i in soft))})
    return series


def log_integrity(symbol: str, timeframe: str, series: OHLCVSeries) -> list[DataIssue]:
    """Check a freshly-fetched series and LOG a warning if anything's wrong. Never drops data —
    returns the issues so a caller can decide (hard-rejection is a separate, flagged decision)."""
    try:
        issues = check_candles(list(series.candles))
    except Exception as exc:  # noqa: BLE001 - integrity checking must never break a data fetch
        log.warning("integrity check failed", extra={"symbol": symbol, "tf": timeframe, "error": str(exc)})
        return []
    if issues:
        by_kind = dict(Counter(i.kind for i in issues))
        log.warning("data-feed integrity issues", extra={"symbol": symbol, "timeframe": timeframe,
                    "counts": by_kind, "sample": issues[0].detail})
    return issues
