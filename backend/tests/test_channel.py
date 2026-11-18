"""'Channel place' and 'this hour so far' (2026-11-17, the user after a day of late entries: "would buying at the
bottom of the channel help?" and "the 1h candle should support us"): the entry rule that reads the chart's regression
channel, 'Not stretched' counted from 1 bar back (on 1h: the hour so far), the learner's fingerprint version 2 with
both - added to the old trades from history - and the lessons' Apply, which adds the rules only on a click."""
from __future__ import annotations

import math
import random
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.agents.learning as L
import app.agents.strategy as st
from app.agents.indicators import regression_channel
from app.models.db import AgentRun, StrategyTradeFact
from app.models.schemas import Candle
from tests.test_learning import NOW, _candles, _cond, _facts, _HistBroker, _Mt5, _strategy

SPEC = st.CATALOG["channel"]


def _p(**kw):
    return st.clean_params(SPEC, kw)


def _rising(n=59, step=0.1, wiggle=0.3, base=100.0):
    return [base + step * i + wiggle * (-1) ** i for i in range(n)]


def _hours(closes, end=NOW):
    """1h candles closed by ``end``: the last one opened an hour before ``end``'s hour."""
    top = end.replace(minute=0, second=0, microsecond=0)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        ts = top - timedelta(hours=len(closes) - i)
        out.append(Candle(ts=ts, open=prev, high=max(prev, c) + 0.2, low=min(prev, c) - 0.2, close=c, volume=100))
        prev = c
    return out


def _chart(ys, k=2.0):
    """Chart.tsx applyChannel, line by line: the lower and upper band at the last bar, and the slope."""
    m = len(ys)
    mean_x, mean_y = (m - 1) / 2, sum(ys) / m
    sxx = sum((x - mean_x) ** 2 for x in range(m))
    sxy = sum((x - mean_x) * (ys[x] - mean_y) for x in range(m))
    slope = sxy / sxx
    icpt = mean_y - slope * mean_x
    std = math.sqrt(sum((ys[x] - (icpt + slope * x)) ** 2 for x in range(m)) / m)
    v = icpt + slope * (m - 1)
    return v - k * std, v + k * std, slope


# --------------------------------------------------------------------------- the channel the chart draws

def test_it_is_the_chart_s_own_channel():
    rnd = random.Random(7)
    ys, p = [], 100.0
    for _ in range(60):
        p += rnd.uniform(-1, 1.2)
        ys.append(p)
    ch = st.reg_channel(ys)
    lo, up, slope = _chart(ys)
    assert abs(ch["lower"] - lo) < 1e-9 and abs(ch["upper"] - up) < 1e-9 and abs(ch["slope"] - slope) < 1e-12
    assert abs(ch["pos"] - (ys[-1] - lo) / (up - lo)) < 1e-9
    # ... and the backend's own (indicators.regression_channel, rounded) on the same closes
    old = regression_channel([Candle(ts=NOW, open=y, high=y, low=y, close=y, volume=1) for y in ys], 60, 2.0)
    assert abs(old["pos"] - ch["pos"]) < 1e-3
    assert st.reg_channel([5.0] * 60) is None and st.reg_channel([1.0, 2.0]) is None


def test_a_buy_holds_low_in_a_rising_channel_not_at_its_top():
    closes = _rising()
    b = st.Bars("1m", _candles(closes))
    line = 100 + 0.1 * 59
    for bid, want in ((line - 0.35, True), (line + 0.35, False)):
        place = st.reg_channel(closes + [bid])["pos"]
        ok, text = st._c_channel(_p(), 1, b, st.Ctx(now=NOW, bid=bid, ask=bid + 0.02))
        assert ok is want and (place * 100 <= 70) is want
        assert text == f"price {place * 100:.0f}% of the way up the channel (at most 70%) · the channel up"


def test_a_sell_reads_it_mirrored():
    closes = [200.0 - x for x in _rising(base=0.0)]          # falling, the same wiggle
    b = st.Bars("1m", _candles(closes))
    line = 200 - 0.1 * 59
    for bid, want in ((line + 0.35, True), (line - 0.35, False)):
        place = 1 - st.reg_channel(closes + [bid])["pos"]
        ok, text = st._c_channel(_p(), -1, b, st.Ctx(now=NOW, bid=bid, ask=bid + 0.02))
        assert ok is want, (bid, place)
        assert text.startswith(f"price {place * 100:.0f}% of the way down the channel") and text.endswith("the channel down")


