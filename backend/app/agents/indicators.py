"""Pure indicator math used by the deterministic Technical Analyst and the backtester.

Plain Python (no pandas dependency) so it's trivially testable and fast on small windows.
"""
from __future__ import annotations

from datetime import timezone

from app.models.schemas import Candle


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI (RMA-smoothed over the whole series) — matches MT5/TradingView so the number on
    the chart agrees with the broker's. Returns None if not enough data."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else 0.0)
        losses.append(-change if change < 0 else 0.0)
    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)
    if not avg_gain or not avg_loss:
        return None
    ag, al = avg_gain[-1], avg_loss[-1]
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - (100 / (1 + rs)), 2)


def swing_levels(candles: list[Candle], lookback: int = 20) -> tuple[float | None, float | None]:
    """Return (support, resistance) from the recent window: min low / max high."""
    window = candles[-lookback:] if candles else []
    if not window:
        return None, None
    support = min(c.low for c in window)
    resistance = max(c.high for c in window)
    return round(support, 4), round(resistance, 4)


_WICK_BODY_MULT = 1.5   # a rejection wick must be >= this many times the candle body
_WICK_RANGE_FRAC = 0.5  # ...and >= this fraction of the whole bar's range


def rejection_candle(candles: list[Candle]) -> dict:
    """Reversal candle on the LAST completed bar — the price-action confirmation a chart trader
    waits for at an RSI extreme (the market showing sellers/buyers defended the level right here):

      rej_bear (1.0) = a bearish rejection (a shooting-star / long UPPER wick that closes in the
                       lower half, or a bearish ENGULFING) — confirms a SHORT at an overbought high.
      rej_bull (1.0) = a bullish rejection (a hammer / long LOWER wick that closes in the upper
                       half, or a bullish ENGULFING) — confirms a LONG at an oversold low.

    Zero flags when there's no clear rejection. Needs >= 2 bars. Pure price action, no lookahead
    (only the last completed bar and the one before it)."""
    out = {"rej_bull": 0.0, "rej_bear": 0.0}
    if len(candles) < 2:
        return out
    c, p = candles[-1], candles[-2]
    o, h, lo, cl = c.open, c.high, c.low, c.close
    rng = h - lo
    if rng <= 0:
        return out
    body = abs(cl - o)
    upper = h - max(o, cl)      # upper wick
    lower = min(o, cl) - lo     # lower wick
    mid = lo + 0.5 * rng
    # Pin bar / long-wick rejection: a dominant wick on one side and the close pushed to the far side.
    pin_bear = upper >= _WICK_BODY_MULT * body and upper >= _WICK_RANGE_FRAC * rng and cl <= mid
    pin_bull = lower >= _WICK_BODY_MULT * body and lower >= _WICK_RANGE_FRAC * rng and cl >= mid
    # Engulfing: the last body fully covers the prior (opposite-coloured) body.
    engulf_bear = cl < o and p.close > p.open and o >= p.close and cl <= p.open
    engulf_bull = cl > o and p.close < p.open and o <= p.close and cl >= p.open
    out["rej_bear"] = 1.0 if (pin_bear or engulf_bear) else 0.0
    out["rej_bull"] = 1.0 if (pin_bull or engulf_bull) else 0.0
    return out


def failed_break(candles: list[Candle], lookback: int = 4, prior_window: int = 20) -> dict:
    """A FAILED BREAK / trap — the multi-bar cousin of the rejection candle. Price sweeps BEYOND a
    prior level within the last ``lookback`` bars, then closes back on the original side (a bull/bear
    trap / stop-run). This catches the "fake break of resistance/support, then reverse a few bars
    later" that a single-bar rejection candle misses:

      fbreak_bear (1.0) = swept ABOVE a prior high (last ``lookback`` bars) but the last close is back
                          BELOW it — a failed upside break → a SHORT rejection.
      fbreak_bull (1.0) = swept BELOW a prior low but the last close is back ABOVE it — a failed
                          downside break → a LONG rejection.

    The "prior level" is the high/low of the ``prior_window`` bars BEFORE the recent sweep, so a fresh
    new-high/low that CLOSES beyond the level (a real break) does NOT flag — only a poke-and-reclaim."""
    out = {"fbreak_bull": 0.0, "fbreak_bear": 0.0}
    if len(candles) < lookback + 3:
        return out
    recent = candles[-lookback:]
    prior = candles[-(lookback + prior_window):-lookback]
    if not prior:
        return out
    prior_high = max(c.high for c in prior)
    prior_low = min(c.low for c in prior)
    swept_high = max(c.high for c in recent)
    swept_low = min(c.low for c in recent)
    last_close = candles[-1].close
    if swept_high > prior_high and last_close < prior_high:
        out["fbreak_bear"] = 1.0   # poked above prior resistance, closed back below = bull trap
    if swept_low < prior_low and last_close > prior_low:
        out["fbreak_bull"] = 1.0   # poked below prior support, closed back above = bear trap
    return out


