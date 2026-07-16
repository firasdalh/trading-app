"""AI scenario read: the successful AI read is cached per symbol so repeats cost no tokens."""
from __future__ import annotations

import app.agents.scenarios as scen
from app.agents.scenarios import _Scenario, _ScenarioRead
from app.models.enums import AssetClass


def _ctx():
    return {"symbol": "AAA", "price": 100.0, "scorecard": [], "nearest_resistance": None,
            "nearest_support": None, "structure": "range", "choch": False, "channel": None,
            "price_action": "flat", "rsi": 50, "volume_trend": "flat", "atr": 2.0,
            "overall_bias": "neutral", "invalidation": None}


def _read():
    return _ScenarioRead(
        headline="lean up", primary="Up", why_primary="structure",
        scenarios=[_Scenario(label="Up", direction="up", probability=60, path="x", reasoning="y"),
                   _Scenario(label="Down", direction="down", probability=40, path="x", reasoning="y")],
        invalidation="99.0")


def test_ai_scenarios_cached(db_session, monkeypatch):
    monkeypatch.setattr(scen, "llm_available", lambda: True)
    monkeypatch.setattr(scen, "build_context", lambda *a, **k: _ctx())
    calls = {"n": 0}
    monkeypatch.setattr(scen, "analyze", lambda **k: (calls.__setitem__("n", calls["n"] + 1) or _read()))
    r1 = scen.ai_scenarios(db_session, "AAA", AssetClass.CRYPTO)
    r2 = scen.ai_scenarios(db_session, "AAA", AssetClass.CRYPTO)
    assert calls["n"] == 1                       # second read served from cache (no token spend)
    assert r1["source"] == "ai" and r2["primary"] == r1["primary"]


def test_ai_scenarios_deterministic_fallback_not_cached(db_session, monkeypatch):
    # No LLM -> cheap deterministic fallback, and it's NOT cached (so it upgrades to the AI read later).
    monkeypatch.setattr(scen, "llm_available", lambda: False)
    monkeypatch.setattr(scen, "build_context", lambda *a, **k: _ctx())
    out = scen.ai_scenarios(db_session, "BBB", AssetClass.CRYPTO)
    assert out is not None and out["source"] == "deterministic"
    assert ("BBB", "crypto") not in scen._CACHE
