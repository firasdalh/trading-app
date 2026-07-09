"""AI DECIDER: the deterministic engine is the analyst; the AI builds two scenarios each with a trade
plan, and the DETERMINISTIC decider picks the best TRADEABLE one (adequate R:R), not just the highest
probability. Covers the open/arm/stand-aside translation, the R:R-aware pick, and the guardrail fallback.
The decision brief is patched out (it needs a broker/DB); we test the decision->proposal logic."""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.ai_decider as dec
from app.agents.ai_decider import _AiScenario, _DecisionLLM, ai_decide_trade
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import FundamentalRead, TechnicalRead, TimeframeRead, TradeProposal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _tech(price: float = 100.0, atr_v: float = 2.0, macro: str = "up") -> TechnicalRead:
    ind = {"last_close": price, "atr14": atr_v, "ema20": price + 1, "ema50": price - 1, "adx": 30.0}
    macro_ind = {"last_close": price, "atr14": atr_v,
                 "ema20": price + 1 if macro == "up" else price - 1,
                 "ema50": price - 1 if macro == "up" else price + 1}
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators=ind,
                      support_levels=[price - 5], resistance_levels=[price + 5]),
        TimeframeRead(timeframe="1d", trend=macro, indicators=macro_ind),
    ])


def _fund() -> FundamentalRead:
    return FundamentalRead(symbol="X", bias=TradingBias.NEUTRAL)


def _scn(label="A", direction="up", prob=60, action="none", trigger=None, stop=None, tp=None) -> _AiScenario:
    return _AiScenario(label=label, direction=direction, probability=prob, action=action,
                       trigger_price=trigger, stop_loss=stop, take_profit=tp,
                       path="trigger -> target", reasoning="cited facts")


def _dec(scenarios, rationale="desk read", risks=("news",)) -> _DecisionLLM:
    return _DecisionLLM(scenarios=list(scenarios), rationale=rationale, key_risks=list(risks))


def _run(monkeypatch, decision, price=100.0, macro="up", facts=None):
    monkeypatch.setattr(dec, "llm_available", lambda: True)
    monkeypatch.setattr(dec, "analyze", lambda **kw: decision)
    # build_decision_brief returns (brief, price, facts_ctx); facts=None -> arm quality bar is graceful.
    monkeypatch.setattr(dec, "build_decision_brief", lambda *a, **k: ("BRIEF", price, facts))
    tech = _tech(price, macro=macro)
    prop = TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0, technical=tech)
    return ai_decide_trade(None, "X", AssetClass.FOREX, "1h", prop, tech, _fund(), NOW)


# ---- open / arm / stand-aside ----

def test_open_long_maps_to_proposal(monkeypatch):
    p = _run(monkeypatch, _dec([_scn("Cont", "up", 60, "open_long", stop=97.0, tp=106.0),
                                _scn("Pull", "down", 40, "none")]))
    assert p.direction == Direction.LONG
    assert p.entry == 100.0 and p.stop_loss == 97.0 and p.take_profit == 106.0
    assert p.review_decision == "ai" and p.confidence == 0.6
    assert p.ai_decision["kind"] == "open" and p.ai_decision["chosen"] == "Cont"
    assert len(p.ai_decision["scenarios"]) == 2 and "CHOSE 'Cont'" in p.rationale


def test_open_short_maps_to_proposal(monkeypatch):
    # a short needs a down macro, else the against-trend guardrail (correctly) blocks it
    p = _run(monkeypatch, _dec([_scn("Down", "down", 60, "open_short", stop=103.0, tp=94.0),
                                _scn("X", "up", 40, "none")]), macro="down")
    assert p.direction == Direction.SHORT and p.stop_loss == 103.0 and p.take_profit == 94.0


def test_stand_aside_when_no_trade_plan(monkeypatch):
    p = _run(monkeypatch, _dec([_scn("A", "up", 60, "none"), _scn("B", "down", 40, "none")]))
    assert p.direction == Direction.NO_TRADE and p.strategy == "stand_aside"
    assert p.ai_decision["kind"] == "stand_aside"


def test_arm_long_breakout_is_buy_stop(monkeypatch):
    p = _run(monkeypatch, _dec([_scn("Brk", "up", 60, "arm_long", trigger=102.0, stop=99.0, tp=108.0),
                                _scn("X", "down", 40, "none")]))
    assert p.direction == Direction.NO_TRADE and p.watch is True and p.conditional is not None
    assert p.conditional.order_type == "buy_stop"
    assert p.conditional.trigger_price == 102.0 and round(p.conditional.rr, 1) == 2.0


def test_arm_long_pullback_is_buy_limit(monkeypatch):
    p = _run(monkeypatch, _dec([_scn("Dip", "up", 60, "arm_long", trigger=98.0, stop=96.0, tp=104.0),
                                _scn("X", "down", 40, "none")]))
    assert p.conditional is not None and p.conditional.order_type == "buy_limit"


def test_arm_short_breakdown_is_sell_stop(monkeypatch):
    p = _run(monkeypatch, _dec([_scn("Brk", "down", 60, "arm_short", trigger=98.0, stop=101.0, tp=92.0),
                                _scn("X", "up", 40, "none")]))
    assert p.conditional is not None and p.conditional.order_type == "sell_stop"


# ---- untradeable scenarios are skipped, not forced ----

def test_thin_open_is_not_traded(monkeypatch):
    # ~0.33R open is not tradeable; with no other plan the decider stands aside (doesn't force it)
    p = _run(monkeypatch, _dec([_scn("Thin", "up", 60, "open_long", stop=97.0, tp=101.0),
                                _scn("X", "down", 40, "none")]))
    assert p.direction == Direction.NO_TRADE and p.ai_decision["kind"] == "stand_aside"


