"""AI SCENARIO read (Step 4 of Run-analysis) — INFO ONLY.

The LLM reasons out EXACTLY TWO forward scenarios for a symbol, ranks them, assigns a probability
score to each, and explains each in the same plain style as the 🗺️ map read. It is *anchored* to the
deterministic map (real multi-TF S/R, HH/HL structure, trend, RSI, volume trend, ATR, and the engine's
setup) so the model reasons over FACTS and cites REAL levels instead of inventing numbers.

Boundaries (deliberate):
- It does NOT gate or change the engine's decision. The AI reviewer was removed from the decision path
  for non-repeatability (see analysis/ai_repeatability.md); this is a read for the user's Mode-A call.
- The probabilities are the model's JUDGEMENT and will vary run-to-run — read them as a lean, not a
  measurement. The deterministic skeleton (build_context) is passed in to keep the variance small.
- If no LLM is configured, it degrades to the deterministic scenarios from build_context (source="deterministic").
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.context import build_context
from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.enums import AssetClass

log = get_logger("agents.scenarios")

# The AI scenario read is a slow-moving "lean", so cache it per symbol and share it across ALL callers
# (the Run-analysis card, the auto-trader, the position advisor) — skips the ~1500-token call on repeats.
_CACHE: dict[tuple, tuple[float, dict]] = {}
_TTL_SEC = 15 * 60


class _Scenario(BaseModel):
    label: str = Field(description="short name, e.g. 'Pullback first, then up'")
    direction: str = Field(description="up | down | sideways")
    probability: int = Field(description="0-100 likelihood; the two scenarios should sum to ~100")
    path: str = Field(description="the trigger -> target path in one line, using the ACTUAL levels given")
    reasoning: str = Field(description="2-3 plain sentences: WHY this scenario, citing structure/levels/momentum")


class _ScenarioRead(BaseModel):
    headline: str = Field(description="one-line overall lean")
    primary: str = Field(description="the label of the more-likely scenario")
    why_primary: str = Field(description="2-3 sentences on WHY the primary scenario is more likely than "
                                         "the other — the head-to-head: which evidence tips the balance")
    scenarios: list[_Scenario] = Field(description="EXACTLY two scenarios, most-likely first")
    invalidation: str = Field(description="the price/level that would flip the read")


_SYSTEM = (
    "You are a disciplined price-action analyst. You are given a FACTUAL market snapshot (already "
    "computed: multi-timeframe support/resistance, HH/HL market structure, trend, RSI, volume trend, "
    "ATR, and the engine's proposed setup). Reason out EXACTLY TWO forward scenarios for the next few "
    "sessions. Rank them most-likely first, assign each a probability (the two should sum to ~100), and "
    "explain each in 2-3 plain sentences that CITE the given levels and structure. Rules: use only the "
    "ACTUAL price levels provided — never invent numbers; usually one scenario is a continuation and the "
    "other a pullback/rejection; favour the scenario that structure + momentum support; no hype, no "
    "financial-advice language. Return the two scenarios, the primary label, a one-line headline, the "
    "single invalidation level that would flip the read, AND a 'why_primary' that argues head-to-head "
    "why the chosen scenario is the more likely one — which specific evidence (structure, level, "
    "momentum, volume) tips the balance over the alternative."
)


def _scenario_target(ctx: dict, scen: list[dict]) -> float | None:
    """The continuation TARGET of the PRIMARY scenario — the next level in its direction (i.e. where to
    set a TP). Reuses build_context's break candidate target (nearest level BEYOND the wall it breaks),
    falling back to the 2nd rung of the ladder. None when there's no clear directional target."""
    primary_dir = (scen[0].get("direction") if scen else "") or ""
    if primary_dir == "down":
        cand = ctx.get("breakdown") or {}
        if cand.get("target"):
            return cand["target"]
        lad = ctx.get("support_ladder") or []
        return lad[1]["price"] if len(lad) >= 2 else None
    if primary_dir == "up":
        cand = ctx.get("breakout_up") or {}
        if cand.get("target"):
            return cand["target"]
        lad = ctx.get("resistance_ladder") or []
        return lad[1]["price"] if len(lad) >= 2 else None
    return None


def _invalidation_price(ctx: dict, scen: list[dict]) -> float | None:
    """The numeric level that INVALIDATES the primary scenario (a stop reference): a close back above
    the resistance for a down scenario, or back below the support for an up scenario."""
    primary_dir = (scen[0].get("direction") if scen else "") or ""
    if primary_dir == "down":
        return (ctx.get("nearest_resistance") or {}).get("price")
    if primary_dir == "up":
        return (ctx.get("nearest_support") or {}).get("price")
    return None