def test_the_channel_must_slope_the_trades_way_unless_unticked():
    falling = [200.0 - x for x in _rising(base=0.0)]
    b = st.Bars("1m", _candles(falling))
    bid = 200 - 0.1 * 59 - 0.35                              # low in a FALLING channel: a buy against it
    ok, text = st._c_channel(_p(), 1, b, st.Ctx(now=NOW, bid=bid, ask=bid + 0.02))
    assert ok is False and text.endswith("the channel down - needs up")
    assert st._c_channel(_p(slope=0), 1, b, st.Ctx(now=NOW, bid=bid, ask=bid + 0.02))[0] is True


def test_over_100_lets_price_stand_above_the_band():
    closes = _rising()
    b = st.Bars("1m", _candles(closes))
    bid = 100 + 0.1 * 59 + 1.0                               # past the upper band
    assert st.reg_channel(closes + [bid])["pos"] > 1
    assert st._c_channel(_p(place=100), 1, b, st.Ctx(now=NOW, bid=bid, ask=bid))[0] is False
    assert st._c_channel(_p(place=200), 1, b, st.Ctx(now=NOW, bid=bid, ask=bid))[0] is True


def test_it_waits_without_bars_a_quote_or_a_band():
    short = st.Bars("1m", _candles(_rising(n=30)))
    assert st._c_channel(_p(), 1, short, st.Ctx(now=NOW, bid=101.0, ask=101.1)) == (None, "not enough bars")
    b = st.Bars("1m", _candles(_rising()))
    assert st._c_channel(_p(), 1, b, st.Ctx(now=NOW))[0] is None
    flat = st.Bars("1m", _candles([100.0] * 59))
    assert st._c_channel(_p(), 1, flat, st.Ctx(now=NOW, bid=100.0, ask=100.0)) == (None, "no channel (flat prices)")


def test_the_board_takes_it_in_the_buy_rules_only_and_reads_it_plainly():
    from app.api.strategy_routes import StrategyIn

    ok = StrategyIn(entry_long=[[_cond("channel", place=70, bars=60, sd=2.0, slope=1)]])
    assert ok.entry_long[0][0].params == {"place": 70, "bars": 60, "sd": 2.0, "slope": 1}
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[_cond("trend")]], exit_rules=[[_cond("channel", place=70, bars=60, sd=2.0, slope=1)]])
    assert any(c["type"] == "channel" for c in st.catalog())
    cond = _cond("channel", place=70, bars=60, sd=2.0, slope=1)
    assert st.label(cond, "long") == ("Channel place: in the lower 70% of the regression channel (60 bars, 2.0 sd) · "
                                      "the channel sloping up: on · 1m")
    assert st.label(cond, "short") == ("Channel place: in the upper 70% of the regression channel (60 bars, 2.0 sd) · "
                                       "the channel sloping down: on · 1m")


def test_the_engine_reads_it_like_any_entry_rule():
    closes = _rising()
    c = _candles(closes)
    group = [[_cond("channel", place=70, bars=60, sd=2.0, slope=1)]]
    line = 100 + 0.1 * 59
    for bid, want in ((line - 0.35, True), (line + 0.35, False)):
        ctx = st.Ctx(now=NOW + timedelta(seconds=1), ask=bid + 0.01, bid=bid, risk=1.0)
        res = st.evaluate(group, 1, {"1m": st.Bars("1m", c)}, ctx, "long")
        assert res["ok"] is want, res


# --------------------------------------------------------------------------- this hour so far (Not stretched, 1 bar back)