def session_vwap(candles: list[Candle]) -> list[float | None]:
    """Volume-weighted average price, re-anchored at each TRADING day (00:00 UTC).

    VWAP is the reference institutions actually execute against, which is why price so often stalls
    or reverses at it — it is the day's "fair value". It resets daily: a VWAP dragged across a week
    is an average of prices nobody is trading around any more.

    IMPORTANT CAVEAT for this data feed: MT5 gives ``tick_volume`` (the number of price CHANGES in
    the bar), not traded contracts. So this is a tick-weighted average, and it tracks real VWAP well
    when activity and volume move together — which is usually, but not always. Treat it as a good
    approximation, not the exchange's official VWAP.
    """
    out: list[float | None] = []
    day: tuple[int, int, int] | None = None
    pv = 0.0   # running sum of typical-price x volume
    vv = 0.0   # running sum of volume
    for c in candles:
        ts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc)
        key = (ts.year, ts.month, ts.day)
        if key != day:
            day, pv, vv = key, 0.0, 0.0
        typical = (c.high + c.low + c.close) / 3.0
        w = c.volume if c.volume and c.volume > 0 else 1.0   # flat weight if the feed gives none
        pv += typical * w
        vv += w
        out.append(pv / vv if vv else None)
    return out


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


def regression_channel(candles: list[Candle], lookback: int = 60, k: float = 2.0) -> dict | None:
    """Linear-regression CHANNEL over the last ``lookback`` closes — an objective, reproducible stand-in
    for a hand-drawn trend line + price channel (no subjective pivot-picking).

    Fits a least-squares line to the closes; the channel is that mid-line ± ``k``× the residual std.
    The MID line is the diagonal trend; the UPPER band is dynamic (diagonal) resistance, the LOWER band
    dynamic support. Returns the values at the LAST bar plus:
      - ``pos``  : where the last close sits — 0 = at the lower band (support), 1 = at the upper band
                   (resistance), >1 = broke above, <0 = broke below.
      - ``slope``: price per bar (sign = up/down channel).
      - ``r2``   : fit quality 0–1 (how cleanly price is actually channelling; low = ignore it).
    ``None`` when there isn't enough data.
    """
    if len(candles) < 20:
        return None
    w = candles[-lookback:] if len(candles) >= lookback else candles[:]
    m = len(w)
    ys = [c.close for c in w]
    mean_x = (m - 1) / 2.0
    mean_y = sum(ys) / m
    sxx = sum((x - mean_x) ** 2 for x in range(m))
    if sxx == 0:
        return None
    slope = sum((x - mean_x) * (ys[x] - mean_y) for x in range(m)) / sxx
    intercept = mean_y - slope * mean_x
    resid = [ys[x] - (intercept + slope * x) for x in range(m)]
    std = (sum(r * r for r in resid) / m) ** 0.5
    mid = intercept + slope * (m - 1)
    upper, lower = mid + k * std, mid - k * std
    last = ys[-1]
    pos = (last - lower) / (upper - lower) if upper > lower else 0.5
    syy = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - (sum(r * r for r in resid) / syy) if syy > 0 else 0.0
    return {"slope": round(slope, 8), "mid": round(mid, 6), "upper": round(upper, 6),
            "lower": round(lower, 6), "std": round(std, 6), "pos": round(pos, 3), "r2": round(r2, 3)}