def _lvl(d: dict | None) -> str:
    """Render a nearest-level dict ('{price, tf, kind}') as 'TF price', or '(none)'."""
    if not d:
        return "(none)"
    return f"{str(d.get('tf', '')).upper()} {d.get('price')}"


def _prob_from(text: str) -> int | None:
    """Pull an integer percent out of a deterministic scenario's 'prob' string ('~60%' -> 60)."""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    return int(digits) if digits else None


def _normalize(scen: list[_Scenario]) -> list[dict]:
    """Coerce to exactly two ranked scenarios whose probabilities sum to 100."""
    scen = list(scen)[:2]
    if not scen:
        return []
    probs = [max(1, min(99, int(s.probability or 50))) for s in scen]
    if len(probs) == 1:
        probs = [probs[0], 100 - probs[0]]
        scen = scen + [scen[0]]  # shouldn't happen; guard
    total = sum(probs) or 100
    probs = [round(p * 100 / total) for p in probs]
    probs[-1] = 100 - sum(probs[:-1])  # make it sum to exactly 100 after rounding
    out = []
    for s, p in zip(scen, probs):
        out.append({"label": s.label, "direction": s.direction, "prob": p,
                    "path": s.path, "reasoning": s.reasoning})
    out.sort(key=lambda x: x["prob"], reverse=True)
    return out


def _deterministic_fallback(ctx: dict) -> dict:
    """Map build_context's rule-based scenarios into the same shape (used when no LLM is configured)."""
    out = []
    for s in ctx.get("scenarios", [])[:2]:
        out.append({"label": s.get("label", ""), "direction": "", "prob": _prob_from(s.get("prob", "")) or 50,
                    "path": s.get("text", ""), "reasoning": s.get("text", "")})
    # if the two don't sum to 100 (e.g. 60/40 already fine; 65/35 fine), leave as-is — they're rule-based
    out.sort(key=lambda x: x["prob"], reverse=True)
    why = ""
    if len(out) >= 2:
        why = (f"The rule-based read favours '{out[0]['label']}' ({out[0]['prob']}%) over "
               f"'{out[1]['label']}' ({out[1]['prob']}%) because the map is {ctx.get('overall_bias', '')}: "
               "market structure sets the primary direction and momentum/level position decides whether "
               "a pullback or an immediate continuation is more likely from here.")
    return {
        "symbol": ctx["symbol"], "price": ctx["price"],
        "timeframe": ctx.get("timeframe", "1h"), "source": "deterministic",
        "computed_at": datetime.now(timezone.utc).isoformat(), "cached": False,
        "headline": ctx.get("overall_bias", ""), "primary": out[0]["label"] if out else "", "why_primary": why,
        "scenarios": out, "invalidation": ctx.get("invalidation"),
        "nearest_support": (ctx.get("nearest_support") or {}).get("price"),
        "nearest_resistance": (ctx.get("nearest_resistance") or {}).get("price"),
        "target": _scenario_target(ctx, out),
        "invalidation_price": _invalidation_price(ctx, out),
        "overall_bias": ctx.get("overall_bias"), "scorecard": ctx.get("scorecard", []),
        "note": "No AI model configured — showing the deterministic map scenarios.",
    }


