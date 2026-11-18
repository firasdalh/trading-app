"""A strategy that studies its own trades (app/agents/learning.py, asked 2026-11-12: "make the strategy learn from
itself"): the entry's fingerprint reads the trade's way, the "not chasing" rule counts closes, a part of the
fingerprint becomes a lesson only far from luck and in both halves of its trades, the what-ifs replay other targets,
a lesson changes the strategy only on the user's click - never its risk - and can be undone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

import app.agents.learning as L
import app.agents.strategy as st
from app.models.db import AgentRun, StrategyConfig, StrategyTradeFact
from app.models.schemas import Candle

NOW = datetime(2026, 11, 13, 12, 0, 30, tzinfo=timezone.utc)


def _candles(closes: list[float], end: datetime = NOW) -> list[Candle]:
    start = end - timedelta(minutes=len(closes) + 1)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(ts=start + timedelta(minutes=i), open=prev, high=max(prev, c) + 0.05,
                          low=min(prev, c) - 0.05, close=c, volume=100))
        prev = c
    return out


def _cond(type_, tf="1m", **params):
    return {"type": type_, "tf": tf, "params": params}


def _strategy(session, **kw) -> StrategyConfig:
    base = dict(name="s1", enabled=True, pairs=[{"symbol": "BTCUSDm", "asset_class": "crypto"},
                                                {"symbol": "XAUUSDm", "asset_class": "metal"}], timeframe="1m",
                sides="both", entry_long=[[_cond("trend")], [_cond("spread", max_pct=30)]], entry_short=[],
                mirror_short=True, exit_rules=[], risk_pct=0.05, stop_atr=5.0, target_r=0.9, max_trades_day=180,
                cooldown_minutes=10, loss_cooldown_minutes=30, live=True, off_groups={"entry_long": [0]})
    base.update(kw)
    cfg = StrategyConfig(**base)
    session.add(cfg)
    session.commit()
    return cfg


# --------------------------------------------------------------------------- the fingerprint

def test_the_fingerprint_reads_the_trades_way():
    rising = [100 + 0.1 * i for i in range(120)]
    c = _candles(rising)
    buy = L.fingerprint(1, c, rising[-1] + 0.02, 0.05, NOW)
    sell = L.fingerprint(-1, c, rising[-1] - 0.02, 0.05, NOW)
    assert buy["streak"] == 119 and sell["streak"] == 0            # every close higher: a buy chases, a sell doesn't
    assert buy["ext20"] > 0 > sell["ext20"] and buy["run15"] > 0 > sell["run15"]
    assert buy["rsi"] > 90 and sell["rsi"] < 10                    # RSI read from the trade's side
    assert buy["pos60"] > 0.9 and sell["pos60"] < 0.1
    assert buy["hour"] == 12 and buy["session"] == "london" and buy["spread_pct"] > 0
    assert L.fingerprint(1, c[:50], 105.0, 0.05, NOW) is None       # too little history


def test_not_chasing_counts_closes_not_colours():
    spec = st.CATALOG["dont_chase"]
    closes = [100.0] * 30 + [99.0, 100.0, 100.5, 101.0]
    b = st.Bars("1m", _candles(closes))
    p1, p3 = st.clean_params(spec, {"max": 1}), st.clean_params(spec, {"max": 3})
    ok, detail = st._c_dont_chase(p1, 1, b, st.Ctx(now=NOW))
    assert ok is False and detail.startswith("3 closes in a row higher")
    assert st._c_dont_chase(p3, 1, b, st.Ctx(now=NOW))[0] is True
    assert st._c_dont_chase(p1, -1, b, st.Ctx(now=NOW))[0] is True      # for a sell nothing went its way


def test_the_board_takes_not_chasing_in_the_buy_rules_only():
    from app.api.strategy_routes import StrategyIn

    ok = StrategyIn(entry_long=[[_cond("dont_chase", max=2)]])
    assert ok.entry_long[0][0].params == {"max": 2}
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[_cond("trend")]], exit_rules=[[_cond("dont_chase", max=2)]])
    assert any(c["type"] == "dont_chase" for c in st.catalog())


# --------------------------------------------------------------------------- the review

GOOD = [0.9, 0.9, -1.0, 0.9, -1.0]      # 60% won at +0.9R
BAD = [-1.0, -1.0, 0.9, -1.0]           # 25% won


def _facts(n_good=150, n_bad=60, bad_first_half_only=False, key=lambda bad: {"streak": 5 if bad else 1}):
    """Interleaved over time: every fourth trade is in the bad part."""
    out, g, b = [], 0, 0
    for i in range(n_good + n_bad):
        bad = (i % 4 == 3 and b < n_bad) or g >= n_good
        if bad:
            r = BAD[b % 4]
            if bad_first_half_only and b >= n_bad // 2:
                r = 0.9 if b % 4 else -1.0                         # the newer half wins
            b += 1
        else:
            r = GOOD[g % 5]
            g += 1
        out.append({"id": i, "symbol": "BTCUSDm", "side": "long", "opened_at": NOW - timedelta(hours=400 - i),
                    "pnl": r * 50, "r": r, "fp": key(bad), "whatif": None})
    return out


def test_a_losing_part_is_a_lesson_only_far_from_luck_and_in_both_halves():
    parts = {p["key"]: p for p in L.find_parts(_facts())}
    weak = parts["streak:4+"]
    assert weak["kind"] == "weak" and weak["strength"] == "strong" and weak["t"] <= -L.STRONG_T
    assert weak["n"] == 60 and weak["r_sum"] < 0 and all(h < 0 for h in weak["halves"])
    assert parts["streak:0-1"]["kind"] == "sweet"
    # the same part, losing only in its older half: no lesson
    mixed = {p["key"]: p for p in L.find_parts(_facts(bad_first_half_only=True))}
    assert mixed["streak:4+"]["kind"] is None


def test_nothing_is_judged_below_the_minimum(db_session):
    from app.api.strategy_routes import _config_of

    cfg = _strategy(db_session)
    for f in _facts(n_good=30, n_bad=10):
        db_session.add(StrategyTradeFact(key=f"k{f['id']}", strategy_id=cfg.id, symbol=f["symbol"], side="long",
                                         opened_at=f["opened_at"], pnl=f["pnl"], r=f["r"], fp=f["fp"]))
    db_session.commit()
    view = L.build(db_session, cfg, _config_of(cfg), NOW)
    assert view["trades"] == 40 and view["parts"] == [] and view["lessons"] == []


def test_the_what_ifs_suggest_another_target_only_when_it_wins_in_both_halves():
    def facts(second_half_worse=False):
        out = []
        for i in range(100):
            gain = 0.1 if i % 2 else 0.3
            if second_half_worse and i >= 50:
                gain = -0.3
            w = {L.whatif_key(t, k): 0.0 for k in L.WHATIF_STOPS for t in L.WHATIF_TARGETS}
            w["t0.9|s1"], w["t1.2|s1"] = 0.1, 0.1 + gain
            out.append({"opened_at": NOW - timedelta(hours=200 - i), "symbol": "BTCUSDm", "side": "long", "r": 0.1,
                        "pnl": 5.0, "whatif": w})
        return out
    table = L.whatif_table(facts(), 0.9, (10, 30, 3))
    assert table["current"] == "t0.9|s1" and table["best"] == "t1.2|s1" and table["gain"]["r"] == 20.0
    assert table["cells"]["t1.2|s1"]["n"] == 100 and all(x > 0 for x in table["gain"]["halves"])
    lesson = L._whatif_lesson(table)
    assert lesson["strength"] == "strong" and lesson["target"] == 1.2 and lesson["stop"] == 1.0
    act = L.action_for(lesson, {"target_r": 0.9, "stop_atr": 5.0})
    assert act["op"] == "levels" and act["target"] == 1.2 and act["stop_atr"] == 5.0 and not act["done"]
    assert L._whatif_lesson(L.whatif_table(facts(second_half_worse=True), 0.9, (10, 30, 3))) is None


def test_a_longer_hold_loses_the_entries_that_came_meanwhile():
    """The 3m test (2026-11-12): the same edge per trade, half the trades. A setting that holds a pair for two hours
    can't take the entry 30 minutes later on it; one that is out in 10 minutes can."""
    fs = [{"opened_at": NOW + timedelta(minutes=30 * i), "symbol": "BTCUSDm", "side": "long", "r": 0.9,
           "whatif": {"t0.9|s1": [0.9, 600], "t2|s1.5": [2.0, 7200]}} for i in range(8)]
    assert len(L.sequenced(fs, "t0.9|s1", (10, 30, 3))) == 8
    assert len(L.sequenced(fs, "t2|s1.5", (10, 30, 3))) == 2          # in at 0 and 2:30, each out 2 h later
    # a loss keeps the same side waiting longer; the cap on open trades counts every pair
    lost = [{**f, "whatif": {"k": [-1.0, 600]}} for f in fs]
    assert len(L.sequenced(lost, "k", (10, 30, 3))) == 4
    pairs = [{**f, "symbol": f"P{i}", "whatif": {"k": [0.9, 7200]}} for i, f in enumerate(fs)]
    assert len(L.sequenced(pairs, "k", (0, 5, 3))) == 6       # the 4th and the 8th find 3 trades still open


