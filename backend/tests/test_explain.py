"""Tests for the explain/translate endpoint (structured + Arabic AI-review rationale)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.agents.explain as explain_mod
from app.agents.explain import ExplainedReview
from app.api.proposal_routes import ExplainRequest, explain


def test_explain_returns_structured(monkeypatch):
    fake = ExplainedReview(
        decision="NO TRADE", is_trade=False, grade="C",
        main_reason="A support cluster sits between entry and target.",
        pros=["With the trend", "Strong ADX"],
        cons=["Support blocks the path to target", "RSI oversold"],
        conclusion="Right direction, wrong timing — stand aside.",
    )
    monkeypatch.setattr(explain_mod, "explain_review", lambda text, lang: fake)
    res = explain(ExplainRequest(text="SHORT setup VETOED by AI review: GRADE C ...", lang="en"))
    assert res.is_trade is False and res.grade == "C" and res.lang == "en"
    assert res.decision == "NO TRADE" and res.pros and res.cons


def test_explain_passes_arabic_lang_through(monkeypatch):
    captured: dict[str, str] = {}

    def fake(text, lang):
        captured["lang"] = lang
        return ExplainedReview(decision="لا تدخل", is_trade=False, main_reason="سبب", conclusion="خلاصة")

    monkeypatch.setattr(explain_mod, "explain_review", fake)
    res = explain(ExplainRequest(text="x", lang="ar"))
    assert captured["lang"] == "ar" and res.lang == "ar"


def test_explain_unavailable_returns_503(monkeypatch):
    monkeypatch.setattr(explain_mod, "explain_review", lambda text, lang: None)
    with pytest.raises(HTTPException) as ei:
        explain(ExplainRequest(text="x", lang="en"))
    assert ei.value.status_code == 503