def test_not_stretched_counts_from_one_bar_back_this_hour_so_far():
    spec = st.CATALOG["max_run"]
    assert st.clean_params(spec, {"bars": 1, "atr": 1.0}) == {"bars": 1, "atr": 1.0}
    hours = _hours([100.0 + 0.5 * i for i in range(40)])
    b = st.Bars("1h", hours)
    a = b.atr(14)[-1]
    ask = hours[-1].close + 1.2 * a                          # this hour already 1.2 ATR up
    ok, detail = st._c_max_run(st.clean_params(spec, {"bars": 1, "atr": 1.0}), 1, b, st.Ctx(now=NOW, ask=ask, bid=ask))
    assert ok is False and detail == "+1.20 ATR up since the close 1 bars back (at most 1)"
    assert st._c_max_run(st.clean_params(spec, {"bars": 1, "atr": 1.5}), 1, b, st.Ctx(now=NOW, ask=ask, bid=ask))[0]
    from app.api.strategy_routes import StrategyIn

    ok_in = StrategyIn(entry_long=[[_cond("max_run", "1h", bars=1, atr=1.0)]])
    assert ok_in.entry_long[0][0].params == {"bars": 1, "atr": 1.0}
    assert st.label(_cond("max_run", "1h", bars=1, atr=1.0), "long") == (
        "Not stretched: price at most 1.0 ATR above the close 1 bars back · 1h")


# --------------------------------------------------------------------------- the learner: fingerprint version 2

def test_the_fingerprint_reads_both_as_the_rules_do():
    closes = _rising(n=80)
    c = _candles(closes)
    hours = _hours([100.0 + 0.5 * i for i in range(40)])
    spread = 0.02
    ask = closes[-1] + 0.1
    fp = L.fingerprint(1, c, ask, spread, NOW, hours)
    assert fp["v"] == L.FP_VERSION == 2 and set(L.FP2_KEYS) <= set(fp)
    # the 1m channel at the bid, as Channel place reads it on the same closed bars
    place = st.reg_channel(closes[-59:] + [ask - spread])["pos"]
    assert abs(fp["chan1m"] - place) < 1e-3 and fp["chan1m_up"] is True
    ok, text = st._c_channel(_p(place=200, slope=0), 1, st.Bars("1m", c), st.Ctx(now=NOW, bid=ask - spread, ask=ask))
    assert text.startswith(f"price {place * 100:.0f}%")
    # the hour so far, as Not stretched 1 bar back on 1h reads it (a buy fills at the ask = the entry)
    _ok, detail = st._c_max_run({"bars": 1, "atr": 99.0}, 1, st.Bars("1h", hours), st.Ctx(now=NOW, ask=ask, bid=ask - spread))
    assert detail.startswith(f"{fp['h1run']:+.2f} ATR up")
    # a sell: the other way round
    sell = L.fingerprint(-1, c, closes[-1], spread, NOW, hours)
    assert abs(sell["chan1m"] - (1 - st.reg_channel(closes[-59:] + [closes[-1]])["pos"])) < 1e-3
    assert sell["chan1m_up"] is False
    _ok, sdetail = st._c_max_run({"bars": 1, "atr": 99.0}, -1, st.Bars("1h", hours),
                                 st.Ctx(now=NOW, ask=closes[-1] + spread, bid=closes[-1]))
    assert sdetail.startswith(f"{sell['h1run']:+.2f} ATR down")
    # no 1h candles: no hour so far, the rest as before
    no_h = L.fingerprint(1, c, ask, spread, NOW)
    assert no_h["h1run"] is None and no_h["chan1m"] == fp["chan1m"] and no_h["streak"] == fp["streak"]


def test_an_old_fingerprint_gets_the_new_parts_from_history_once(db_session):
    cfg = _strategy(db_session)
    t0 = (NOW - timedelta(hours=10)).replace(second=0, microsecond=0)

    def price(t):
        return 100 + 0.01 * (t - t0).total_seconds() / 60

    broker = _HistBroker(_Mt5(t0, 10 * 60, price))
    o = (NOW - timedelta(hours=3)).replace(second=5, microsecond=0)
    old_fp = {"v": 1, "streak": 3, "hour": 9, "run15": 1.25}
    for key, entry in (("a", price(o)), ("b", None)):
        db_session.add(StrategyTradeFact(key=key, strategy_id=cfg.id, symbol="BTCUSDm", side="long", opened_at=o,
                                         closed_at=o + timedelta(minutes=20), pnl=5.0, r=0.2, entry=entry,
                                         risk_dist=1.0, fp=dict(old_fp), fp_from="entry", path=[[1, 0.1]],
                                         whatif={"t0.9|s1": [0.9, 30]}))
    db_session.commit()
    assert L.fill_from_history(db_session, cfg, lambda ac: broker, NOW) == 1          # the one with an entry
    rows = {r.key: r for r in db_session.query(StrategyTradeFact).all()}
    a = rows["a"]
    assert a.fp["v"] == L.FP_VERSION and a.fp_from == "entry"
    assert {k: a.fp[k] for k in old_fp if k != "v"} == {"streak": 3, "hour": 9, "run15": 1.25}   # kept as they were
    assert a.fp["chan1m"] is not None and a.fp["chan1m_up"] is True and a.fp["h1run"] is not None
    assert rows["b"].fp == old_fp                                                       # no entry: nothing to read
    assert L.fill_from_history(db_session, cfg, lambda ac: broker, NOW) == 0          # once


