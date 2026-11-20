"""Stop past a nearby 5m level (2026-11-20, strategy level_stop): when a 5m swing low (a sell: high) sits within 0.3R
of the ATR stop, the stop goes 0.1R past the furthest such swing, and one R with it - the target, the broker's safety
target, the follow lock and the lots follow. The user, after the tick-replay study: "build it and switch it on"."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

import app.agents.strategy as st
from app.models.db import AgentRun
from app.models.schemas import Candle, OHLCVSeries
from tests.test_strategy import NOW, RISING, _Broker, _candles, _strategy, engine  # noqa: F401 - engine: the fixture

SYM = "BTCUSDm"


def _five(lows=None, highs=None, n=60, base_low=123.0, base_high=124.0, end=NOW):
    """Closed 5m bars ending before ``end``: the lows creep up from ``base_low`` and the highs down from
    ``base_high`` (so no bar is a swing of its own), but the ones given ({place: value}, counted from the oldest)."""
    lows, highs = lows or {}, highs or {}
    top = end.replace(second=0, microsecond=0) - timedelta(minutes=end.minute % 5)
    out = []
    for i in range(n):
        ts = top - timedelta(minutes=5 * (n - i))
        lo, hi = lows.get(i, base_low + 0.001 * i), highs.get(i, base_high - 0.001 * i)
        out.append(Candle(ts=ts, open=(lo + hi) / 2, high=hi, low=lo, close=(lo + hi) / 2, volume=10))
    return st.Bars("5m", out)


# ----------------------------------------------------------------- the swings

def test_a_swing_is_the_lowest_low_of_three_bars_either_side_confirmed_by_three_closed_ones():
    b = _five(lows={20: 121.5, 30: 122.0, 58: 120.0})          # 58: only one closed bar after it - not yet a swing
    assert st.swing_levels(b, 1) == [121.5, 122.0]
    b = _five(highs={10: 126.0, 11: 126.0, 40: 125.0})          # ties count
    assert st.swing_levels(b, -1) == [126.0, 126.0, 125.0]


def test_only_the_last_150_bars_count():
    b = _five(lows={5: 110.0, 100: 121.0}, n=200)
    assert st.swing_levels(b, 1) == [121.0]
    assert st.swing_levels(_five(n=10), 1) == []                # too few bars
    assert st.swing_levels(None, 1) == []


# ----------------------------------------------------------------- where the stop goes

def test_a_buy_s_stop_goes_past_the_furthest_swing_low_near_its_atr_stop():
    # entry 124, 1R = 2 -> ATR stop 122; near = within 0.6 of it: 121.4 .. 122.6
    b = _five(lows={10: 120.9, 20: 121.5, 30: 122.4, 40: 123.5})
    lv = st.level_stop(1, 124.0, 2.0, b)
    assert lv["level"] == 121.5                                 # 120.9 too far, 123.5 not near; the furthest of the rest
    assert lv["dist"] == pytest.approx(124.0 - (121.5 - 0.2))
    assert lv["x"] == pytest.approx(2.7 / 2.0, abs=1e-3) and lv["atr_dist"] == 2.0 and lv["tf"] == "5m"


def test_a_swing_just_inside_the_stop_brings_it_in_behind_the_swing():
    lv = st.level_stop(1, 124.0, 2.0, _five(lows={20: 122.5}))
    assert lv["dist"] == pytest.approx(124.0 - 122.3)          # 1.7: a bit tighter, still past the swing


def test_a_sell_reads_the_swing_highs_above_it():
    b = _five(highs={20: 126.4, 30: 125.8, 40: 127.0}, base_high=124.5, base_low=124.0)
    lv = st.level_stop(-1, 124.0, 2.0, b)                       # ATR stop 126; near: 125.4 .. 126.6
    assert lv["level"] == 126.4 and lv["dist"] == pytest.approx(126.6 - 124.0)


def test_no_swing_near_keeps_the_atr_stop():
    assert st.level_stop(1, 124.0, 2.0, _five(lows={20: 120.0})) is None
    assert st.level_stop(1, 124.0, 2.0, _five()) is None        # no swing at all
    assert st.level_stop(1, 124.0, 0.0, _five(lows={20: 122.0})) is None
    assert st.level_stop(1, 124.0, 2.0, None) is None


def test_never_under_half_the_atr_stop(monkeypatch):
    monkeypatch.setattr(st, "LEVEL_NEAR_R", 0.8)
    assert st.level_stop(1, 124.0, 2.0, _five(lows={20: 123.5})) is None    # 0.4: under half of 2


# ----------------------------------------------------------------- the strategy reads its 5m bars for it

def test_it_reads_5m_bars_only_when_on_and_never_with_a_supertrend_stop(db_session, engine):  # noqa: F811
    on = st.Rules.of(_strategy(db_session, level_stop=True))
    assert on.level and "5m" in on.tfs() and on.sizes()["5m"] >= st.LEVEL_BARS
    off = st.Rules.of(_strategy(db_session, name="b"))
    assert not off.level and "5m" not in off.tfs()
    with_st = st.Rules.of(_strategy(db_session, name="c", level_stop=True,
                                    stop={"tf": "1m", "period": 10, "mult": 2.7, "buffer": 0.2}))
    assert not with_st.level


class _LevelBroker(_Broker):
    def __init__(self, closes, five):
        super().__init__(closes)
        self.five = five

    def get_ohlcv(self, symbol, timeframe="1m", limit=200):
        if timeframe == "5m":
            return OHLCVSeries(symbol=symbol, timeframe="5m", candles=list(self.five.c))
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=_candles(self.closes, timeframe))


def _run(db_session, engine, monkeypatch, five, **kw):  # noqa: F811
    calls, holder = engine
    holder["broker"] = _LevelBroker(RISING, five)
    monkeypatch.setattr(st, "_side_risk", lambda *a, **k: 2.0)  # one ATR R = 2: the ATR stop sits 2 under the entry
    cfg = _strategy(db_session, target_r=0.9, exit_rules=[], **kw)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert out[0].get("opened") == "long", out
    return calls["assess"][0][0], cfg


def test_on_the_order_gets_the_level_stop_and_a_target_of_the_new_r(db_session, engine, monkeypatch):  # noqa: F811
    entry = RISING[-1] + 0.05                                   # the ask
    five = _five(lows={20: round(entry - 2.25, 2)}, base_low=entry - 0.5, base_high=entry + 0.5)
    prop, cfg = _run(db_session, engine, monkeypatch, five, level_stop=True)
    level = round(entry - 2.25, 2)
    new = entry - (level - 0.2)
    assert prop.entry == pytest.approx(entry)
    assert prop.stop_loss == pytest.approx(level - 0.2, abs=0.011)
    assert prop.take_profit == pytest.approx(entry + 0.9 * new, abs=0.011)
    opened = db_session.scalars(select(AgentRun).where(AgentRun.event == "opened")).one().detail
    assert opened["level_stop"]["level"] == pytest.approx(level) and opened["level_stop"]["tf"] == "5m"
    assert opened["level_stop"]["atr_stop"] == pytest.approx(entry - 2.0, abs=0.011)
    assert opened["level_stop"]["x"] == pytest.approx(new / 2.0, abs=1e-3)


def test_with_follow_the_lock_and_the_safety_target_use_the_new_r(db_session, engine, monkeypatch):  # noqa: F811
    entry = RISING[-1] + 0.05
    five = _five(lows={20: round(entry - 2.25, 2)}, base_low=entry - 0.5, base_high=entry + 0.5)
    prop, cfg = _run(db_session, engine, monkeypatch, five, level_stop=True, follow_target=True)
    new = entry - prop.stop_loss
    assert prop.take_profit == pytest.approx(entry + (0.9 + st.FOLLOW_SAFETY_R) * new, abs=0.011)
    fe = cfg.state[SYM]["follow"]
    assert fe["risk"] == pytest.approx(new, abs=0.011) and fe["target"] == pytest.approx(entry + 0.9 * new, abs=0.011)
    assert fe["lock"] == pytest.approx(fe["target"] - st.FOLLOW_LOCK_R * new, abs=0.011)


def test_off_or_with_no_swing_near_the_atr_stop_is_as_before(db_session, engine, monkeypatch):  # noqa: F811
    entry = RISING[-1] + 0.05
    five = _five(lows={20: round(entry - 2.25, 2)}, base_low=entry - 0.5, base_high=entry + 0.5)
    prop, _cfg = _run(db_session, engine, monkeypatch, five)                     # the switch off
    assert prop.stop_loss == pytest.approx(entry - 2.0, abs=0.011)
    assert "level_stop" not in db_session.scalars(select(AgentRun).where(AgentRun.event == "opened")).one().detail


def test_no_swing_near_keeps_the_atr_stop_on_the_order(db_session, engine, monkeypatch):  # noqa: F811
    entry = RISING[-1] + 0.05
    five = _five(base_low=entry - 0.5, base_high=entry + 0.5)                    # no swing anywhere near entry - 2
    prop, _cfg = _run(db_session, engine, monkeypatch, five, level_stop=True)
    assert prop.stop_loss == pytest.approx(entry - 2.0, abs=0.011)


def test_a_failure_reading_the_level_keeps_the_atr_stop(db_session, engine, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("no 5m")
    monkeypatch.setattr(st, "level_stop", boom)
    entry = RISING[-1] + 0.05
    prop, _cfg = _run(db_session, engine, monkeypatch, _five(), level_stop=True)
    assert prop.stop_loss == pytest.approx(entry - 2.0, abs=0.011)


def test_the_floor_still_holds(db_session, engine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(st, "_stop_floor", lambda session, q: 3.0)
    lv_rules = st.Rules(level=True)
    entry = 124.0
    data = {"5m": _five(lows={20: 122.5})}
    dist, lv = st._level_risk(db_session, lv_rules, data, {"spread": 0.1}, 1, entry, 2.0)
    assert dist == 3.0 and lv["dist"] == 3.0


def test_a_watch_signal_s_levels_use_it_too(db_session, engine):  # noqa: F811
    cfg = _strategy(db_session, target_r=0.9, level_stop=True)
    rules = st.Rules.of(cfg)
    data = {"5m": _five(lows={20: 121.5})}
    import unittest.mock as um
    with um.patch.object(st, "_side_risk", lambda *a, **k: 2.0):
        out = st._signal_levels(db_session, cfg, rules, data, None, 1, 124.0)
    assert out["risk"] == pytest.approx(2.7) and out["stop"] == pytest.approx(121.3)
    assert out["target"] == pytest.approx(124.0 + 0.9 * 2.7) and out["level_x"] == pytest.approx(1.35)


# ----------------------------------------------------------------- the board

def test_the_board_saves_it_off_by_default_never_with_a_supertrend_stop_and_records_the_change(db_session, engine):  # noqa: F811
    from app.api.strategy_routes import StrategyIn, create_strategy, update_strategy

    base = dict(name="f", pairs=[{"symbol": SYM, "asset_class": "crypto"}],
                entry_long=[[{"type": "trend", "tf": "1m", "params": {}}]], target_r=0.9)
    view = create_strategy(StrategyIn(**base), db_session)
    assert view.level_stop is False
    view = update_strategy(view.id, StrategyIn(**base, level_stop=True), db_session)
    assert view.level_stop is True
    edit = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).all()[-1].detail
    assert "stop past a nearby 5m level: off → on" in edit["changes"]
    view = update_strategy(view.id, StrategyIn(**base, level_stop=True,
                                               stop={"tf": "1m", "period": 10, "mult": 2.7, "buffer": 0.2}), db_session)
    assert view.level_stop is False                             # a SuperTrend stop sits on its own line


def test_a_lesson_never_switches_it(db_session, engine):  # noqa: F811
    from app.api.strategy_routes import _NOT_LEARNED, _config_of

    cfg = _strategy(db_session, level_stop=True)
    assert "level_stop" in _NOT_LEARNED and _config_of(cfg)["level_stop"] is True
