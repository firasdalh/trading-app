"""Shared pytest fixtures.

``db_session`` gives each test a fresh SQLite DB, forces deterministic agents (no Anthropic
key), and resets the broker registry + provider singletons so tests don't leak state.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _force_deterministic_agents(monkeypatch):
    """Keep agent tests hermetic: never call a real LLM regardless of the developer's saved
    provider config. The provider-dispatch tests exercise app.agents.llm directly and are
    unaffected (they don't go through these agent modules)."""
    for mod_name in ("technical", "orchestrator", "fundamental", "reflection"):
        try:
            mod = importlib.import_module(f"app.agents.{mod_name}")
            monkeypatch.setattr(mod, "llm_available", lambda: False, raising=False)
        except Exception:  # noqa: BLE001
            pass
    # Clear the per-symbol LLM-read caches so a cached read never leaks between tests.
    for mod_name, attr in (("fundamental", "_FUND_CACHE"), ("scenarios", "_CACHE"),
                           ("momentum_read", "_CACHE")):
        try:
            getattr(importlib.import_module(f"app.agents.{mod_name}"), attr).clear()
        except Exception:  # noqa: BLE001
            pass
    # Offline data providers — never hit the network (e.g. the live economic calendar).
    from app.data import providers
    providers.set_providers(
        news=providers.StubNewsProvider(),
        calendar=providers.StubCalendarProvider(),
        sentiment=providers.StubSentimentProvider(),
    )


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path/'test.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("BROKER_ENV", "paper")

    from app.core import config
    config.get_settings.cache_clear()

    from app.core import database as db_mod
    importlib.reload(db_mod)

    from app.brokers import registry
    registry.reset_registry()

    # Reset swappable data providers to defaults.
    from app.data import providers
    providers.set_providers(
        news=providers.StubNewsProvider(),
        calendar=providers.StubCalendarProvider(),
        sentiment=providers.StubSentimentProvider(),
    )

    db_mod.init_db()
    with db_mod.session_scope() as s:
        yield s
