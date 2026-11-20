"""'Near the average' (2026-11-18, stage 58): an entry rule that skips a trade when the live price is already far past
the fast EMA, in ATR - the common scalping advice "don't chase", tested on strategy 1's real trades. On 1m with EMA 20
it reads the same number as the learner's fingerprint ext20."""
from __future__ import annotations

import random

import app.agents.learning as L
import app.agents.strategy as st
from app.api.strategy_routes import StrategyIn
from tests.test_learning import NOW, _candles, _cond

SPEC = st.CATALOG["ema_dist"]


def _p(**kw):
    return st.clean_params(SPEC, kw)


def _walk(n=120, seed=3, drift=0.0):
    rnd = random.Random(seed)
    out, x = [], 100.0
    for _ in range(n):
        x += drift + rnd.uniform(-0.3, 0.3)
        out.append(x)
    return out


def test_it_reads_the_learner_s_own_ext20():
    for seed in range(12):
        closes = _walk(seed=seed, drift=0.05 * (seed % 3 - 1))
        c = _candles(closes)
        b = st.Bars("1m", c)
        for side in (1, -1):
            spread = 0.02
            entry = closes[-1] + (0.4 * side) + (spread if side > 0 else 0.0)   # a buy fills at the ask
            fp = L.fingerprint(side, c, entry, spread, NOW)
            ctx = st.Ctx(now=NOW, ask=entry if side > 0 else entry + spread, bid=entry - spread if side > 0 else entry)
            ok, detail = st._c_ema_dist(_p(atr=99), side, b, ctx)
            assert ok is True
            assert abs(float(detail.split(" ATR")[0]) - fp["ext20"]) < 0.006      # the learner rounds to 3 places


def test_far_past_the_average_is_blocked_both_ways():
    closes = _walk(drift=0.0)
    b = st.Bars("1m", _candles(closes))
    e, a = b.ema(20)[-1], b.atr(14)[-1]
    far_up, near = e + 2.5 * a, e + 1.5 * a
    assert st._c_ema_dist(_p(), 1, b, st.Ctx(now=NOW, ask=far_up, bid=far_up))[0] is False
    assert st._c_ema_dist(_p(), 1, b, st.Ctx(now=NOW, ask=near, bid=near))[0] is True
    # a pullback below the EMA is fine for a buy
    assert st._c_ema_dist(_p(), 1, b, st.Ctx(now=NOW, ask=e - 3 * a, bid=e - 3 * a))[0] is True
    # a sell: far BELOW the average is the chase
    far_dn = e - 2.5 * a
    assert st._c_ema_dist(_p(), -1, b, st.Ctx(now=NOW, ask=far_dn, bid=far_dn))[0] is False
    assert st._c_ema_dist(_p(), -1, b, st.Ctx(now=NOW, ask=far_up, bid=far_up))[0] is True
    # the cap is the user's
    assert st._c_ema_dist(_p(atr=3.0), 1, b, st.Ctx(now=NOW, ask=far_up, bid=far_up))[0] is True


def test_no_quote_or_too_few_bars_is_unknown():
    b = st.Bars("1m", _candles(_walk()))
    assert st._c_ema_dist(_p(), 1, b, st.Ctx(now=NOW))[0] is None
    short = st.Bars("1m", _candles(_walk(n=10)))
    assert st._c_ema_dist(_p(), 1, short, st.Ctx(now=NOW, ask=100.0, bid=100.0)) == (None, "not enough bars")


def test_it_is_on_the_board_as_an_entry_rule_with_its_words():
    assert SPEC.use == "entry" and SPEC.default_tf == "1m"
    assert {p.key: p.default for p in SPEC.params} == {"period": 20, "atr": 2.0}
    StrategyIn(entry_long=[[_cond("ema_dist", "1m", period=20, atr=2.0)]])          # accepted as a BUY rule
    assert st.label(_cond("ema_dist", "1m", period=20, atr=2.0), "long") == (
        "Near the average: price at most 2.0 ATR above EMA 20 · 1m")
    assert st.label(_cond("ema_dist", "1m", period=20, atr=2.0), "short") == (
        "Near the average: price at most 2.0 ATR below EMA 20 · 1m")
    assert any(c["type"] == "ema_dist" for c in st.catalog())
