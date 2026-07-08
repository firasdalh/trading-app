"""Task 12 — structured decision logging.

Records EVERY funnel evaluation (including 'no trade') to the queryable ``AgentRun`` table with
``agent="decision"``, so you can answer "why didn't it fire on this setup?" after the fact without
re-reading code: the deciding gate, the indicator values at that moment, the confidence, and the AI
reviewer's confirm/veto verdict — all in one JSON row.

Uses the existing AgentRun table (no migration); query via GET /api/decisions or SQL on
agent='decision'.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.db import AgentRun
from app.models.schemas import RiskDecision, TradeProposal

log = get_logger("agents.decision_log")


def classify_gate(proposal: TradeProposal, risk: RiskDecision | None) -> str:
    """A short, stable tag for the gate that DECIDED this evaluation — the thing you'd filter on."""
    d = proposal.direction.value
    if d in ("long", "short"):
        if proposal.review_decision == "veto":
            return "ai_veto"
        if risk is not None and not risk.approved:
            return "risk_veto"
        return "approved"
    # Not actionable -> classify from the rationale (which names the funnel gate) + state.
    r = (proposal.rationale or "").lower()
    if proposal.conditional is not None or proposal.watch and "arm" in r:
        return "armed_wait"
    table = [
        ("event", "news_blackout"), ("trend-only", "regime_not_trending"),
        ("no confluence", "mtf_conflict"), ("structure conflict", "structure_conflict"),
        ("volatil", "volatility"), ("whipsaw", "volatility"), ("divergence", "divergence"),
        ("no clear trend", "no_trend"), ("ema20 band", "st_band_inside"),
        ("supertrend", "st_band"), ("r:r", "thin_rr"), ("only ~", "thin_rr"),
        ("ranging", "ranging_fade"), ("pullback", "pullback_wait"),
    ]
    for needle, tag in table:
        if needle in r:
            return tag
    return "no_trade_other"


def _entry_indicators(proposal: TradeProposal, timeframe: str) -> dict:
    tech = proposal.technical
    if not tech or not tech.timeframes:
        return {}
    prim = next((x for x in tech.timeframes if x.timeframe == timeframe), tech.timeframes[0])
    ind = prim.indicators or {}
    keys = ("adx", "plus_di", "minus_di", "rsi14", "macd_hist", "atr14",
            "ema20", "ema50", "ema200", "supertrend_dir", "structure")
    return {k: ind.get(k) for k in keys if ind.get(k) is not None}


def record_decision(session: Session, symbol: str, timeframe: str, proposal: TradeProposal,
                    risk: RiskDecision | None = None) -> None:
    """Persist one evaluation. Best-effort — never raises into the pipeline."""
    try:
        session.add(AgentRun(
            agent="decision", symbol=symbol, event=proposal.direction.value,
            detail={
                "timeframe": timeframe,
                "direction": proposal.direction.value,
                "actionable": proposal.direction.value in ("long", "short"),
                "gate": classify_gate(proposal, risk),
                "regime": proposal.regime,
                "strategy": proposal.strategy,
                "confidence": proposal.confidence,
                "review_decision": proposal.review_decision,
                "risk_decision": (risk.decision.value if risk else None),
                "risk_reason": (risk.reason if risk else None),
                "rationale": proposal.rationale,
                "indicators": _entry_indicators(proposal, timeframe),
                "conditional": (proposal.conditional.order_type if proposal.conditional else None),
            },
        ))
        # caller commits (analyze_symbol commits right after).
    except Exception as exc:  # noqa: BLE001 - logging must never break a decision
        log.warning("decision log failed", extra={"symbol": symbol, "error": str(exc)})
