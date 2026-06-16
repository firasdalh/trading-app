"""Pure indicator math used by the deterministic Technical Analyst and the backtester.

Plain Python (no pandas dependency) so it's trivially testable and fast on small windows.
"""
from __future__ import annotations

from app.models.schemas import Candle


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Classic RSI. Returns None if not enough data."""
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def swing_levels(candles: list[Candle], lookback: int = 20) -> tuple[float | None, float | None]:
    """Return (support, resistance) from the recent window: min low / max high."""
    window = candles[-lookback:] if candles else []
    if not window:
        return None, None
    support = min(c.low for c in window)
    resistance = max(c.high for c in window)
    return round(support, 4), round(resistance, 4)


def market_structure(candles: list[Candle], left: int = 3, right: int = 3) -> dict:
    """Read market structure the way a chart trader does — from the sequence of swing pivots.

    A *swing high* is a bar whose high is strictly above the ``left`` bars before and ``right``
    bars after it (a local peak); a *swing low* is the mirror. We then classify the trend from
    the last two of each:
      - higher-high AND higher-low  -> "up"   (a real uptrend's footprint)
      - lower-high  AND lower-low   -> "down"
      - anything mixed              -> "range"
    ``choch`` (change-of-character) is True when the latest close breaks back through the most
    recent opposing swing — the earliest warning that the trend may be turning.

    Returns: {structure, swing_high, swing_low, choch}. The newest pivot is necessarily ``right``
    bars old (a peak isn't confirmed until price turns), so nothing here repaints.
    """
    n = len(candles)
    out = {"structure": "range", "swing_high": None, "swing_low": None, "choch": False}
    if n < left + right + 3:
        return out

    sh: list[float] = []  # swing-high prices, oldest-first
    sl: list[float] = []  # swing-low prices, oldest-first
    for i in range(left, n - right):
        h = candles[i].high
        if all(h > candles[j].high for j in range(i - left, i)) and all(
            h > candles[j].high for j in range(i + 1, i + right + 1)
        ):
            sh.append(h)
        lo = candles[i].low
        if all(lo < candles[j].low for j in range(i - left, i)) and all(
            lo < candles[j].low for j in range(i + 1, i + right + 1)
        ):
            sl.append(lo)

    if len(sh) >= 2 and len(sl) >= 2:
        if sh[-1] > sh[-2] and sl[-1] > sl[-2]:
            out["structure"] = "up"
        elif sh[-1] < sh[-2] and sl[-1] < sl[-2]:
            out["structure"] = "down"

    last_sh = sh[-1] if sh else None
    last_sl = sl[-1] if sl else None
    out["swing_high"] = round(last_sh, 6) if last_sh is not None else None
    out["swing_low"] = round(last_sl, 6) if last_sl is not None else None

    last_close = candles[-1].close
    if out["structure"] == "up" and last_sl is not None and last_close < last_sl:
        out["choch"] = True
    elif out["structure"] == "down" and last_sh is not None and last_close > last_sh:
        out["choch"] = True
    return out


def _swing_pivots(candles: list[Candle], left: int, right: int, start: int) -> tuple[list[int], list[int]]:
    """Indices of confirmed swing-high and swing-low pivots from ``start`` onward."""
    highs: list[int] = []
    lows: list[int] = []
    for i in range(max(left, start), len(candles) - right):
        h = candles[i].high
        if all(h > candles[j].high for j in range(i - left, i)) and all(
            h > candles[j].high for j in range(i + 1, i + right + 1)
        ):
            highs.append(i)
        lo = candles[i].low
        if all(lo < candles[j].low for j in range(i - left, i)) and all(
            lo < candles[j].low for j in range(i + 1, i + right + 1)
        ):
            lows.append(i)
    return highs, lows


def divergence(candles: list[Candle], left: int = 3, right: int = 3, lookback: int = 60) -> dict:
    """RSI divergence between the last two swing pivots — the classic momentum-vs-price tell.

    Regular (reversal):  bear = higher price high + lower RSI high (uptrend exhausting);
                         bull = lower price low + higher RSI low (downtrend exhausting).
    Hidden (continuation): bear_hidden = lower high + higher RSI high (downtrend resuming);
                           bull_hidden = higher low + lower RSI low (uptrend resuming).
    RSI is taken on closes UP TO each pivot, so nothing repaints. Returns four bool flags.
    """
    out = {"bull": False, "bear": False, "bull_hidden": False, "bear_hidden": False}
    n = len(candles)
    if n < left + right + 5:
        return out
    closes = [c.close for c in candles]
    start = max(left, n - lookback)
    highs, lows = _swing_pivots(candles, left, right, start)

    def _rsi_at(i: int) -> float | None:
        return rsi(closes[: i + 1])

    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        ra, rb = _rsi_at(a), _rsi_at(b)
        if ra is not None and rb is not None:
            if candles[b].high > candles[a].high and rb < ra:
                out["bear"] = True
            elif candles[b].high < candles[a].high and rb > ra:
                out["bear_hidden"] = True
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        ra, rb = _rsi_at(a), _rsi_at(b)
        if ra is not None and rb is not None:
            if candles[b].low < candles[a].low and rb > ra:
                out["bull"] = True
            elif candles[b].low > candles[a].low and rb < ra:
                out["bull_hidden"] = True
    return out


def reference_levels(daily_candles: list[Candle]) -> dict:
    """Institutional reference levels from DAILY candles: the prior completed day's and week's
    high/low — the levels desks actually watch. The last daily bar is the (forming) current day,
    so 'prior day' is the second-to-last bar; 'prior week' is the most recent COMPLETED ISO week.
    """
    out: dict[str, float] = {}
    n = len(daily_candles)
    if n >= 2:
        prev_day = daily_candles[-2]
        out["prior_day_high"] = round(prev_day.high, 6)
        out["prior_day_low"] = round(prev_day.low, 6)
    weeks: dict[tuple[int, int], dict[str, float]] = {}
    order: list[tuple[int, int]] = []
    for c in daily_candles:
        wk = c.ts.isocalendar()[:2]  # (iso-year, iso-week)
        if wk not in weeks:
            weeks[wk] = {"high": c.high, "low": c.low}
            order.append(wk)
        else:
            weeks[wk]["high"] = max(weeks[wk]["high"], c.high)
            weeks[wk]["low"] = min(weeks[wk]["low"], c.low)
    if len(order) >= 2:
        prev_week = weeks[order[-2]]  # the week before the current (forming) one
        out["prior_week_high"] = round(prev_week["high"], 6)
        out["prior_week_low"] = round(prev_week["low"], 6)
    return out


def trend_from_smas(closes: list[float]) -> str:
    """Classify trend from fast/slow SMA relationship + last-close position."""
    fast = sma(closes, 10) or sma(closes, min(len(closes), 5))
    slow = sma(closes, 50) or sma(closes, min(len(closes), 20))
    if fast is None or slow is None:
        return "sideways"
    spread = (fast - slow) / slow if slow else 0.0
    if spread > 0.002:
        return "up"
    if spread < -0.002:
        return "down"
    return "sideways"


# --------------------------------------------------------------------------- #
#  EMA / ATR / ADX / MACD / Bollinger / volume — the indicator bundle
# --------------------------------------------------------------------------- #


def _ema_full(values: list[float], period: int) -> list[float | None]:
    """EMA aligned to ``values`` (None during warmup, seeded with the SMA)."""
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period or period <= 0:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def ema(values: list[float], period: int) -> float | None:
    series = _ema_full(values, period)
    return round(series[-1], 6) if series and series[-1] is not None else None


def _rma(values: list[float], period: int) -> list[float]:
    """Wilder's smoothing (running moving average). Returns len(values)-period+1 points."""
    if len(values) < period or period <= 0:
        return []
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Average True Range — the volatility used for stop distance + position sizing."""
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, lo, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    rma = _rma(trs, period)
    return round(rma[-1], 6) if rma else None


def adx(candles: list[Candle], period: int = 14) -> dict | None:
    """Wilder's ADX with +DI/-DI. ADX < ~20 => weak/no trend (chop)."""
    if len(candles) < 2 * period + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        h, lo, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))

    atr_s, pdm_s, mdm_s = _rma(trs, period), _rma(plus_dm, period), _rma(minus_dm, period)
    if not atr_s:
        return None
    pdi: list[float] = []
    mdi: list[float] = []
    dx: list[float] = []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a == 0:
            pdi.append(0.0); mdi.append(0.0); dx.append(0.0); continue
        pd, md = 100 * p / a, 100 * m / a
        pdi.append(pd); mdi.append(md)
        s = pd + md
        dx.append(100 * abs(pd - md) / s if s else 0.0)
    adx_s = _rma(dx, period)
    if not adx_s:
        return None
    return {"adx": round(adx_s[-1], 2), "plus_di": round(pdi[-1], 2), "minus_di": round(mdi[-1], 2)}


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD line, signal line, histogram (momentum/trend confirmation)."""
    if len(closes) < slow + signal:
        return None
    ef, es = _ema_full(closes, fast), _ema_full(closes, slow)
    macd_line = [(a - b) for a, b in zip(ef, es) if a is not None and b is not None]
    if len(macd_line) < signal:
        return None
    sig = _ema_full(macd_line, signal)
    macd_last, signal_last = macd_line[-1], sig[-1]
    if signal_last is None:
        return None
    return {
        "macd": round(macd_last, 6),
        "signal": round(signal_last, 6),
        "hist": round(macd_last - signal_last, 6),
    }


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> dict | None:
    """Bollinger Bands (volatility regime / squeeze / mean-reversion context)."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper, lower = mid + mult * std, mid - mult * std
    return {
        "upper": round(upper, 6), "mid": round(mid, 6), "lower": round(lower, 6),
        "width": round((upper - lower) / mid, 4) if mid else 0.0,
    }


def volume_ratio(candles: list[Candle], period: int = 20) -> float | None:
    """Last volume relative to its average (participation/confirmation)."""
    vols = [c.volume for c in candles]
    if len(vols) < period:
        return None
    avg = sum(vols[-period:]) / period
    return round(vols[-1] / avg, 2) if avg else None


def trend_from_emas(closes: list[float]) -> str:
    """Trend from EMA stack (20 vs 50 vs 200 when available)."""
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    if e20 is None or e50 is None:
        return trend_from_smas(closes)
    if e200 is not None:
        if e20 > e50 > e200:
            return "up"
        if e20 < e50 < e200:
            return "down"
    spread = (e20 - e50) / e50 if e50 else 0.0
    if spread > 0.001:
        return "up"
    if spread < -0.001:
        return "down"
    return "sideways"