def test_the_what_ifs_replay_every_target_and_stop_on_the_prices():
    path = [(NOW + timedelta(minutes=i), 100 + 0.01 * i + 0.05, 100 + 0.01 * i - 0.05, 100 + 0.01 * i, 0.02)
            for i in range(1, 600)]
    w = L.whatifs(1, 100.0, 1.0, path, NOW + timedelta(hours=24), NOW + timedelta(hours=30), at=NOW)
    # a slow rise (+0.01 a minute, highs 0.05 above) reaches every target, the further ones later
    assert w["t0.9|s1"][0] == 0.9 and 85 * 60 <= w["t0.9|s1"][1] <= 86 * 60
    assert w["t2|s1.5"][0] == 2.0 and 295 * 60 <= w["t2|s1.5"][1] <= 296 * 60
    falling = [(t, 200 - lo, 200 - h, 200 - c, sp) for t, h, lo, c, sp in path]
    w2 = L.whatifs(1, 199.9, 1.0, falling, NOW + timedelta(hours=24), NOW + timedelta(hours=30))
    assert {v[0] for v in w2.values()} == {-1.0}
    # still inside its window with a level not reached: not final yet
    assert L.whatifs(1, 100.0, 1.0, path[:30], NOW + timedelta(hours=24), NOW + timedelta(minutes=31)) is None