def test_a_failed_history_read_is_tried_three_times(db_session):
    cfg = _strategy(db_session)
    empty = _HistBroker(_Mt5(NOW - timedelta(hours=10), 0, lambda t: 100.0))       # no candles at all
    o = NOW - timedelta(hours=3)
    db_session.add(StrategyTradeFact(key="a", strategy_id=cfg.id, symbol="BTCUSDm", side="long", opened_at=o,
                                     closed_at=o + timedelta(minutes=20), pnl=5.0, r=0.2, entry=100.0, risk_dist=1.0,
                                     fp={"v": 1, "streak": 1}, path=[[1, 0.1]], whatif={"t0.9|s1": [0.9, 30]}))
    db_session.commit()
    for _ in range(3):
        assert L.fill_from_history(db_session, cfg, lambda ac: empty, NOW) == 1
    assert L.fill_from_history(db_session, cfg, lambda ac: empty, NOW) == 0
    row = db_session.query(StrategyTradeFact).one()
    assert row.fp == {"v": 1, "streak": 1} and row.fp_tries == 3


# --------------------------------------------------------------------------- the review and its lessons

def test_the_review_splits_trades_by_both():
    part = L.FEATURES["chan1m"][1]
    assert part({"fp": {"chan1m": 0.3, "chan1m_up": True}})[0] == "low"
    assert part({"fp": {"chan1m": L.CHAN_LOW, "chan1m_up": True}})[0] == "high"
    assert part({"fp": {"chan1m": 1.4, "chan1m_up": True}})[0] == "high"
    assert part({"fp": {"chan1m": 0.1, "chan1m_up": False}})[0] == "against"
    assert part({"fp": {"v": 1, "streak": 2}}) is None                                 # an old one, not yet filled
    hour = L.FEATURES["h1run"][1]
    assert hour({"fp": {"h1run": L.H1RUN_HIGH}})[0] == "high"
    assert hour({"fp": {"h1run": 0.99}})[0] == "mid" and hour({"fp": {"h1run": -0.4}})[0] == "low"


CFG = {"pairs": [{"symbol": "BTCUSDm"}], "sides": "both", "hours": None,
       "entry_long": [[_cond("trend")], [_cond("spread", max_pct=30), _cond("max_run", bars=15, atr=3.5)]],
       "off_groups": {"entry_long": [0]}, "target_r": 0.9, "stop_atr": 5.0}


def test_the_channel_lessons_ask_for_channel_place_in_the_groups_that_trade():
    high = {"feature": "chan1m", "bucket": "high", "kind": "weak"}
    act = L.action_for(high, CFG)
    assert act["op"] == "channel" and act["place"] == 70 and act["done"] is False
    assert "in the lower 70% of the 1m regression channel, sloping our way" in act["text"]
    out = L.apply_change(CFG, act)
    assert out["entry_long"][0] == [_cond("trend")]                                    # the group switched off
    assert out["entry_long"][1][-1] == {"type": "channel", "tf": "1m",
                                        "params": {"place": 70, "bars": 60, "sd": 2.0, "slope": 1}}
    assert CFG["entry_long"][1] == [_cond("spread", max_pct=30), _cond("max_run", bars=15, atr=3.5)]   # untouched
    assert L.action_for(high, out)["done"] is True
    assert L.action_for({"feature": "chan1m", "bucket": "low", "kind": "sweet"}, CFG)["place"] == 70
    against = L.action_for({"feature": "chan1m", "bucket": "against", "kind": "weak"}, CFG)
    assert against["place"] == L.CHAN_ANY and "the 1m regression channel sloping our way" in against["text"]
    assert L.action_for({"feature": "chan1m", "bucket": "against", "kind": "weak"}, out)["done"] is True  # 70 is stricter
    # a winning top or a losing bottom asks for nothing
    assert L.action_for({"feature": "chan1m", "bucket": "high", "kind": "sweet"}, CFG) is None
    assert L.action_for({"feature": "chan1m", "bucket": "low", "kind": "weak"}, CFG) is None
    # the user's own stricter place stays when the lesson's change is made on their rule
    mine = {**CFG, "entry_long": [[_cond("trend")], [_cond("channel", place=50, bars=60, sd=2.0, slope=0)]]}
    assert L.action_for({"feature": "chan1m", "bucket": "against", "kind": "weak"}, mine)["done"] is False
    got = L.apply_change(mine, against)
    assert [c for c in got["entry_long"][1] if c["type"] == "channel"] == [
        {"type": "channel", "tf": "1m", "params": {"place": 50, "bars": 60, "sd": 2.0, "slope": 1}}]


