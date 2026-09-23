"""Measured odds: the base rate that says whether a plan's levels are worth taking.

Confidence grades the setup; this counts what the stop/target DISTANCES were historically worth on
the symbol itself. It is a read-out — it must be honest, cheap and never break an analysis.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.agents.odds import barrier_odds
from app.models.schemas import Candle

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _series(closes: list[float], wick: float = 0.0005) -> list[Candle]:
    return [Candle(ts=T0 + timedelta(hours=i), open=c, high=c * (1 + wick), low=c * (1 - wick),
                   close=c, volume=1000.0) for i, c in enumerate(closes)]


def _walk(n=3000, seed=7, drift=0.0, step=0.002) -> list[Candle]:
    rnd = random.Random(seed)
    px = 100.0
    out = [px]
    for _ in range(n - 1):
        px *= 1 + drift + rnd.uniform(-step, step)
        out.append(px)
    return _series(out)


def test_a_further_target_is_reached_first_less_often():
    """The whole point, stated so it holds on ANY tape: pushing the target further away (same stop)
    can only lower the share of times it is reached first. A single random series can drift either
    way, so the test asserts the ordering, not a number."""
    c = _walk()
    near = barrier_odds(c, direction="long", entry=100.0, stop=99.0, target=100.5)
    same = barrier_odds(c, direction="long", entry=100.0, stop=99.0, target=101.0)
    far = barrier_odds(c, direction="long", entry=100.0, stop=99.0, target=103.0)
    assert near["p_target"] > same["p_target"] > far["p_target"]
    assert all(abs(o["p_target"] + o["p_stop"] - 1.0) < 1e-9 for o in (near, same, far))
    assert near["samples"] > 250      # sampled every other bar (see _STRIDE)


def test_barriers_are_measured_on_the_real_path_not_assumed_even():
    """A driftless coin flip is an assumption; this reports what the series actually did. On a walk
    that fell 100 -> 88, a long's 1% target was reached first well under half the time."""
    o = barrier_odds(_walk(), direction="long", entry=100.0, stop=99.0, target=101.0)
    assert o is not None and o["p_target"] < 0.5


def test_a_rising_market_favours_the_long():
    """Same distances, tape with an upward drift -> the long's target is reached first far more often
    (and the short's version of the same levels is the mirror image)."""
    up = _walk(drift=0.0009, step=0.0015, seed=11)
    long_o = barrier_odds(up, direction="long", entry=100.0, stop=99.0, target=101.0)
    short_o = barrier_odds(up, direction="short", entry=100.0, stop=101.0, target=99.0)
    assert long_o["p_target"] > 0.60
    # The short is judged only on the falling stretches of the same series, so it isn't 1 - long.
    assert short_o is None or short_o["p_target"] < long_o["p_target"]


def test_edge_r_is_the_expectancy_those_odds_imply():
    o = barrier_odds(_walk(), direction="long", entry=100.0, stop=99.0, target=103.0)   # 3:1
    assert o is not None
    assert abs(o["edge_r"] - (o["p_target"] * 3.0 - o["p_stop"])) < 0.02


def test_refuses_to_guess_without_enough_history_or_valid_levels():
    assert barrier_odds(_walk(n=100), direction="long", entry=100.0, stop=99.0, target=101.0) is None
    c = _walk()
    assert barrier_odds(c, direction="long", entry=100.0, stop=101.0, target=102.0) is None  # stop above entry
    assert barrier_odds(c, direction="long", entry=100.0, stop=99.0, target=99.5) is None    # target below entry
    assert barrier_odds([], direction="long", entry=100.0, stop=99.0, target=101.0) is None


def test_odds_for_proposal_never_breaks_the_analysis():
    """A broker/data failure must return None, not raise — it's a read-out on the hot path."""
    from types import SimpleNamespace

    from app.agents.odds import odds_for_proposal

    class _Broken:
        name = "x"

        def get_ohlcv(self, *a, **k):
            raise RuntimeError("no data")

    prop = SimpleNamespace(symbol="X", direction=SimpleNamespace(value="long"), entry=100.0,
                           stop_loss=99.0, take_profit=103.0)
    assert odds_for_proposal(_Broken(), prop, "1h") is None
    aside = SimpleNamespace(symbol="X", direction=SimpleNamespace(value="no_trade"), entry=None,
                            stop_loss=None, take_profit=None)
    assert odds_for_proposal(_Broken(), aside, "1h") is None
