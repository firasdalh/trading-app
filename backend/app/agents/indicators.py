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