# --------------------------------------------------------------------------- what a lesson changes

def test_each_change_is_made_on_the_strategy_and_checked():
    cfg = {"pairs": [{"symbol": "BTCUSDm"}, {"symbol": "XAUUSDm"}], "sides": "both", "hours": None,
           "entry_long": [[_cond("trend")], [_cond("spread", max_pct=30)]], "off_groups": {"entry_long": [0]},
           "target_r": 0.9, "stop_atr": 5.0}
    assert L.apply_change(cfg, {"op": "remove_pair", "symbol": "BTCUSDm"})["pairs"] == [{"symbol": "XAUUSDm"}]
    with pytest.raises(ValueError):
        L.apply_change({**cfg, "pairs": [{"symbol": "BTCUSDm"}]}, {"op": "remove_pair", "symbol": "BTCUSDm"})
    # hours: around the clock -> every hour but those; picked hours lose them; sessions become hours
    out = L.apply_change(cfg, {"op": "remove_hours", "hours": [13]})
    assert out["hours"]["hours_utc"] == [h for h in range(24) if h != 13]
    out = L.apply_change({**cfg, "hours": {"hours_utc": [8, 9, 13], "tz": "Asia/Dubai", "crypto_24h": True}},
                         {"op": "remove_hours", "hours": [13]})
    assert out["hours"] == {"hours_utc": [8, 9], "tz": "Asia/Dubai", "crypto_24h": True}
    out = L.apply_change({**cfg, "hours": {"sessions": ["london", "new_york"], "tz": None}},
                         {"op": "remove_hours", "hours": [13, 14]})
    assert out["hours"]["hours_utc"] == [8, 9, 10, 11, 12] + list(range(15, 21))
    with pytest.raises(ValueError):
        L.apply_change({**cfg, "hours": {"start": "09:00", "end": "12:00", "tz": "UTC"}},
                       {"op": "remove_hours", "hours": [10]})
    # not chasing: into every BUY group that trades, not the one switched off; an existing one is tightened
    out = L.apply_change(cfg, {"op": "dont_chase", "max": 1})
    assert out["entry_long"][0] == [_cond("trend")]
    assert out["entry_long"][1][-1] == {"type": "dont_chase", "tf": "1m", "params": {"max": 1}}
    again = L.apply_change(out, {"op": "dont_chase", "max": 3})
    assert [c for c in again["entry_long"][1] if c["type"] == "dont_chase"] == [
        {"type": "dont_chase", "tf": "1m", "params": {"max": 3}}]
    tight = L.apply_change(cfg, {"op": "spread_max", "pct": 20})
    assert tight["entry_long"][1] == [_cond("spread", max_pct=20)]
    assert cfg["entry_long"][1] == [_cond("spread", max_pct=30)]          # the original is never touched
    # done: the strategy already does it
    lesson = {"feature": "streak", "bucket": "2-3", "kind": "weak"}
    assert L.action_for(lesson, cfg)["done"] is False and L.action_for(lesson, out)["done"] is True
    assert L.action_for({"feature": "pair", "bucket": "EURUSDm", "kind": "weak"}, cfg)["done"] is True
    assert L.action_for({"feature": "rsi", "bucket": "high", "kind": "weak"}, cfg) is None   # no rule says it


