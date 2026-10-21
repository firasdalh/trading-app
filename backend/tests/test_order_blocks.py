"""Tests for order-block detection (app/agents/order_blocks.py).

Two of these matter more than the rest:
  * `test_visible_at_hides_blocks_before_they_were_knowable` - the look-ahead guard. An order block
    is confirmed several bars AFTER its origin candle, so replaying with the full list hands the
    engine zones that had not been drawn yet. That is the exact error class that invalidated every
    backtest in this repo before the 2026-09 audit.
  * the NEGATIVE cases - a detector that fires on every other bar is why order blocks have a
    reputation for being useless.
"""
from __future__ import annotations

import datetime as dt

from app.agents.order_blocks import find_order_blocks, visible_at
from app.models.schemas import Candle

T0 = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)


def C(i, o, h, l, c, v=1000.0):
    return Candle(ts=T0 + dt.timedelta(hours=i), open=o, high=h, low=l, close=c, volume=v)


def chop(n, px=100.0, w=1.0, seed=7):
    """Quiet, varied noise - not a clean alternation (that makes textbook patterns on every bar)."""
    out = []
    for i in range(n):
        seed = (seed * 1103515245 + 12345) % 2147483648
        r = (seed / 2147483648.0) - 0.5
        o = px + r * w * 0.6
        c = px - r * w * 0.35
        out.append(C(i, o, max(o, c) + abs(r) * w * 0.3, min(o, c) - abs(r) * w * 0.3, c))
    return out


def bullish_setup():
    """Base, a dip (the origin candle), then an impulsive run that closes above the prior swing."""
    cs = chop(40, 100.0, 1.0)
    cs.append(C(40, 100.0, 100.2, 98.0, 98.2))            # <- origin: the last down candle
    px = 98.2
    for i in range(41, 50):                                # displacement away, with a gap
        o = px
        px += 1.6
        cs.append(C(i, o, px + 0.3, o - 0.05, px, 3000.0))
    return cs


def bearish_setup():
    cs = chop(40, 100.0, 1.0)
    cs.append(C(40, 100.0, 102.0, 99.8, 101.8))           # <- origin: the last up candle
    px = 101.8
    for i in range(41, 50):
        o = px
        px -= 1.6
        cs.append(C(i, o, o + 0.05, px - 0.3, px, 3000.0))
    return cs


def test_quiet_market_has_no_order_blocks():
    """The headline negative case: no displacement, no structure break, no blocks."""
    assert find_order_blocks(chop(120)) == []


def test_bullish_order_block_is_found_below_price():
    obs = find_order_blocks(bullish_setup())
    bulls = [o for o in obs if o.kind == "bullish"]
    assert bulls, "a clean impulse off a down candle through a swing high must produce a block"
    ob = bulls[0]
    assert ob.bottom <= 98.5 and ob.top <= 101.0          # the dip candle, not the rally
    assert ob.displacement_atr >= 1.2
    assert ob.confirmed_index > ob.origin_index           # confirmed only once the move happened


def test_bearish_order_block_is_found_above_price():
    obs = find_order_blocks(bearish_setup())
    bears = [o for o in obs if o.kind == "bearish"]
    assert bears
    assert bears[0].top >= 101.0


def test_drift_without_displacement_is_not_an_order_block():
    """Same shape, but the move away crawls. An order block without an impulse is a red candle."""
    cs = chop(40, 100.0, 1.0)
    cs.append(C(40, 100.0, 100.2, 99.6, 99.7))
    px = 99.7
    for i in range(41, 60):
        o = px
        px += 0.06                                        # far under the 1.2 ATR requirement
        cs.append(C(i, o, px + 0.02, o - 0.02, px))
    assert not [o for o in find_order_blocks(cs) if o.kind == "bullish" and o.origin_index == 40]


def test_move_that_breaks_no_structure_is_not_an_order_block():
    """A rally that stays under the prior swing high is a pullback, not a break of structure."""
    cs = chop(40, 100.0, 1.0)
    hi = max(c.high for c in cs)
    cs.append(C(40, 99.8, 100.0, 99.0, 99.1))
    px = 99.1
    for i in range(41, 48):
        o = px
        px += 0.1                                          # never reaches `hi`
        cs.append(C(i, o, px + 0.02, o - 0.02, px))
    assert max(c.close for c in cs) < hi                   # precondition: no break happened
    assert not [o for o in find_order_blocks(cs) if o.kind == "bullish" and o.origin_index == 40]


def test_one_long_visit_counts_as_one_touch():
    """Counting every overlapping BAR made a zone price drifted through read as many tests - one
    gold zone came back with 77 of them."""
    cs = bullish_setup()
    ob = [o for o in find_order_blocks(cs) if o.kind == "bullish"][0]
    mid = ob.mid
    n = len(cs)
    for k in range(12):                                    # price sits inside the zone for 12 bars
        cs.append(C(n + k, mid, mid + 0.05, mid - 0.05, mid))
    again = [o for o in find_order_blocks(cs) if o.kind == "bullish" and o.origin_index == ob.origin_index]
    assert again and again[0].touches == 1, f"expected one visit, got {again[0].touches if again else None}"
    assert again[0].mitigated is True


def test_close_through_the_far_side_invalidates():
    cs = bullish_setup()
    ob = [o for o in find_order_blocks(cs) if o.kind == "bullish"][0]
    n = len(cs)
    below = ob.bottom - 2.0
    for k in range(4):
        cs.append(C(n + k, below, below + 0.1, below - 0.1, below))
    survivors = [o for o in find_order_blocks(cs) if o.origin_index == ob.origin_index]
    assert not survivors, "a block price closed clean through must stop being drawn"
    dead = [o for o in find_order_blocks(cs, include_invalidated=True)
            if o.origin_index == ob.origin_index]
    assert dead and dead[0].invalidated and dead[0].strength <= 15


