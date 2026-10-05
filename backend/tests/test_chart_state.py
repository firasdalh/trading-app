"""Tests for the chart-state read (app/agents/chart_state.py).

The regression at the bottom is the one that matters most: the first working version of this
classifier called EVERY instrument a breakout, because `swing_levels` returns (support, resistance)
and it was unpacked as (high, low). A classifier that says the same thing about every chart is
worse than none, and it looked plausible until it was run across nine symbols at once.
"""
from __future__ import annotations

import datetime as dt

from app.agents.chart_state import read_chart_state
from app.models.schemas import Candle

T0 = dt.datetime(2026, 9, 29, tzinfo=dt.timezone.utc)


def C(i: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Candle:
    return Candle(ts=T0 + dt.timedelta(hours=i), open=o, high=h, low=l, close=c, volume=v)


def flat(n: int, px: float = 100.0, width: float = 1.0) -> list[Candle]:
    """A quiet, choppy base so every test starts from a defined range.

    Deliberately NOT a clean alternation: bars of identical size that flip direction every candle
    form a textbook engulfing pattern on every single bar, so the first version of this helper made
    a flat market read as a rejection. Varied bodies from a fixed pseudo-random sequence keep it
    deterministic without being pathological.
    """
    out = []
    seed = 7
    for i in range(n):
        seed = (seed * 1103515245 + 12345) % 2147483648
        r = (seed / 2147483648.0) - 0.5              # -0.5 .. +0.5
        o = px + r * width * 0.6
        c = px - r * width * 0.35
        hi = max(o, c) + abs(r) * width * 0.3
        lo = min(o, c) - abs(r) * width * 0.3
        out.append(C(i, o, hi, lo, c))
    return out


def ramp(start_i: int, frm: float, to: float, n: int, vol: float = 1000.0) -> list[Candle]:
    step = (to - frm) / n
    out = []
    for i in range(n):
        o = frm + step * i
        c = o + step
        out.append(C(start_i + i, o, max(o, c) + abs(step) * 0.2, min(o, c) - abs(step) * 0.2, c, vol))
    return out


def test_quiet_market_reads_as_range_not_breakout():
    s = read_chart_state(flat(60))
    assert s.state == "range"
    assert s.trigger is not None and s.invalidation is not None


def test_decisive_new_high_reads_as_breakout():
    candles = flat(50) + ramp(50, 100.0, 112.0, 6, vol=3000.0)
    s = read_chart_state(candles)
    assert s.state == "breakout"
    assert s.target_level is None or s.target_level > candles[-1].close
    assert "above" in s.expected or "holds" in s.expected


def test_decisive_new_low_reads_as_breakdown():
    candles = flat(50) + ramp(50, 100.0, 88.0, 6, vol=3000.0)
    s = read_chart_state(candles)
    assert s.state == "breakdown"


def test_pullback_reports_a_retrace_fraction():
    """A real pullback is SHALLOW and slow enough not to flip the trend.

    The first version of this fixture dropped 10 points in 6 bars after rising 30 in 20 - steeper
    down than up - which flips SuperTrend and is a new downtrend, not a pullback. That is the
    distinction the classifier is supposed to make, so the fixture has to respect it.
    """
    candles = flat(30) + ramp(30, 100.0, 130.0, 25) + ramp(55, 130.0, 126.5, 4)
    s = read_chart_state(candles)
    assert s.state in ("pullback", "rejection", "reversal")
    if s.state == "pullback":
        assert 0.0 < s.retrace_frac < 0.786
        assert s.trigger is not None and s.invalidation is not None
        assert s.depth_atr is not None


def test_state_is_never_a_trade_instruction():
    """Descriptive only. If any of these words appear as a verdict, the read-out has become a
    signal — which is how six earlier ideas in this codebase died."""
    for candles in (flat(60), flat(50) + ramp(50, 100.0, 112.0, 6)):
        s = read_chart_state(candles)
        payload = s.as_dict()
        assert set(payload) >= {"state", "trend", "trigger", "invalidation", "expected"}
        for banned in ("buy", "sell", "signal", "recommendation", "action", "direction"):
            assert banned not in payload, f"chart state must not expose {banned!r}"
        assert s.state in ("impulse", "pullback", "rejection", "breakout", "breakdown",
                           "range", "reversal", "unclear")


def test_short_history_degrades_quietly():
    s = read_chart_state(flat(10))
    assert s.state == "unclear" and "not enough" in s.headline


def test_not_every_chart_is_a_breakout():
    """THE REGRESSION. swing_levels returns (support, resistance); unpacking it the other way round
    compared every close against the range LOW, so nine different instruments all read 'breakout'."""
    shapes = {
        "quiet": flat(60),
        "up": flat(30) + ramp(30, 100.0, 130.0, 25),
        "down": flat(30) + ramp(30, 100.0, 75.0, 25),
        "pullback": flat(30) + ramp(30, 100.0, 130.0, 20) + ramp(50, 130.0, 118.0, 6),
    }
    states = {k: read_chart_state(v).state for k, v in shapes.items()}
    assert len(set(states.values())) > 1, f"classifier collapsed to one state: {states}"
    assert states["quiet"] != "breakout", states


# ---------------------------------------------------- the engine filter


def test_pattern_side_is_mechanical_not_a_forecast():
    """`pattern_side` records how the pattern RESOLVED, which is a fact about the last bar. The
    engine is allowed to refuse a trade that contradicts it, never to open one because of it."""
    up = read_chart_state(flat(50) + ramp(50, 100.0, 112.0, 6))
    dn = read_chart_state(flat(50) + ramp(50, 100.0, 88.0, 6))
    assert up.pattern_side == "up" and dn.pattern_side == "down"
    quiet = read_chart_state(flat(60))
    assert quiet.state == "range" and quiet.pattern_side is None    # nothing to contradict


def test_price_action_gate_is_not_wired_into_the_engine():
    """It was built, measured and rejected (2026-09-29): per-trade +0.125R vs +0.109R but TOTAL
    +128.8R vs +134.6R, OOS worse, and the fold band blew out from +0.082..+0.133 to
    +0.025..+0.286. The classifier remains a read-out. This test stops it being quietly re-wired
    without a fresh measurement."""
    from app.agents import orchestrator

    assert "price_action" not in orchestrator.DET_FILTER_KEYS
    import inspect
    src = inspect.getsource(orchestrator._deterministic_decision)
    assert "read_chart_state" not in src, "chart state must not gate the engine without a holdout"