def test_the_hour_lesson_asks_for_its_own_not_stretched_on_1h():
    lesson = {"feature": "h1run", "bucket": "high", "kind": "weak"}
    act = L.action_for(lesson, CFG)
    assert act["op"] == "hour_run" and act["atr"] == 1.0 and act["done"] is False
    out = L.apply_change(CFG, act)
    assert [c for c in out["entry_long"][1] if c["type"] == "max_run"] == [
        {"type": "max_run", "tf": "1m", "params": {"bars": 15, "atr": 3.5}},          # the 15-minute one stays
        {"type": "max_run", "tf": "1h", "params": {"bars": 1, "atr": 1.0}}]
    assert L.action_for(lesson, out)["done"] is True
    assert L.action_for({"feature": "run15", "bucket": "high", "kind": "weak"}, out)["done"] is True
    two_back = {**CFG, "entry_long": [[_cond("trend")], [_cond("max_run", "1h", bars=2, atr=1.0)]]}
    assert L.action_for(lesson, two_back)["done"] is False                            # 2 bars back is another reading
    got = L.apply_change(two_back, act)
    assert [c for c in got["entry_long"][1] if c["type"] == "max_run"] == [
        {"type": "max_run", "tf": "1h", "params": {"bars": 2, "atr": 1.0}},
        {"type": "max_run", "tf": "1h", "params": {"bars": 1, "atr": 1.0}}]
    assert L.action_for({"feature": "h1run", "bucket": "mid", "kind": "weak"}, CFG) is None
    assert L.action_for({"feature": "h1run", "bucket": "low", "kind": "sweet"}, CFG) is None


def test_applied_on_a_click_and_undone(db_session):
    from app.api.strategy_routes import LearningAct, strategy_learning, strategy_learning_act

    cfg = _strategy(db_session)
    for f in _facts(key=lambda bad: {"chan1m": 0.9 if bad else 0.3, "chan1m_up": True}):
        db_session.add(StrategyTradeFact(key=f"k{f['id']}", strategy_id=cfg.id, symbol="BTCUSDm", side="long",
                                         opened_at=f["opened_at"], pnl=f["pnl"], r=f["r"], fp=f["fp"]))
    db_session.commit()
    view = strategy_learning(cfg.id, db_session)
    lesson = next(p for p in view["lessons"] if p["key"] == "chan1m:high")
    assert lesson["kind"] == "weak" and lesson["strength"] == "strong"
    assert lesson["action"]["op"] == "channel" and not lesson["action"]["done"]
    db_session.refresh(cfg)
    assert not any(c["type"] == "channel" for g in cfg.entry_long for c in g)         # looking changes nothing

    strategy_learning_act(cfg.id, LearningAct(key="chan1m:high", action="apply"), db_session)
    db_session.refresh(cfg)
    assert cfg.entry_long[1][-1] == {"type": "channel", "tf": "1m",
                                     "params": {"place": 70, "bars": 60, "sd": 2.0, "slope": 1}}
    assert cfg.entry_long[0] == [_cond("trend")] and cfg.risk_pct == 0.05 and cfg.live
    edit = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).one()
    assert edit.detail["by"] == "lesson"
    assert any("Channel place: in the lower 70% of the regression channel" in ch for ch in edit.detail["changes"])

    strategy_learning_act(cfg.id, LearningAct(key="chan1m:high", action="undo"), db_session)
    db_session.refresh(cfg)
    assert not any(c["type"] == "channel" for g in cfg.entry_long for c in g)
