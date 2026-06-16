"""LLM wrapper shared by the agents — supports Claude (Anthropic) and Gemini (Google).

The active provider/model/key is resolved from the UI (DB) or .env (see ``llm_config``).
Both paths constrain the model to our Pydantic schema via structured outputs:
- Anthropic: ``messages.parse(output_format=Schema)`` + adaptive thinking.
- Gemini: ``generate_content(config=response_schema=Schema, response_mime_type=json)``.

DEFENSIVE: any failure (no key, malformed output, API/network error) returns ``None`` so
callers fall back to the deterministic analyzer. The agent layer never crashes the app.
SDKs are imported lazily so the app boots without either package installed.
"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from app.agents.llm_config import resolve_llm_config
from app.core.logging import get_logger

log = get_logger("agents.llm")

T = TypeVar("T", bound=BaseModel)

# Cache the provider clients (keyed by "provider:api_key" so re-config rebuilds them).
_clients: dict[str, object] = {}


def llm_available() -> bool:
    """True if the active provider has an API key configured."""
    return resolve_llm_config().available


def active_provider() -> str:
    return resolve_llm_config().provider


def _anthropic_client(api_key: str):
    key = f"anthropic:{api_key}"
    if key not in _clients:
        import anthropic
        _clients[key] = anthropic.Anthropic(api_key=api_key)
    return _clients[key]


def _gemini_client(api_key: str):
    key = f"gemini:{api_key}"
    if key not in _clients:
        from google import genai
        _clients[key] = genai.Client(api_key=api_key)
    return _clients[key]


def _openai_client(api_key: str):
    key = f"openai:{api_key}"
    if key not in _clients:
        from openai import OpenAI
        _clients[key] = OpenAI(api_key=api_key)
    return _clients[key]


def _anthropic_analyze(model: str, api_key: str, system: str, user: str,
                       schema: type[T], max_tokens: int) -> T | None:
    client = _anthropic_client(api_key)
    resp = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    return resp.parsed_output


def _gemini_analyze(model: str, api_key: str, system: str, user: str,
                    schema: type[T], max_tokens: int) -> T | None:
    from google.genai import types

    client = _gemini_client(api_key)
    cfg = dict(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=max_tokens,
    )
    # Disable "thinking" for structured extraction: it consumes the output-token budget and
    # can truncate the JSON mid-string. Not all models accept it, so degrade gracefully.
    try:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:  # noqa: BLE001
        pass
    resp = client.models.generate_content(
        model=model, contents=user, config=types.GenerateContentConfig(**cfg),
    )
    # The SDK parses the JSON into the Pydantic schema for us.
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    # Fallback: validate the raw JSON text ourselves.
    text = getattr(resp, "text", None)
    if text:
        return schema.model_validate_json(text)
    return None


def _openai_analyze(model: str, api_key: str, system: str, user: str,
                    schema: type[T], max_tokens: int) -> T | None:
    """OpenAI structured output via the chat-completions parse helper. GPT-5 models are reasoning
    models, so the token budget covers thinking too — give generous headroom and keep reasoning
    low for fast, cheap structured extraction (mirrors disabling Gemini's 'thinking')."""
    client = _openai_client(api_key)
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format=schema,
        max_completion_tokens=max(max_tokens, 8000),
    )
    try:  # GPT-5 accepts reasoning_effort; older models reject it — degrade gracefully.
        resp = client.beta.chat.completions.parse(reasoning_effort="low", **kwargs)
    except TypeError:
        resp = client.beta.chat.completions.parse(**kwargs)
    return resp.choices[0].message.parsed


class _Ping(BaseModel):
    ok: bool


def probe() -> None:
    """Run a tiny structured call against the active provider. Raises on failure (used by
    the 'test connection' button so the real error surfaces)."""
    cfg = resolve_llm_config()
    if not cfg.available:
        raise RuntimeError("no API key configured for the selected provider")
    system = "You are a connectivity check. Respond with ok=true."
    user = "Return ok=true."
    if cfg.provider == "gemini":
        result = _gemini_analyze(cfg.model, cfg.api_key, system, user, _Ping, 2000)
    elif cfg.provider == "openai":
        result = _openai_analyze(cfg.model, cfg.api_key, system, user, _Ping, 2000)
    else:
        result = _anthropic_analyze(cfg.model, cfg.api_key, system, user, _Ping, 2000)
    if result is None:
        raise RuntimeError("provider returned no parseable output")


def analyze(*, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T | None:
    """Call the active LLM and parse the response into ``schema``. Returns None on any failure."""
    cfg = resolve_llm_config()
    if not cfg.available:
        return None
    try:
        if cfg.provider == "gemini":
            parsed = _gemini_analyze(cfg.model, cfg.api_key, system, user, schema, max_tokens)
        elif cfg.provider == "openai":
            parsed = _openai_analyze(cfg.model, cfg.api_key, system, user, schema, max_tokens)
        else:
            parsed = _anthropic_analyze(cfg.model, cfg.api_key, system, user, schema, max_tokens)
        if parsed is None:
            log.warning("llm returned no parsed output; falling back",
                        extra={"provider": cfg.provider, "schema": schema.__name__})
        return parsed
    except Exception as exc:  # noqa: BLE001 - degrade to deterministic fallback
        log.warning("llm call failed; falling back to deterministic analyzer",
                    extra={"provider": cfg.provider, "schema": schema.__name__, "error": str(exc)})
        return None