def pivot_levels(candles: list[Candle], ref: float, w: int = 3, per_side: int = 3,
                 gap_frac: float = 0.002) -> list[dict]:
    """Nearest swing-pivot support/resistance to ``ref``. A pivot high tops the ``w`` bars each side
    (mirror for a low); keeps the ``per_side`` closest above (resistance) / below (support), spaced at
    least ``gap_frac`` apart. Shared by the chart levels endpoint and the market-context read."""
    n = len(candles)
    highs = [candles[i].high for i in range(w, n - w)
             if all(candles[i - k].high <= candles[i].high and candles[i + k].high <= candles[i].high
                    for k in range(1, w + 1))]
    lows = [candles[i].low for i in range(w, n - w)
            if all(candles[i - k].low >= candles[i].low and candles[i + k].low >= candles[i].low
                   for k in range(1, w + 1))]
    gap = gap_frac * ref

    def pick(vals, above: bool) -> list[float]:
        cand = sorted((v for v in vals if (v > ref if above else v < ref)), reverse=not above)
        kept: list[float] = []
        for p in cand:
            if all(abs(p - q) > gap for q in kept):
                kept.append(p)
            if len(kept) >= per_side:
                break
        return kept

    return ([{"price": round(p, 8), "kind": "resistance"} for p in pick(highs, True)]
            + [{"price": round(p, 8), "kind": "support"} for p in pick(lows, False)])


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


ST_PERIOD = 10     # SuperTrend ATR period
ST_FACTOR = 2.7    # SuperTrend ATR multiplier (matches the chart overlay)


def supertrend_series(candles: list[Candle], period: int = ST_PERIOD,
                      mult: float = ST_FACTOR) -> dict:
    """Full per-bar SuperTrend: ``{"line": [...], "dir": [...]}`` aligned to ``candles`` (None line /
    0 dir during warmup). CAUSAL — bar i uses only bars <= i — so it's safe to index in a backtest.
    Wilder-smoothed ATR; identical to the chart's overlay."""
    n = len(candles)
    line: list[float | None] = [None] * n
    direction: list[int] = [0] * n
    if n < period + 1:
        return {"line": line, "dir": direction}

    def _tr(i: int) -> float:
        h, lo, pc = candles[i].high, candles[i].low, candles[i - 1].close
        return max(h - lo, abs(h - pc), abs(lo - pc))

    atr_i = [0.0] * n                          # Wilder ATR aligned to candle index (valid i >= period)
    atr_i[period] = sum(_tr(i) for i in range(1, period + 1)) / period
    for i in range(period + 1, n):
        atr_i[i] = (atr_i[i - 1] * (period - 1) + _tr(i)) / period

    final_upper = final_lower = 0.0
    prev_dir = 1
    for i in range(period, n):
        hl2 = (candles[i].high + candles[i].low) / 2
        bu = hl2 + mult * atr_i[i]
        bl = hl2 - mult * atr_i[i]
        if i == period:
            final_upper, final_lower, prev_dir = bu, bl, 1
            direction[i], line[i] = 1, round(bl, 6)
            continue
        prev_upper, prev_lower = final_upper, final_lower
        final_upper = bu if (bu < prev_upper or candles[i - 1].close > prev_upper) else prev_upper
        final_lower = bl if (bl > prev_lower or candles[i - 1].close < prev_lower) else prev_lower
        d = prev_dir
        if prev_dir == 1 and candles[i].close < final_lower:
            d = -1
        elif prev_dir == -1 and candles[i].close > final_upper:
            d = 1
        prev_dir = d
        direction[i] = d
        line[i] = round(final_lower if d == 1 else final_upper, 6)
    return {"line": line, "dir": direction}


