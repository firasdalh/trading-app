"""AI DECIDER: the deterministic engine is the analyst, the AI is the judge.

Covers the translation of the AI's structured decision into a TradeProposal — open now, ARM a pending
order (correct order-type from trigger vs price), stand aside, and the guardrail block on a bad setup.
The decision brief itself is patched out (it needs a broker/DB); we test the decision->proposal logic."""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.ai_decider as dec
from app.agents.ai_decider import _AiScenario, _DecisionLLM, ai_decide_trade
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import FundamentalRead, TechnicalRead, TimeframeRead

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


def _patch(monkeypatch, decision, price=100.0):
    monkeypatch.setattr(dec, "llm_available", lambda: True)
    monkeypatch.setattr(dec, "analyze", lambda **kw: decision)
    # skip the DB/broker-backed brief; just feed a fixed price.
    monkeypatch.setattr(dec, "build_decision_brief", lambda *a, **k: ("BRIEF", price))


def _run(monkeypatch, decision, price=100.0, macro="up"):
    _patch(monkeypatch, decision, price)
    from app.models.schemas import TradeProposal
    tech = _tech(price, macro=macro)
    prop = TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0, technical=tech)
    return ai_decide_trade(None, "X", AssetClass.FOREX, "1h", prop, tech, _fund(), NOW)


def _scenarios() -> list[_AiScenario]:
    # the AI CREATES these from the facts; the decider just carries them into the rationale
    return [
        _AiScenario(label="Continuation up", direction="up", probability=60,
                    path="hold 98 -> push to 106", reasoning="uptrend + support holds"),
        _AiScenario(label="Pullback first", direction="down", probability=40,
                    path="dip to 96 then resume", reasoning="momentum cooling"),
    ]


def _dec(**kw) -> _DecisionLLM:
    base = dict(action="open_long", scenarios=_scenarios(), chosen="Continuation up",
                why_chosen="structure + momentum favour continuation", conviction=0.7,
                trigger_price=None, stop_loss=97.0, take_profit=106.0,
                rationale="with-trend at value", key_risks=["news"])
    base.update(kw)
    return _DecisionLLM(**base)


def test_open_long_maps_to_proposal(monkeypatch):
    p = _run(monkeypatch, _dec())
    assert p.direction == Direction.LONG
    assert p.entry == 100.0 and p.stop_loss == 97.0 and p.take_profit == 106.0
    assert p.review_decision == "ai" and p.confidence == 0.7
    # the AI-created scenarios + the chosen one are carried into the rationale
    assert "Scenarios the AI built" in p.rationale
    assert "Continuation up" in p.rationale and "CHOSE" in p.rationale


def test_open_short_maps_to_proposal(monkeypatch):
    # short needs a down macro, else the against-trend guardrail (correctly) blocks it
    p = _run(monkeypatch, _dec(action="open_short", stop_loss=103.0, take_profit=94.0), macro="down")
    assert p.direction == Direction.SHORT
    assert p.stop_loss == 103.0 and p.take_profit == 94.0


def test_stand_aside(monkeypatch):
    p = _run(monkeypatch, _dec(action="stand_aside", stop_loss=None, take_profit=None))
    assert p.direction == Direction.NO_TRADE and p.strategy == "stand_aside"


def test_arm_long_breakout_is_buy_stop(monkeypatch):
    # trigger ABOVE price -> buy_stop (breakout continuation)
    p = _run(monkeypatch, _dec(action="arm_long", trigger_price=102.0, stop_loss=99.0, take_profit=108.0))
    assert p.direction == Direction.NO_TRADE and p.watch is True
    assert p.conditional is not None
    assert p.conditional.order_type == "buy_stop"
    assert p.conditional.trigger_price == 102.0 and round(p.conditional.rr, 1) == 2.0


def test_arm_long_pullback_is_buy_limit(monkeypatch):
    # trigger BELOW price -> buy_limit (pullback to value)
    p = _run(monkeypatch, _dec(action="arm_long", trigger_price=98.0, stop_loss=96.0, take_profit=104.0))
    assert p.conditional is not None and p.conditional.order_type == "buy_limit"


def test_arm_short_breakdown_is_sell_stop(monkeypatch):
    # trigger BELOW price -> sell_stop (breakdown)
    p = _run(monkeypatch, _dec(action="arm_short", trigger_price=98.0, stop_loss=101.0, take_profit=92.0))
    assert p.conditional is not None and p.conditional.order_type == "sell_stop"


def test_open_bad_rr_blocked_by_guardrail(monkeypatch):
    # risk 3, reward 1 -> 0.33R, below the min-RR floor -> guardrail converts to NO_TRADE
    p = _run(monkeypatch, _dec(take_profit=101.0))
    assert p.direction == Direction.NO_TRADE
    assert "blocked" in p.rationale.lower()


def test_arm_wrong_side_stands_aside(monkeypatch):
    # long arm but stop ABOVE trigger -> invalid -> stand aside
    p = _run(monkeypatch, _dec(action="arm_long", trigger_price=100.0, stop_loss=103.0, take_profit=108.0))
    assert p.direction == Direction.NO_TRADE and p.conditional is None


def test_arm_thin_rr_rejected(monkeypatch):
    # the USOIL bug: a huge stop + tiny target (~0.3R) must be rejected, not armed (below the 1.5R floor)
    p = _run(monkeypatch, _dec(action="arm_long", trigger_price=100.0, stop_loss=94.0, take_profit=102.0))
    assert p.direction == Direction.NO_TRADE and p.conditional is None
    assert "1.5R floor" in p.rationale


def test_arm_hair_trigger_stop_rejected(monkeypatch):
    # ATR is 2.0; a 0.1-wide stop (< 0.25*ATR) is a hair-trigger -> rejected, no conditional armed
    p = _run(monkeypatch, _dec(action="arm_long", trigger_price=102.0, stop_loss=101.9, take_profit=140.0))
    assert p.direction == Direction.NO_TRADE and p.conditional is None
    assert "too tight" in p.rationale


def test_llm_unavailable_keeps_deterministic(monkeypatch):
    monkeypatch.setattr(dec, "llm_available", lambda: False)
    from app.models.schemas import TradeProposal
    det = TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                        direction=Direction.LONG, entry=100.0, stop_loss=97.0, take_profit=106.0,
                        confidence=0.72, technical=_tech(), rationale="deterministic")
    out = ai_decide_trade(None, "X", AssetClass.FOREX, "1h", det, _tech(), _fund(), NOW)
    assert out is det  # unchanged fallback
