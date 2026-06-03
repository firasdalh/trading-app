"""Tests for LLM provider selection + dispatch (Claude/Gemini), without real API calls."""
from __future__ import annotations

from pydantic import BaseModel

from app.agents import llm
from app.agents.llm_config import resolve_llm_config


class _Out(BaseModel):
    value: str


def test_no_key_means_unavailable(db_session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from app.core import config
    config.get_settings.cache_clear()
    assert llm.llm_available() is False
    assert llm.analyze(system="s", user="u", schema=_Out) is None


def test_resolve_prefers_db_config(db_session):
    from app.models.db import LlmConfig
    db_session.add(LlmConfig(id=1, provider="gemini", model="gemini-2.5-flash", api_key="k-123"))
    db_session.commit()
    cfg = resolve_llm_config()
    assert cfg.provider == "gemini" and cfg.model == "gemini-2.5-flash" and cfg.available


def test_analyze_dispatches_to_gemini(db_session, monkeypatch):
    from app.models.db import LlmConfig
    db_session.add(LlmConfig(id=1, provider="gemini", model="gemini-2.5-flash", api_key="k-123"))
    db_session.commit()

    called = {}

    def fake_gemini(model, api_key, system, user, schema, max_tokens):
        called["provider"] = "gemini"
        called["model"] = model
        return schema(value="ok")

    monkeypatch.setattr(llm, "_gemini_analyze", fake_gemini)
    out = llm.analyze(system="s", user="u", schema=_Out)
    assert out is not None and out.value == "ok"
    assert called == {"provider": "gemini", "model": "gemini-2.5-flash"}


def test_analyze_dispatches_to_anthropic(db_session, monkeypatch):
    from app.models.db import LlmConfig
    db_session.add(LlmConfig(id=1, provider="anthropic", model="claude-opus-4-8", api_key="k-xyz"))
    db_session.commit()

    captured = {}

    def fake_anthropic(model, api_key, system, user, schema, max_tokens):
        captured["model"] = model
        return schema(value="claude")

    monkeypatch.setattr(llm, "_anthropic_analyze", fake_anthropic)
    out = llm.analyze(system="s", user="u", schema=_Out)
    assert out is not None and out.value == "claude" and captured["model"] == "claude-opus-4-8"
