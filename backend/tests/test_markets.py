"""Markets (2026-11-16, the user: "if sells are better on crypto, let it watch that too - let it watch the markets
themselves, so we get control in the strategy per market"): a "Markets" entry rule says which markets buys and sells
may open on; every rule gets the pair's market; the learner splits its trades by market and by market + side, and a
losing "buys on crypto" offers the rule with crypto buys off - applied only on a click, undone the same way."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.agents.learning as L
import app.agents.strategy as st
from app.models.db import AgentRun, Position, StrategyTradeFact
from tests.test_learning import NOW, _candles, _cond, _facts, _strategy
from tests.test_strategy import FALLING, RISING, _Broker, engine  # noqa: F401 - the engine fixture
from tests.test_strategy import NOW as ST_NOW
from tests.test_strategy import _strategy as _engine_strategy

ALL_ON = {f"{pre}{k}": 1 for pre in ("b_", "s_") for k, _n in st.MARKETS}


def _mk(*off):
    """The Markets rule's chips: all on, but the ones named (b_crypto = buys on crypto off)."""
    return {**ALL_ON, **{k: 0 for k in off}}


def _read(params, s, ac):
    spec = st.CATALOG["markets"]
    b = st.Bars("1m", _candles([100.0] * 30))
    return st._c_markets(st.clean_params(spec, params), s, b, st.Ctx(now=NOW, asset_class=ac))


# --------------------------------------------------------------------------- the rule

def test_a_market_ticked_off_for_a_side_stops_only_that_side():
    p = _mk("b_crypto")
    assert _read(p, 1, "crypto") == (False, "buys on crypto: switched off")
    assert _read(p, -1, "crypto") == (True, "sells on crypto: on")
    assert _read(p, 1, "forex") == (True, "buys on forex: on")
    both = _mk("b_index", "s_index")
    assert _read(both, 1, "index")[0] is False and _read(both, -1, "index")[0] is False
    assert _read(_mk(), 1, None) == (None, "no market for this pair")     # never a hold without the pair's market


def test_it_reads_plainly_and_only_as_an_entry_rule():
    from app.api.strategy_routes import StrategyIn

    assert st.label(_cond("markets", **_mk()), "long") == "Markets - buys on every market · sells on every market"
    assert st.label(_cond("markets", **_mk("b_crypto")), "long") == \
        "Markets - buys on every market but crypto · sells on every market"
    few = _mk("s_stock", "s_crypto", "s_forex", "s_metal")
    assert st.label(_cond("markets", **few), "short").endswith("sells on energy, indices")
    none = _mk(*(f"b_{k}" for k, _n in st.MARKETS))
    assert "buys on no market" in st.label(_cond("markets", **none), "long")
    ok = StrategyIn(entry_long=[[_cond("markets", **_mk("b_crypto"))]])
    assert ok.entry_long[0][0].params["b_crypto"] == 0 and ok.entry_long[0][0].params["s_crypto"] == 1
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[_cond("trend")]], exit_rules=[[_cond("markets", **_mk())]])
    spec = next(c for c in st.catalog() if c["type"] == "markets")
    assert [p["key"] for p in spec["params"]][:6] == [f"b_{k}" for k, _n in st.MARKETS]
    assert all(p["kind"] == "pick" for p in spec["params"])