def test_visible_at_hides_blocks_before_they_were_knowable():
    """THE look-ahead guard.

    A block is confirmed several bars after its origin candle. Replaying history with the full list
    hands the engine a zone that had not been drawn yet - the error class that invalidated every
    backtest in this repo before the 2026-09 audit.
    """
    cs = bullish_setup()
    obs = find_order_blocks(cs)
    assert obs
    ob = obs[0]
    assert ob.confirmed_index > ob.origin_index
    assert visible_at(obs, ob.origin_index) == [], "the zone must not exist at its own origin bar"
    assert visible_at(obs, ob.confirmed_index - 1) == []
    assert ob in visible_at(obs, ob.confirmed_index)


def test_a_huge_candle_contributes_its_body_not_its_wick():
    """A 7-ATR bar is not a precise area. Using its full range made a gold zone so wide that price
    was permanently 'inside' it."""
    cs = chop(40, 100.0, 1.0)
    cs.append(C(40, 100.0, 108.0, 92.0, 99.0))             # enormous wick both sides
    px = 99.0
    for i in range(41, 48):
        o = px
        px += 1.8
        cs.append(C(i, o, px + 0.2, o - 0.05, px, 3000.0))
    found = [o for o in find_order_blocks(cs) if o.origin_index == 40]
    if found:
        ob = found[0]
        assert ob.top <= 100.1 and ob.bottom >= 98.9, "zone should be the body, not the 16-point range"


def test_volume_credit_belongs_to_the_blocks_own_break_not_to_now():
    """The first version used the CURRENT bar's activity for every block and printed it as
    "activity Nx average on the break" - a claim about each block that was really about right now.
    """
    # A block that broke on NORMAL activity, then a huge-volume bar arrives much later.
    cs = chop(40, 100.0, 1.0)
    cs.append(C(40, 100.0, 100.2, 98.0, 98.2))
    px = 98.2
    for i in range(41, 50):
        o = px
        px += 1.6
        cs.append(C(i, o, px + 0.3, o - 0.05, px, 1000.0))     # break on ordinary activity
    n = len(cs)
    for k in range(20):
        cs.append(C(n + k, px, px + 0.2, px - 0.2, px, 1000.0))
    cs.append(C(n + 20, px, px + 0.2, px - 0.2, px, 9000.0))    # a spike NOW, unrelated to the block
    bulls = [o for o in find_order_blocks(cs) if o.kind == "bullish" and o.origin_index == 40]
    assert bulls
    assert not any("activity" in r for r in bulls[0].reasons), \
        "a volume spike today must not be credited to a block that broke on ordinary activity"


def test_engine_detection_is_not_capped_to_the_chart_display_limit():
    """find_order_blocks keeps the 8 strongest for the chart. The engine must see them all, or the
    one block a wall check exists to find can be truncated away."""
    import inspect

    from app.agents import orchestrator
    src = inspect.getsource(orchestrator._deterministic_decision)
    assert "find_order_blocks(list(entry_candles), max_blocks=1000)" in src


def test_unproven_engine_features_never_run_on_the_live_desk():
    """The filter convention is "on unless disabled", so a newly built feature switches itself ON in
    production the moment the code loads. That happened here on 2026-10-19: ob_wall / ob_target were
    live-by-default before a single backtest result was in. The live path must add them to its
    disable set regardless of the user's settings."""
    from app.agents.pipeline import _UNPROVEN_FILTERS, _live_disable

    class _S:
        disabled_filters: list = []          # a user who has switched nothing off

    live = _live_disable(_S())
    assert {"ob_wall", "ob_target"} <= _UNPROVEN_FILTERS
    assert _UNPROVEN_FILTERS <= live


def test_entry_filters_panel_does_not_offer_unproven_features(db_session):
    """A toggle that reads ON while the feature is forced off is worse than no toggle."""
    from app.api.settings_routes import get_det_filters

    keys = {f.key for f in get_det_filters(db_session).filters}
    assert "ob_wall" not in keys and "ob_target" not in keys


def test_the_forming_bar_alone_does_not_break_a_zone():
    """The last bar is still forming - its close is only the latest tick. A zone must not vanish when
    price pokes through intrabar (UK100 1h demand, 2026-10-21); a FINISHED bar's close breaks it."""
    cs = bullish_setup()
    ob = [o for o in find_order_blocks(cs) if o.kind == "bullish"][0]
    n = len(cs)
    below = ob.bottom - 2.0
    cs.append(C(n, below, below + 0.1, below - 0.1, below))          # forming, below the zone
    assert [o for o in find_order_blocks(cs) if o.origin_index == ob.origin_index]
    cs.append(C(n + 1, below, below + 0.1, below - 0.1, below))      # the first one has now closed
    assert not [o for o in find_order_blocks(cs) if o.origin_index == ob.origin_index]


def test_the_forming_bar_alone_does_not_create_a_zone():
    """A zone born from the still-forming bar's tick flickered on and off the chart (UK100 1h
    "Supply 58", 2026-10-21): the break that creates a zone must be a finished bar."""
    cs = bullish_setup()
    ob = [o for o in find_order_blocks(cs) if o.kind == "bullish"][0]
    b = ob.confirmed_index
    assert not [o for o in find_order_blocks(cs[:b + 1]) if o.origin_index == ob.origin_index]
    assert [o for o in find_order_blocks(cs[:b + 2]) if o.origin_index == ob.origin_index]
