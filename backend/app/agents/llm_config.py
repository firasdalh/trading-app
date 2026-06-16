"""Resolve the active LLM provider/model/key: UI-set (DB) first, then .env fallback."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.db import LlmConfig

log = get_logger("agents.llm_config")

_DEFAULT_MODEL = {"anthropic": "claude-opus-4-8", "gemini": "gemini-2.5-flash",
                  "openai": "gpt-5-mini"}


@dataclass
class LlmCfg:
    provider: str        # "anthropic" | "gemini" | "openai"
    model: str
    api_key: str

    @property
    def available(self) -> bool:
        return bool(self.api_key)


def resolve_llm_config() -> LlmCfg:
    # Lazy import so a reloaded DB engine (tests) is picked up correctly.
    from app.core.database import session_scope

    # 1. UI-set config in the DB (provider + key present).
    try:
        with session_scope() as session:
            row = session.get(LlmConfig, 1)
            if row and row.provider and row.api_key:
                provider = row.provider
                model = row.model or _DEFAULT_MODEL.get(provider, "")
                return LlmCfg(provider=provider, model=model, api_key=row.api_key)
    except Exception as exc:  # noqa: BLE001 - DB not ready / table missing
        log.warning("llm config DB lookup failed; using env", extra={"error": str(exc)})

    # 2. Fall back to .env.
    cfg = get_settings()
    provider = (cfg.llm_provider or "anthropic").lower()
    if provider == "gemini":
        return LlmCfg("gemini", cfg.gemini_model or _DEFAULT_MODEL["gemini"], cfg.gemini_api_key)
    if provider == "openai":
        return LlmCfg("openai", cfg.openai_model or _DEFAULT_MODEL["openai"], cfg.openai_api_key)
    return LlmCfg("anthropic", cfg.anthropic_model or _DEFAULT_MODEL["anthropic"], cfg.anthropic_api_key)