def test_the_engine_hands_every_rule_the_pairs_market(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(RISING)
    cfg = _engine_strategy(db_session, entry_long=[[_cond("trend"), _cond("markets", **_mk("b_crypto"))]])
    out = st.run_strategy(db_session, cfg, now=ST_NOW)
    assert calls["assess"] == [] and not out[0].get("opened")             # BTCUSDm is crypto: no buy
    cfg.entry_long = [[_cond("trend"), _cond("markets", **_mk())]]
    db_session.commit()
    st._BAR_CACHE.clear()
    st._LAST_LOOK.clear()
    out = st.run_strategy(db_session, cfg, now=ST_NOW + timedelta(minutes=1))
    assert out[0].get("opened") == "long"


def test_the_board_check_reads_it_for_the_pair(db_session, engine):
    _, holder = engine
    holder["broker"] = _Broker(FALLING)
    rules = st.Rules(timeframe="1m", entry_long=[[_cond("markets", **_mk("s_crypto"))]])
    out = st.check_now(db_session, rules, "BTCUSDm", "crypto", now=ST_NOW)
    assert out["long"]["ok"] is True
    cond = out["short"]["groups"][0]["conditions"][0]
    assert out["short"]["ok"] is False and cond["detail"] == "sells on crypto: switched off"
    st._BAR_CACHE.clear()
    assert st.check_now(db_session, rules, "XAUUSDm", "metal", now=ST_NOW)["short"]["ok"] is True


# --------------------------------------------------------------------------- the learner

def test_the_review_splits_trades_by_market_and_by_market_and_side():
    facts = _facts(key=lambda bad: {})
    for f in facts:
        f["market"] = "crypto" if f["id"] % 2 else "forex"
    parts = {p["key"]: p for p in L.find_parts(facts)}
    assert {"market:crypto", "market:forex", "market_side:crypto:long", "market_side:forex:long"} <= set(parts)
    assert parts["market_side:crypto:long"]["label"] == "buys on crypto"
    assert parts["market:forex"]["label"] == "trades on forex"
    for f in facts:
        f.pop("market")
    assert not any(p["feature"] in ("market", "market_side") for p in L.find_parts(facts))   # no market: not split


def test_each_trade_gets_its_market_even_from_a_pair_taken_off_since(db_session):
    cfg = _strategy(db_session)                     # pairs: BTCUSDm crypto, XAUUSDm metal
    db_session.add(Position(symbol="USOILm", asset_class="energy", direction="long", qty=1.0, entry_price=60.0,
                            status="closed", source=st.strategy_source(cfg), opened_at=NOW - timedelta(days=2)))
    db_session.add(Position(symbol="EURUSDm", asset_class="forex", direction="long", qty=1.0, entry_price=1.1,
                            status="closed", source="manual", opened_at=NOW - timedelta(days=2)))
    db_session.commit()
    mk = L.markets_of(db_session, cfg)
    assert mk == {"BTCUSDM": "crypto", "XAUUSDM": "metal",       # its pairs
                  "USOILM": "energy"}                            # its own trade on a pair it no longer trades
    # (EURUSDm: someone else's trade, not the strategy's)


def _cfg(**kw):
    base = {"pairs": [{"symbol": "BTCUSDm", "asset_class": "crypto"}, {"symbol": "XAUUSDm", "asset_class": "metal"}],
            "sides": "both", "hours": None, "timeframe": "1m", "mirror_short": True, "entry_short": [],
            "entry_long": [[_cond("trend")], [_cond("spread", max_pct=30)]], "off_groups": {"entry_long": [0]},
            "target_r": 0.9, "stop_atr": 5.0}
    base.update(kw)
    return base


def test_a_losing_market_side_asks_for_that_side_off_there_only():
    cfg = _cfg()
    lesson = {"feature": "market_side", "bucket": "crypto:long", "kind": "weak"}
    act = L.action_for(lesson, cfg)
    assert act["op"] == "market_side" and act["done"] is False
    assert act["text"].startswith("Stop opening buys on crypto - sells only there")
    out = L.apply_change(cfg, act)
    assert out["entry_long"][0] == [_cond("trend")]                           # the group switched off: untouched
    added = out["entry_long"][1][-1]
    assert added["type"] == "markets" and added["params"]["b_crypto"] == 0
    assert sum(1 for v in added["params"].values() if v == 0) == 1             # every other chip stays on
    assert L.action_for(lesson, out)["done"] is True
    sells = L.apply_change(out, L.action_for({"feature": "market_side", "bucket": "crypto:short", "kind": "weak"}, out))
    rows = [c for c in sells["entry_long"][1] if c["type"] == "markets"]
    assert len(rows) == 1 and rows[0]["params"]["b_crypto"] == 0 and rows[0]["params"]["s_crypto"] == 0
    # nothing to do: the market isn't traded, or the side isn't
    assert L.action_for({"feature": "market_side", "bucket": "forex:long", "kind": "weak"}, cfg)["done"] is True
    assert L.action_for(lesson, _cfg(sides="short"))["done"] is True
    # SELL rules of their own: a losing "sells on crypto" goes there
    own = _cfg(mirror_short=False, entry_short=[[_cond("trend")]], off_groups={"entry_long": [0], "entry_short": []})
    s_act = L.action_for({"feature": "market_side", "bucket": "crypto:short", "kind": "weak"}, own)
    assert "SELL rules" in s_act["text"]
    s_out = L.apply_change(own, s_act)
    assert s_out["entry_short"][0][-1]["params"]["s_crypto"] == 0 and s_out["entry_long"] == own["entry_long"]
    # a winning part asks for nothing
    assert L.action_for({"feature": "market_side", "bucket": "crypto:short", "kind": "sweet"}, cfg) is None


def test_a_losing_market_asks_to_take_its_pairs_off():
    cfg = _cfg()
    act = L.action_for({"feature": "market", "bucket": "crypto", "kind": "weak"}, cfg)
    assert act == {"op": "remove_market", "market": "crypto", "text": "Stop trading crypto: take BTCUSDm off",
                   "done": False}
    assert [p["symbol"] for p in L.apply_change(cfg, act)["pairs"]] == ["XAUUSDm"]
    with pytest.raises(ValueError):
        L.apply_change(_cfg(pairs=[{"symbol": "BTCUSDm", "asset_class": "crypto"}]), act)
    assert L.action_for({"feature": "market", "bucket": "forex", "kind": "weak"}, cfg)["done"] is True


def test_applied_on_a_click_and_undone(db_session):
    from app.api.strategy_routes import LearningAct, strategy_learning, strategy_learning_act

    cfg = _strategy(db_session)
    # 60 losing buys on crypto (BTCUSDm long); the winners: gold buys and crypto sells
    for f in _facts(key=lambda bad: {"bad": bad}):
        bad = f["fp"]["bad"]
        sym, side = ("BTCUSDm", "long") if bad else (("XAUUSDm", "long") if f["id"] % 2 else ("BTCUSDm", "short"))
        db_session.add(StrategyTradeFact(key=f"k{f['id']}", strategy_id=cfg.id, symbol=sym, side=side,
                                         opened_at=f["opened_at"], pnl=f["pnl"], r=f["r"], fp=None))
    db_session.commit()
    view = strategy_learning(cfg.id, db_session)
    lesson = next(p for p in view["lessons"] if p["key"] == "market_side:crypto:long")
    assert lesson["kind"] == "weak" and lesson["strength"] == "strong" and lesson["title"] == "Losing: buys on crypto"
    assert lesson["action"]["op"] == "market_side" and not lesson["action"]["done"]
    db_session.refresh(cfg)
    assert not any(c["type"] == "markets" for g in cfg.entry_long for c in g)     # looking changes nothing

    strategy_learning_act(cfg.id, LearningAct(key="market_side:crypto:long", action="apply"), db_session)
    db_session.refresh(cfg)
    rule = cfg.entry_long[1][-1]
    assert rule["type"] == "markets" and rule["params"]["b_crypto"] == 0 and rule["params"]["s_crypto"] == 1
    assert cfg.entry_long[0] == [_cond("trend")] and cfg.risk_pct == 0.05 and cfg.live
    edit = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).one()
    assert edit.detail["by"] == "lesson"
    assert any("Markets - buys on every market but crypto · sells on every market" in ch for ch in edit.detail["changes"])

    strategy_learning_act(cfg.id, LearningAct(key="market_side:crypto:long", action="undo"), db_session)
    db_session.refresh(cfg)
    assert not any(c["type"] == "markets" for g in cfg.entry_long for c in g)