def _seed_pair_lesson(session, cfg):
    """210 trades: BTCUSDm loses (25% won), XAUUSDm wins - a strong 'stop trading BTCUSDm'."""
    for f in _facts(key=lambda bad: {"bad": bad}):
        sym = "BTCUSDm" if f["fp"]["bad"] else "XAUUSDm"
        session.add(StrategyTradeFact(key=f"k{f['id']}", strategy_id=cfg.id, symbol=sym, side="long",
                                      opened_at=f["opened_at"], pnl=f["pnl"], r=f["r"], fp=None))
    session.commit()


def test_a_lesson_changes_the_strategy_only_on_a_click_and_can_be_undone(db_session):
    from app.api.strategy_routes import LearningAct, strategy_learning, strategy_learning_act

    cfg = _strategy(db_session)
    _seed_pair_lesson(db_session, cfg)
    view = strategy_learning(cfg.id, db_session)
    lesson = next(p for p in view["lessons"] if p["key"] == "pair:BTCUSDm")
    assert lesson["kind"] == "weak" and lesson["strength"] == "strong"
    assert lesson["action"]["op"] == "remove_pair" and not lesson["action"]["done"]
    db_session.refresh(cfg)
    assert [p["symbol"] for p in cfg.pairs] == ["BTCUSDm", "XAUUSDm"]       # looking changes nothing

    out = strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="apply"), db_session)
    db_session.refresh(cfg)
    assert [p["symbol"] for p in cfg.pairs] == ["XAUUSDm"]
    assert cfg.risk_pct == 0.05 and cfg.cooldown_minutes == 10 and cfg.loss_cooldown_minutes == 30 and cfg.live
    edit = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).one()
    assert edit.detail["by"] == "lesson" and edit.detail["changes"] == ["− pairs: BTCUSDm"]
    done = next(p for p in out["learning"]["lessons"] if p["key"] == "pair:BTCUSDm")
    assert done["status"] == "applied" and done["action"]["done"]
    assert out["learning"]["applied"][0]["key"] == "pair:BTCUSDm"
    with pytest.raises(HTTPException) as again:
        strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="apply"), db_session)
    assert again.value.status_code == 409

    strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="undo"), db_session)
    db_session.refresh(cfg)
    assert [p["symbol"] for p in cfg.pairs] == ["BTCUSDm", "XAUUSDm"]


