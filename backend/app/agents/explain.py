"""Turn a raw agent/AI-review rationale into a clean, structured, optionally-Arabic explanation.

The orchestrator's review is one dense paragraph (grade + primary reason + a ';'-joined concerns
list). This re-formats it into Decision / main reason / pros / cons / conclusion so a trader reads
it at a glance, and can render it in Arabic. It is FAITHFUL: it only restructures/translates the
text it's given — it never adds new analysis, levels, or reasons. Falls back to None (caller shows
the raw text) when no LLM is configured.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.llm import analyze
from app.core.logging import get_logger

log = get_logger("agents.explain")


class ExplainedReview(BaseModel):
    decision: str = Field(description="Short decision headline, e.g. 'NO TRADE' / 'ENTER SHORT'.")
    is_trade: bool = Field(description="True if the verdict is to TAKE the trade, False to stand aside.")
    grade: str | None = Field(None, description="Setup grade A/B/C if stated in the text, else null.")
    main_reason: str = Field(description="The single most important driver of the decision (1-2 sentences).")
    pros: list[str] = Field(default_factory=list, description="Concrete points in the trade's favour.")
    cons: list[str] = Field(default_factory=list, description="Concrete points against / the risks.")
    conclusion: str = Field(description="A short final-verdict paragraph tying it together.")


_RULES = (
    "You are a senior trading-desk analyst. You are given the RAW TEXT of an automated trade "
    "review/decision. Reformat it into a clear, faithful, well-structured explanation a trader can "
    "read at a glance.\n"
    "Rules:\n"
    "1. Be FAITHFUL: do NOT invent facts, price levels, indicators, or reasons that are not in the "
    "text. Only restructure, clarify, and lightly summarise what is already there.\n"
    "2. decision: the final call as a short headline (e.g. 'NO TRADE', 'ENTER SHORT').\n"
    "3. is_trade: true only if the verdict is to take the trade.\n"
    "4. grade: the A/B/C grade if the text states one, otherwise null.\n"
    "5. main_reason: the single most important driver, 1-2 sentences.\n"
    "6. pros: the concrete points in the trade's favour (trend, momentum, ADX, etc.).\n"
    "7. cons: the concrete points against it / the risks (blocking structure, oversold, liquidity, "
    "event risk, etc.).\n"
    "8. conclusion: a short final-verdict paragraph.\n"
)

_SYSTEM_EN = _RULES + "Respond in English."
_SYSTEM_AR = (
    _RULES
    + "Respond ENTIRELY in Arabic (العربية), using clear, professional trading language. Keep the "
    "grade letter (A/B/C) and all numbers/price levels as-is (digits)."
)


def explain_review(text: str, lang: str = "en") -> ExplainedReview | None:
    """Structure (and, for ``lang='ar'``, translate) a rationale. None if the LLM is unavailable."""
    text = (text or "").strip()
    if not text:
        return None
    system = _SYSTEM_AR if lang == "ar" else _SYSTEM_EN
    user = f"RAW TRADE REVIEW TEXT:\n{text}"
    out = analyze(system=system, user=user, schema=ExplainedReview, max_tokens=2000)
    if out is None:
        log.info("explain_review: LLM unavailable/failed", extra={"lang": lang})
    return out
