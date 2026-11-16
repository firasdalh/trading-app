"""'Not stretched' (2026-11-16, the user: "I don't want to change the strategy, I want to stop the bad trades"): the
entry rule the learner's strongest lesson on strategy 1 asks for - price already ran 3.5+ ATR our way in the last 15
minutes. It reads the learner's own run15 number, holds only up to its cap, mirrors for a sell, and the lesson's Apply
adds it to the BUY groups that trade (one click, undone the same way) - never on its own."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.agents.learning as L
import app.agents.strategy as st
from app.models.db import AgentRun, StrategyTradeFact
from tests.test_learning import NOW, _candles, _cond, _facts, _strategy

SPEC = st.CATALOG["max_run"]


def _rise(n_flat=60, n_up=15, step=0.12):
    return [100.0] * n_flat + [100.0 + step * (i + 1) for i in range(n_up)]


def _p(**kw):
    return st.clean_params(SPEC, kw)


def test_it_measures_the_learners_own_run15():
    closes = _rise()
    c = _candles(closes)
    b = st.Bars("1m", c)
    ask = closes[-1] + 0.03
    run = L.fingerprint(1, c, ask, 0.03, NOW)["run15"]
    assert run > 1
    # the cap a hair over the learner's number holds, a hair under it doesn't: the same number
    assert st._c_max_run(_p(atr=run + 0.01), 1, b, st.Ctx(now=NOW, ask=ask, bid=ask - 0.03))[0] is True
    ok, detail = st._c_max_run(_p(atr=run - 0.01), 1, b, st.Ctx(now=NOW, ask=ask, bid=ask - 0.03))
    assert ok is False and detail.startswith(f"{run:+.2f} ATR up since the close 15 bars back")


def test_a_sell_reads_it_mirrored():
    closes = _rise()
    c = _candles(closes)
    b = st.Bars("1m", c)
    bid = closes[-1]
    sell_run = L.fingerprint(-1, c, bid, 0.03, NOW)["run15"]
    assert sell_run < 0                                     # a rise is not a run a sell's way
    assert st._c_max_run(_p(atr=0.5), -1, b, st.Ctx(now=NOW, ask=bid + 0.03, bid=bid))[0] is True
    falling = [200.0 - x for x in closes]
    fb = st.Bars("1m", _candles(falling))
    r = L.fingerprint(-1, _candles(falling), falling[-1], 0.03, NOW)["run15"]
    assert r > 1
    assert st._c_max_run(_p(atr=r - 0.01), -1, fb, st.Ctx(now=NOW, ask=falling[-1] + 0.03, bid=falling[-1]))[0] is False
    assert st._c_max_run(_p(atr=r + 0.01), -1, fb, st.Ctx(now=NOW, ask=falling[-1] + 0.03, bid=falling[-1]))[0] is True


def test_it_waits_without_a_quote_or_bars():
    b = st.Bars("1m", _candles(_rise()))
    assert st._c_max_run(_p(), 1, b, st.Ctx(now=NOW))[0] is None
    short = st.Bars("1m", _candles([100.0] * 10))
    assert st._c_max_run(_p(), 1, short, st.Ctx(now=NOW, ask=100.0, bid=99.9))[0] is None


def test_the_board_takes_it_in_the_buy_rules_only_and_reads_it_plainly():
    from app.api.strategy_routes import StrategyIn

    ok = StrategyIn(entry_long=[[_cond("max_run", bars=15, atr=3.5)]])
    assert ok.entry_long[0][0].params == {"bars": 15, "atr": 3.5}
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[_cond("trend")]], exit_rules=[[_cond("max_run", bars=15, atr=3.5)]])
    assert any(c["type"] == "max_run" for c in st.catalog())
    cond = _cond("max_run", bars=15, atr=3.5)
    assert st.label(cond, "long") == "Not stretched: price at most 3.5 ATR above the close 15 bars back · 1m"
    assert st.label(cond, "short") == "Not stretched: price at most 3.5 ATR below the close 15 bars back · 1m"


def test_the_cap_is_where_the_lessons_high_part_starts():
    part = L.FEATURES["run15"][1]
    assert part({"fp": {"run15": L.RUN15_HIGH}})[0] == "high"
    assert part({"fp": {"run15": L.RUN15_HIGH - 0.001}})[0] == "mid"


def test_the_lesson_asks_for_it_in_the_groups_that_trade():
    cfg = {"pairs": [{"symbol": "BTCUSDm"}], "sides": "both", "hours": None,
           "entry_long": [[_cond("trend")], [_cond("spread", max_pct=30)]], "off_groups": {"entry_long": [0]},
           "target_r": 0.9, "stop_atr": 5.0}
    lesson = {"feature": "run15", "bucket": "high", "kind": "weak"}
    act = L.action_for(lesson, cfg)
    assert act["op"] == "max_run" and act["atr"] == 3.5 and act["done"] is False
    assert "Not stretched: at most 3.5 ATR" in act["text"]
    out = L.apply_change(cfg, act)
    assert out["entry_long"][0] == [_cond("trend")]                       # the group switched off: untouched
    assert out["entry_long"][1][-1] == {"type": "max_run", "tf": "1m", "params": {"bars": 15, "atr": 3.5}}
    assert cfg["entry_long"][1] == [_cond("spread", max_pct=30)]          # the original is never touched
    assert L.action_for(lesson, out)["done"] is True
    loose = {**cfg, "entry_long": [[_cond("trend")], [_cond("max_run", bars=15, atr=5.0)]]}
    assert L.action_for(lesson, loose)["done"] is False                   # a looser cap doesn't say it
    tight = L.apply_change(loose, act)
    assert [c for c in tight["entry_long"][1] if c["type"] == "max_run"] == [
        {"type": "max_run", "tf": "1m", "params": {"bars": 15, "atr": 3.5}}]
    other_bars = {**cfg, "entry_long": [[_cond("trend")], [_cond("max_run", bars=30, atr=3.0)]]}
    assert L.action_for(lesson, other_bars)["done"] is False              # 30 bars back is another reading
    # the winning parts of run15 ask for nothing
    assert L.action_for({"feature": "run15", "bucket": "mid", "kind": "strong"}, cfg) is None
    assert L.action_for({"feature": "run15", "bucket": "low", "kind": "weak"}, cfg) is None


def test_applied_on_a_click_and_undone(db_session):
    from app.api.strategy_routes import LearningAct, strategy_learning, strategy_learning_act

    cfg = _strategy(db_session)
    for f in _facts(key=lambda bad: {"run15": 5.0 if bad else 2.0}):
        db_session.add(StrategyTradeFact(key=f"k{f['id']}", strategy_id=cfg.id, symbol="BTCUSDm", side="long",
                                         opened_at=f["opened_at"], pnl=f["pnl"], r=f["r"], fp=f["fp"]))
    db_session.commit()
    view = strategy_learning(cfg.id, db_session)
    lesson = next(p for p in view["lessons"] if p["key"] == "run15:high")
    assert lesson["kind"] == "weak" and lesson["strength"] == "strong"
    assert lesson["action"]["op"] == "max_run" and not lesson["action"]["done"]
    db_session.refresh(cfg)
    assert not any(c["type"] == "max_run" for g in cfg.entry_long for c in g)   # looking changes nothing

    strategy_learning_act(cfg.id, LearningAct(key="run15:high", action="apply"), db_session)
    db_session.refresh(cfg)
    assert cfg.entry_long[1][-1] == {"type": "max_run", "tf": "1m", "params": {"bars": 15, "atr": 3.5}}
    assert cfg.entry_long[0] == [_cond("trend")]
    assert cfg.risk_pct == 0.05 and cfg.live
    edit = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).one()
    assert edit.detail["by"] == "lesson"
    assert any("Not stretched: price at most 3.5 ATR above the close 15 bars back" in ch for ch in edit.detail["changes"])

    strategy_learning_act(cfg.id, LearningAct(key="run15:high", action="undo"), db_session)
    db_session.refresh(cfg)
    assert not any(c["type"] == "max_run" for g in cfg.entry_long for c in g)


def test_the_engine_reads_it_like_any_entry_rule():
    """Through evaluate: a group with only 'Not stretched' holds for a buy after a small rise, not after a big one."""
    small = _candles([100.0] * 60 + [100.0 + 0.01 * i for i in range(15)])
    big = _candles(_rise(step=0.5))
    group = [[_cond("max_run", bars=15, atr=3.5)]]
    for c, want in ((small, True), (big, False)):
        bars = {"1m": st.Bars("1m", c)}
        q = c[-1].close
        ctx = st.Ctx(now=NOW + timedelta(seconds=1), ask=q + 0.01, bid=q, risk=1.0)
        res = st.evaluate(group, 1, bars, ctx, "long")
        assert res["ok"] is want, res