def test_thin_arm_is_not_armed(monkeypatch):
    # the USOIL bug: huge stop + tiny target (~0.33R) arm is not tradeable -> nothing armed
    p = _run(monkeypatch, _dec([_scn("Thin", "up", 60, "arm_long", trigger=100.0, stop=94.0, tp=102.0),
                                _scn("X", "down", 40, "none")]))
    assert p.direction == Direction.NO_TRADE and p.conditional is None


def test_hair_trigger_arm_is_not_armed(monkeypatch):
    # ATR 2.0; a 0.1-wide stop (< 0.25*ATR) is a hair-trigger -> not tradeable
    p = _run(monkeypatch, _dec([_scn("Hair", "up", 60, "arm_long", trigger=102.0, stop=101.9, tp=140.0),
                                _scn("X", "down", 40, "none")]))
    assert p.direction == Direction.NO_TRADE and p.conditional is None


def test_wrong_side_arm_is_not_armed(monkeypatch):
    # long arm but stop ABOVE the trigger -> invalid -> not tradeable
    p = _run(monkeypatch, _dec([_scn("Bad", "up", 60, "arm_long", trigger=100.0, stop=103.0, tp=108.0),
                                _scn("X", "down", 40, "none")]))
    assert p.direction == Direction.NO_TRADE and p.conditional is None


# ---- the two new behaviours ----

def test_picks_better_rr_over_higher_probability(monkeypatch):
    # the USDCHF case: a 55% open_long worth ~0.17R (thin) loses to a 45% arm_short worth 3R.
    p = _run(monkeypatch, _dec([_scn("Continuation", "up", 55, "open_long", stop=97.0, tp=100.5),
                                _scn("Pullback", "down", 45, "arm_short", trigger=99.0, stop=101.0, tp=93.0)]))
    assert p.conditional is not None and p.conditional.order_type == "sell_stop"
    assert p.ai_decision["chosen"] == "Pullback"


def test_falls_back_to_next_scenario_when_top_open_blocked(monkeypatch):
    # macro is DOWN: the higher-prob long (55%) is blocked by the against-trend guardrail, so the
    # decider falls back to the tradeable short (45%) instead of standing aside.
    p = _run(monkeypatch, _dec([_scn("Long", "up", 55, "open_long", stop=97.0, tp=106.0),
                                _scn("Short", "down", 45, "open_short", stop=103.0, tp=94.0)]), macro="down")
    assert p.direction == Direction.SHORT and p.ai_decision["chosen"] == "Short"


def test_llm_unavailable_keeps_deterministic(monkeypatch):
    monkeypatch.setattr(dec, "llm_available", lambda: False)
    det = TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                        direction=Direction.LONG, entry=100.0, stop_loss=97.0, take_profit=106.0,
                        confidence=0.72, technical=_tech(), rationale="deterministic")
    out = ai_decide_trade(None, "X", AssetClass.FOREX, "1h", det, _tech(), _fund(), NOW)
    assert out is det  # unchanged fallback


# ---- ③ arm quality bar (unit tests on _score_scenario with facts) ----

def _facts(level=102.0, tests=4, vol=1.0) -> dict:
    return {"resistance_ladder": [{"price": level, "tests": tests}], "support_ladder": [],
            "compression": {"vol_atr_ratio": vol}}


def test_arm_passes_at_strong_level_2r():
    sc = _scn(action="arm_long", trigger=102.0, stop=99.0, tp=108.0)  # 2R at a 4x-tested level
    ev = dec._score_scenario(sc, price=100.0, atr=2.0, facts=_facts(102.0, 4, 1.0))
    assert ev["tradeable"] and ev["rr"] == 2.0


def test_arm_rejected_at_weak_level():
    sc = _scn(action="arm_long", trigger=102.0, stop=99.0, tp=108.0)  # level only 1x tested
    ev = dec._score_scenario(sc, price=100.0, atr=2.0, facts=_facts(102.0, 1, 1.0))
    assert not ev["tradeable"] and "strong" in ev["reject"]


def test_arm_rejected_in_loose_range():
    sc = _scn(action="arm_long", trigger=102.0, stop=99.0, tp=108.0)  # strong level, but loose range
    ev = dec._score_scenario(sc, price=100.0, atr=2.0, facts=_facts(102.0, 4, 1.6))
    assert not ev["tradeable"] and "loose" in ev["reject"]


def test_arm_uses_1_5r_floor():
    # arm R:R floor lowered 2.0 -> 1.5: a 1.4R arm is rejected, a 1.5R arm at a strong level passes.
    strong = _facts(102.0, 4, 1.0)
    thin = _scn(action="arm_long", trigger=102.0, stop=99.0, tp=106.2)   # risk 3 reward 4.2 = 1.4R
    ev_thin = dec._score_scenario(thin, price=100.0, atr=2.0, facts=strong)
    assert not ev_thin["tradeable"] and "R:R" in ev_thin["reject"]
    ok = _scn(action="arm_long", trigger=102.0, stop=99.0, tp=106.5)     # risk 3 reward 4.5 = 1.5R
    ev_ok = dec._score_scenario(ok, price=100.0, atr=2.0, facts=strong)
    assert ev_ok["tradeable"] and round(ev_ok["rr"], 1) == 1.5


def test_open_uses_1_5_floor():
    # opens use the 1.5R floor and don't need a strong level / compression (unlike arms)
    sc = _scn(action="open_long", stop=98.0, tp=103.5)  # risk 2 reward 3.5 = 1.75R
    ev = dec._score_scenario(sc, price=100.0, atr=2.0, facts=_facts(102.0, 1, 1.6))
    assert ev["tradeable"] and ev["kind"] == "open"