def supertrend(candles: list[Candle], period: int = ST_PERIOD, mult: float = ST_FACTOR) -> dict | None:
    """SuperTrend — the LAST bar's ``{"line": band value, "dir": 1 up | -1 down}``, or None if there
    isn't enough data."""
    s = supertrend_series(candles, period, mult)
    if not s["line"] or s["line"][-1] is None:
        return None
    return {"line": s["line"][-1], "dir": s["dir"][-1]}


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
    return {"adx": round(adx_s[-1], 2), "plus_di": round(pdi[-1], 2), "minus_di": round(mdi[-1], 2),
            # Previous bar's ADX -> "is trend strength BUILDING or FADING?". ADX 30 and falling is a
            # trend running out of steam; ADX 24 and rising is often one starting. The level alone
            # can't tell them apart.
            "adx_prev": round(adx_s[-2], 2) if len(adx_s) >= 2 else None}


def ema_slope(closes: list[float], period: int, lookback: int = 20,
              atr_value: float | None = None) -> float | None:
    """How far the EMA has TRAVELLED over ``lookback`` bars, measured in ATRs.

    Price being above a flat EMA200 is not an uptrend — it's a range with the average in the middle.
    The slope separates the two. Normalising by ATR makes the number comparable across instruments
    (0.5 means the average moved half a typical bar's range per lookback window); without it a gold
    slope and a EURUSD slope aren't on the same scale. Returns None when there isn't enough history.
    """
    if lookback <= 0 or len(closes) < period + lookback:
        return None
    series = _ema_full(closes, period)
    now, then = series[-1], series[-1 - lookback]
    if now is None or then is None:
        return None
    if not atr_value or atr_value <= 0:
        return None
    return round((now - then) / atr_value, 4)


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


def macd_signals(candles, fast: int = 12, slow: int = 26, signal: int = 9,
                 div_lookback: int = 34) -> dict | None:
    """MACD with the two EARLY-turn signals used by the RSI-Over pullback entry:
      cross      : +1 = MACD line crossed ABOVE the signal on the last bar (bullish), -1 = crossed
                   BELOW (bearish), 0 = no fresh cross.
      div_bull   : 1.0 = bullish divergence (price made a LOWER low over the lookback but MACD made a
                   HIGHER low), else 0.0.
      div_bear   : 1.0 = bearish divergence (price HIGHER high, MACD LOWER high), else 0.0.
    Needs the candle series (highs/lows) to line MACD up against price swings. None if too little data."""
    closes = [c.close for c in candles]
    if len(closes) < slow + signal:
        return None
    ef, es = _ema_full(closes, fast), _ema_full(closes, slow)
    macd_line = [(a - b) for a, b in zip(ef, es) if a is not None and b is not None]
    if len(macd_line) < signal + 1:
        return None
    sig_full = _ema_full(macd_line, signal)
    pairs = [(m, s) for m, s in zip(macd_line, sig_full) if s is not None]
    if len(pairs) < 2:
        return None
    macd_arr = [p[0] for p in pairs]
    sig_arr = [p[1] for p in pairs]
    n = len(macd_arr)

    # fresh signal-line cross on the last bar
    cross = 0
    prev, cur = macd_arr[-2] - sig_arr[-2], macd_arr[-1] - sig_arr[-1]
    if prev <= 0 < cur:
        cross = 1
    elif prev >= 0 > cur:
        cross = -1

    # divergence: compare price swing vs MACD at that swing, older half vs recent half of the window.
    offset = len(candles) - n  # macd_arr[k] lines up with candles[offset + k]
    div_bull = div_bear = 0.0
    win = min(div_lookback, n)
    if win >= 6:
        idx = list(range(n - win, n))
        half = win // 2
        older, recent = idx[:half], idx[half:]
        hi = lambda k: candles[offset + k].high
        lo = lambda k: candles[offset + k].low
        oh, rh = max(older, key=hi), max(recent, key=hi)      # price-high bars, each half
        if hi(rh) > hi(oh) and macd_arr[rh] < macd_arr[oh]:
            div_bear = 1.0
        ol, rl = min(older, key=lo), min(recent, key=lo)      # price-low bars, each half
        if lo(rl) < lo(ol) and macd_arr[rl] > macd_arr[ol]:
            div_bull = 1.0

    return {"macd": round(macd_arr[-1], 6), "signal": round(sig_arr[-1], 6),
            "hist": round(cur, 6), "hist_prev": round(prev, 6),   # prev bar's histogram -> "is it expanding?"
            "cross": float(cross), "div_bull": div_bull, "div_bear": div_bear}


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