def test_an_undo_waits_when_the_strategy_was_changed_since(db_session):
    from app.api.strategy_routes import LearningAct, strategy_learning_act

    cfg = _strategy(db_session)
    _seed_pair_lesson(db_session, cfg)
    strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="apply"), db_session)
    cfg.pairs = [{"symbol": "XAUUSDm", "asset_class": "metal"}, {"symbol": "EURUSDm", "asset_class": "forex"}]
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="undo"), db_session)
    assert exc.value.status_code == 409


def test_only_a_strong_lesson_can_be_applied_and_an_ignored_one_steps_aside(db_session):
    from app.api.strategy_routes import LearningAct, strategy_learning, strategy_learning_act

    cfg = _strategy(db_session)
    _seed_pair_lesson(db_session, cfg)
    with pytest.raises(HTTPException) as exc:
        strategy_learning_act(cfg.id, LearningAct(key="pair:EURUSDm", action="apply"), db_session)
    assert exc.value.status_code == 404
    out = strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="ignore"), db_session)
    assert [p["key"] for p in out["learning"]["ignored"]] == ["pair:BTCUSDm"]
    assert "pair:BTCUSDm" not in [p["key"] for p in out["learning"]["lessons"]]
    out = strategy_learning_act(cfg.id, LearningAct(key="pair:BTCUSDm", action="unignore"), db_session)
    assert "pair:BTCUSDm" in [p["key"] for p in out["learning"]["lessons"]]
    db_session.refresh(cfg)
    assert len(cfg.pairs) == 2
    # a "watching" lesson has no button: refused if asked anyway
    for p in strategy_learning(cfg.id, db_session)["lessons"]:
        if p["strength"] == "watching":
            with pytest.raises(HTTPException):
                strategy_learning_act(cfg.id, LearningAct(key=p["key"], action="apply"), db_session)


def test_a_lesson_never_changes_risk(db_session):
    from app.api.strategy_routes import _config_of, _save_learned

    cfg = _strategy(db_session)
    with pytest.raises(HTTPException) as exc:
        _save_learned(db_session, cfg, {**_config_of(cfg), "risk_pct": 0.06}, "lesson", "x")
    assert exc.value.status_code == 500
    db_session.refresh(cfg)
    assert cfg.risk_pct == 0.05


# --------------------------------------------------------------------------- the tick, on the broker's history

class _Mt5:
    COPY_TICKS_ALL = 1

    def __init__(self, t0: datetime, minutes: int, price):
        self.rates = []
        for i in range(minutes):
            t = t0 + timedelta(minutes=i)
            p = price(t)
            self.rates.append({"time": int(t.timestamp()), "open": p - 0.01, "high": p + 0.05, "low": p - 0.05,
                               "close": p, "tick_volume": 10})
        self.price = price

    def copy_rates_range(self, sym, tf, start, end):
        return [r for r in self.rates if start.timestamp() <= r["time"] < end.timestamp()]

    def copy_ticks_range(self, sym, start, end, flags):
        out, t = [], start
        while t < end:
            p = self.price(t)
            out.append({"time_msc": int(t.timestamp() * 1000), "bid": p, "ask": p + 0.02})
            t += timedelta(seconds=10)
        return out


