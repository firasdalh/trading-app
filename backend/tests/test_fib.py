"""The Auto Fibonacci in the engine (app/agents/fib.py) and the "Fibonacci zone" entry rule (2026-11-15)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as N

import pytest
from pydantic import ValidationError

import app.agents.fib as fib
import app.agents.strategy as st
from app.models.schemas import Candle

NOW = datetime(2026, 11, 15, 12, 0, 30, tzinfo=timezone.utc)
UP = fib.Fib("up", fib.Swing(0, 100.0, False), fib.Swing(10, 110.0, True), False, "trend")
DOWN = fib.Fib("down", fib.Swing(0, 110.0, True), fib.Swing(10, 100.0, False), False, "trend")


def test_the_engine_finds_the_same_leg_as_the_chart():
    """16 windows of real MT5 bars (every kind of leg, broken or not) against what autoFib.ts returned for them."""
    cases = json.loads((Path(__file__).parent / "fib_cases.json").read_text())
    kinds = set()
    for k in cases:
        c = [N(high=h, low=lo, close=cl) for h, lo, cl in k["c"]]
        f = fib.auto_fib(c)
        e = k["expect"]
        assert (f.trend, [f.a.i, f.a.price], [f.b.i, f.b.price], f.broken, f.why) == \
            (e["trend"], e["a"], e["b"], e["broken"], e["why"]), (k["sym"], k["tf"])
        assert fib.position(f, c[-1].close) == pytest.approx(e["pos"], abs=1e-12)
        kinds.add((f.why, f.broken))
    assert len(kinds) == 8


def _path(prices: list[float], tf: str = "15m") -> list[Candle]:
    """Bars stepping through ``prices`` (0.2 wicks), closed by NOW."""
    secs = st.TF_SECONDS[tf]
    start = NOW - timedelta(seconds=secs * (len(prices) + 1))
    out, prev = [], prices[0]
    for i, p in enumerate(prices):
        out.append(Candle(ts=start + timedelta(seconds=secs * i), open=prev, high=max(prev, p) + 0.2,
                          low=min(prev, p) - 0.2, close=p, volume=100))
        prev = p
    return out


def _walk(*legs: float, step: float = 1.0) -> list[float]:
    out = [legs[0]]
    for to in legs[1:]:
        while abs(out[-1] - to) > 1e-9:
            out.append(out[-1] + (step if to > out[-1] else -step))
    return out


def test_the_leg_holds_still_while_price_runs_to_its_targets():
    """Finished swings only: a new high that price hasn't turned away from yet doesn't move the 0 or the targets."""
    base = _walk(100, 110, 104, 116, 110)            # up: a higher high (116) and a higher low (104)
    f = fib.auto_fib(_path(base))
    assert f.trend == "up" and f.b.price == pytest.approx(116.2) and f.a.price == pytest.approx(103.8)
    g = fib.auto_fib(_path(base + _walk(110, 120)[1:]))     # running past 116 to 120, no 3 x ATR turn yet
    assert (g.a, g.b) == (f.a, f.b) and not g.broken


def test_where_a_buy_and_a_sell_open_on_an_up_leg():
    places = {p: fib.place(UP, p, 1).place for p in (104.0, 108.0, 111.0, 113.5, 118.0)}
    assert places == {104.0: "pull", 108.0: "near", 111.0: "past", 113.5: "zone", 118.0: "beyond"}
    assert fib.place(UP, 111.0, 1).target == pytest.approx(112.72)
    assert fib.place(UP, 108.0, -1).place == "against"                 # selling inside an up leg: no target ahead
    # Broken down (price under the 1): the sell aims at the break zone 1.272-1.618 under the low, the buy has none.
    assert [fib.place(UP, p, -1).place for p in (98.0, 96.0, 92.0)] == ["past", "zone", "beyond"]
    assert fib.place(UP, 101.0, -1).place == "against"                 # not broken yet: still an up leg
    assert fib.place(replace(UP, broken=True), 101.0, -1).place == "pull"   # broken, price back inside it
    assert fib.place(UP, 98.0, -1).target == pytest.approx(97.28) and fib.place(UP, 98.0, -1).broken
    assert fib.place(UP, 98.0, 1).place == "against"
    # A down leg mirrors it all for a sell.
    assert [fib.place(DOWN, p, -1).place for p in (106.0, 102.0, 99.0, 96.5, 92.0)] == \
        ["pull", "near", "past", "zone", "beyond"]


def _zone(leg, s, price, risk=1.0, **params):
    b = st.Bars("15m", _path(_walk(100, 110, 104, 116, 110)))
    b._memo[("fib",)] = leg
    p = st.clean_params(st.CATALOG["fib_zone"], params)
    return st._c_fib_zone(p, s, b, st.Ctx(now=NOW, bid=price, ask=price, risk=risk))


def test_a_buy_waits_for_room_before_the_target_zone():
    ok, detail = _zone(UP, 1, 111.0)                  # 1.72R before ext 1.272 (112.72)
    assert ok is True and "past the swing" in detail and "1.7R before the target zone" in detail
    ok, detail = _zone(UP, 1, 112.5)                  # only 0.22R left
    assert ok is False and detail.endswith("needs 0.5R")
    assert _zone(UP, 1, 112.5, room=0)[0] is True
    ok, detail = _zone(UP, 1, 113.5)                  # in the target zone: by default it waits for the pullback
    assert ok is False and "in the target zone" in detail and "wait for the pullback" in detail
    assert _zone(UP, 1, 113.5, zone=1)[0] is True
    assert _zone(UP, 1, 118.0)[0] is False and _zone(UP, 1, 118.0, beyond=1)[0] is True
    ok, detail = _zone(UP, 1, 104.5)
    assert ok is True and "(golden zone)" in detail and "in a pullback" in detail


def test_a_sell_reads_it_mirrored():
    ok, detail = _zone(DOWN, -1, 99.0)                # 97.28 is 1.72R under it
    assert ok is True and "1.7R before the target zone" in detail
    assert _zone(DOWN, -1, 96.5)[0] is False
    ok, detail = _zone(UP, -1, 98.0)                  # the up leg broke down: its break zone is the sell's target
    assert ok is True and "broken" in detail and "before the break zone" in detail
    ok, detail = _zone(UP, -1, 108.0)
    assert ok is True and "no target ahead for a sell" in detail
    assert _zone(UP, -1, 108.0, against=0)[0] is False


def test_it_never_holds_on_what_it_cannot_read():
    assert _zone(UP, 1, None)[0] is None                                   # no quote
    assert _zone(None, 1, 111.0)[0] is None                                # no leg yet
    assert _zone(UP, 1, 111.0, pull=0, near=0, past=0, zone=0, beyond=0, against=0)[0] is None
    ok, detail = _zone(UP, 1, 111.0, risk=None)                           # a room to measure, but no R
    assert ok is None and "no stop distance" in detail
    ok, detail = _zone(UP, 1, 111.0, risk=None, room=0)
    assert ok is True and "target zone at 112.72" in detail


def test_the_board_takes_it_on_its_timeframes_in_the_entry_rules():
    from app.api.strategy_routes import StrategyIn

    req = StrategyIn(entry_long=[[{"type": "fib_zone", "tf": "15m", "params": {"zone": 1, "room": 0.9}}]])
    assert req.entry_long[0][0].params == {"pull": 1, "near": 1, "past": 1, "zone": 1, "beyond": 0, "against": 1,
                                           "room": 0.9}
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[{"type": "fib_zone", "tf": "10s"}]])
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[{"type": "trend"}]], exit_rules=[[{"type": "fib_zone", "tf": "15m"}]])
    cat = {c["type"]: c for c in st.catalog()}["fib_zone"]
    assert cat["default_tf"] == "5m" and cat["group"] == "Levels & zones"
    assert [q["kind"] for q in cat["params"]] == ["pick"] * 6 + ["num"]
    r = st.Rules(timeframe="1m", entry_long=[[{"type": "fib_zone", "tf": "15m", "params": {}}]])
    assert r.sizes() == {"1m": st._BARS, "15m": st._FIB_BARS}
    assert st.label({"type": "fib_zone", "tf": "15m", "params": {}}, "long") == (
        "Auto Fibonacci: a buy opens in a pullback · at the swing · past the swing · no target ahead, "
        "with 0.5R+ before the target zone · 15m")
    assert st.label({"type": "fib_zone", "tf": "1h", "params": {"room": 0, "pull": 0}}, "short").startswith(
        "Auto Fibonacci: a sell opens at the swing")
