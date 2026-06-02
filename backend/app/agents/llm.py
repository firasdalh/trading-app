"""Thin Anthropic wrapper shared by the LLM agents.

Design:
- Uses the official `anthropic` SDK with structured outputs (`messages.parse` +
  `output_format=<PydanticModel>`), so the model is constrained to our schema and the SDK
  validates/strips unsupported JSON-schema constraints for us.
- Adaptive thinking on (Opus 4.8). Model name is a config value.
- The system prompt is sent as a cache_control block (cheap on repeat calls).
- DEFENSIVE: any failure (no key, malformed output, API/network error) returns ``None`` so
  callers fall back to the deterministic analyzer. The agent layer never crashes the app.
"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("agents.llm")

T = TypeVar("T", bound=BaseModel)

_client = None


def llm_available() -> bool:
    """True if an Anthropic API key is configured."""
    return bool(get_settings().anthropic_api_key)


def _get_client():
    global _client
    if _client is None:
        import anthropic  # lazy import so the app boots without the package configured

        _client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    return _client


def analyze(*, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T | None:
    """Call Claude and parse the response into ``schema``. Returns None on any failure."""
    if not llm_available():
        return None
    cfg = get_settings()
    try:
        client = _get_client()
        resp = client.messages.parse(
            model=cfg.anthropic_model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        parsed = resp.parsed_output
        if parsed is None:
            log.warning("llm returned no parsed output; falling back", extra={"schema": schema.__name__})
        return parsed
    except Exception as exc:  # noqa: BLE001 - degrade to deterministic fallback
        log.warning("llm call failed; falling back to deterministic analyzer",
                    extra={"schema": schema.__name__, "error": str(exc)})
        return None