class _HistBroker:
    name = "hist"

    def __init__(self, mt5):
        self._mt5 = mt5

    def _resolve_symbol(self, s):
        return s

    def _tf(self, tf):
        return tf


def test_the_tick_keeps_each_trade_once_with_its_fingerprint_and_what_ifs(db_session):
    from app.api.strategy_routes import _config_of

    cfg = _strategy(db_session)
    t_old, t_new = NOW - timedelta(hours=30), NOW - timedelta(hours=2)
    t0 = NOW - timedelta(hours=40)

    def price(t):
        return 100 + 0.01 * (t - t0).total_seconds() / 60

    broker = _HistBroker(_Mt5(t0, 40 * 60, price))
    old = SimpleNamespace(symbol="BTCUSDm", direction="long", opened_at=t_old, closed_at=t_old + timedelta(minutes=90),
                          realized_pnl=45.0, entry_price=price(t_old), source=f"strategy:{cfg.id}")
    new = SimpleNamespace(symbol="BTCUSDm", direction="long", opened_at=t_new, closed_at=t_new + timedelta(minutes=50),
                          realized_pnl=-50.0, entry_price=price(t_new), source=f"strategy:{cfg.id}")
    other = SimpleNamespace(symbol="BTCUSDm", direction="long", opened_at=t_new, closed_at=t_new, realized_pnl=9.0,
                            entry_price=1.0, source="manual")
    row = SimpleNamespace(initial_risk_amount=50.0, risk_amount=50.0)
    # the engine's own logs: the old trade's has no fingerprint (opened before it wrote one), the new one's has
    db_session.add(AgentRun(agent="strategy", symbol="BTCUSDm", event="opened", created_at=t_old.replace(tzinfo=None),
                            detail={"strategy_id": cfg.id, "side": "long", "entry": price(t_old),
                                    "stop": price(t_old) - 1.0, "target": price(t_old) + 0.9}))
    db_session.add(AgentRun(agent="strategy", symbol="BTCUSDm", event="opened", created_at=t_new.replace(tzinfo=None),
                            detail={"strategy_id": cfg.id, "side": "long", "entry": price(t_new),
                                    "stop": price(t_new) - 1.0, "fp": {"v": 1, "streak": 2, "hour": 10}}))
    db_session.commit()
    book = [(old, row), (new, row), (other, None)]
    out = L.learning_tick(db_session, now=NOW, broker_for=lambda ac: broker, config_of=_config_of, book=book)
    assert out["new"] == 2
    facts = {f.opened_at.replace(tzinfo=timezone.utc): f for f in db_session.scalars(select(StrategyTradeFact)).all()}
    a, b = facts[t_old], facts[t_new]
    assert a.r == 0.9 and b.r == -1.0
    assert a.fp_from == "history" and a.fp["streak"] > 4 and a.fp["hour"] == t_old.hour
    # the engine's own (version 1) parts stay as it wrote them; version 2's are added from the history
    assert b.fp_from == "entry" and {k: b.fp[k] for k in ("streak", "hour")} == {"streak": 2, "hour": 10}
    assert b.fp["v"] == L.FP_VERSION and b.fp["chan1m"] is not None and set(L.FP2_KEYS) <= set(b.fp)
    assert abs(a.risk_dist - 1.0) < 1e-9 and a.whatif["t0.9|s1"][0] == 0.9 and a.whatif["t2|s1.5"][0] == 2.0
    assert b.whatif is None                                   # its 24 h are not over: replayed later, once
    again = L.learning_tick(db_session, now=NOW, broker_for=lambda ac: broker, config_of=_config_of, book=book)
    assert again["new"] == 0 and len(db_session.scalars(select(StrategyTradeFact)).all()) == 2