def ai_scenarios(session: Session, symbol: str, asset_class: AssetClass,
                 timeframe: str = "1h", force: bool = False) -> dict | None:
    """Two AI-reasoned, ranked, scored forward scenarios anchored to the deterministic map read.

    Returns None only when there's no market data at all. When the LLM is unavailable OR the call
    fails, returns the deterministic fallback (source='deterministic') so the endpoint always answers.

    The successful AI read is cached per (symbol, asset_class) for ``_TTL_SEC`` and shared across every
    caller, so repeat reads within the window cost no tokens. The cheap deterministic fallback isn't
    cached (so it's retried / upgraded to the AI read as soon as the LLM is available again).
    """
    # The timeframe is part of the cache key. Without it a 1h read cached a minute ago would be
    # handed back for a 15m request — the panel would look updated while showing another chart's
    # analysis, which is the hardest kind of wrong to notice.
    key = (symbol.upper(), asset_class.value, timeframe)
    hit = _CACHE.get(key)
    if hit is not None and not force:
        age = time.monotonic() - hit[0]
        if age < _TTL_SEC:
            # Served without touching the model. Flagged as cached and stamped with its real age so
            # the UI can say WHEN it was reasoned — a 14-minute-old read that renders as "just now"
            # is a read you'd trust more than it deserves.
            return {**hit[1], "cached": True, "age_sec": int(age),
                    "expires_in_sec": int(_TTL_SEC - age)}

    ctx = build_context(session, symbol, asset_class, timeframe)
    if ctx is None:
        return None
    if not llm_available():
        return _deterministic_fallback(ctx)

    # Build a compact, factual snapshot for the model — all numbers come from the deterministic read.
    # We feed FACTS only (no pre-made scenarios); the AI creates its own scenarios from them.
    sc = "; ".join(f"{s['factor']} {s['signal']} ({s['note']})" for s in ctx.get("scorecard", []))
    tf_read = ctx.get("timeframe", timeframe)
    # State the real timeframe. A model told "1h" while being fed 15m numbers reasons on the wrong
    # horizon — it will talk about moves playing out over days when the chart spans hours.
    horizon = {"5m": "the next few hours", "15m": "the rest of the session",
               "1h": "the next 1-3 days", "4h": "the next 1-2 weeks",
               "1d": "the next few weeks"}.get(tf_read, "the near term")
    user = (
        f"SYMBOL: {symbol}  PRICE: {ctx['price']}  (timeframe {tf_read}, with higher-timeframe context)\n"
        f"SCENARIO HORIZON: reason over {horizon} — the scale this timeframe actually resolves on.\n"
        f"NEAREST RESISTANCE: {_lvl(ctx.get('nearest_resistance'))}\n"
        f"NEAREST SUPPORT: {_lvl(ctx.get('nearest_support'))}\n"
        f"STRUCTURE: {ctx.get('structure')}  (change-of-character: {ctx.get('choch')})\n"
        f"CHANNEL: {ctx.get('channel') or 'n/a'}\n"
        f"MOMENTUM/PRICE-ACTION: {ctx.get('price_action')}\n"
        f"RSI: {ctx.get('rsi') or 'n/a'}   VOLUME: {ctx.get('volume_trend')}   ATR: {ctx.get('atr') or 'n/a'}\n"
        f"SCORECARD: {sc}\n"
        f"OVERALL BIAS (engine's mechanical read, reference only): {ctx.get('overall_bias')}\n"
        f"INVALIDATION LEVEL (flips the structural read): {ctx.get('invalidation') or 'n/a'}\n\n"
        "These are FACTS/numbers, not scenarios. CREATE your own two forward scenarios from them "
        "(anchored to the SAME real levels — do not invent prices), then rank and score them."
    )
    read = analyze(system=_SYSTEM, user=user, schema=_ScenarioRead, max_tokens=1500)
    if read is None or not read.scenarios:
        log.info("scenario LLM unavailable/failed; deterministic fallback", extra={"symbol": symbol})
        return _deterministic_fallback(ctx)

    scen = _normalize(read.scenarios)
    primary = scen[0]["label"] if scen else read.primary
    log.info("ai scenarios", extra={"symbol": symbol, "primary": primary,
                                     "probs": [s["prob"] for s in scen]})
    result = {
        "symbol": symbol, "price": ctx["price"], "timeframe": tf_read, "source": "ai",
        # When the model actually reasoned. Every later cache hit carries this same stamp.
        "computed_at": datetime.now(timezone.utc).isoformat(), "cached": False,
        "ttl_sec": _TTL_SEC,
        "headline": read.headline, "primary": primary, "why_primary": read.why_primary,
        "scenarios": scen, "invalidation": read.invalidation or ctx.get("invalidation"),
        # The exact S/R levels the AI reasoned over (it was fed these) — so the UI can plot them.
        "nearest_support": (ctx.get("nearest_support") or {}).get("price"),
        "nearest_resistance": (ctx.get("nearest_resistance") or {}).get("price"),
        "target": _scenario_target(ctx, scen),   # the primary scenario's continuation target (TP reference)
        "invalidation_price": _invalidation_price(ctx, scen),   # the level that flips it (stop reference)
        "overall_bias": ctx.get("overall_bias"), "scorecard": ctx.get("scorecard", []),
        "note": "AI judgement — probabilities are a lean, not a measurement, and will vary run-to-run.",
    }
    _CACHE[key] = (time.monotonic(), result)
    return result
