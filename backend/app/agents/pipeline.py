"""The analysis pipeline (Milestone 4, Mode A — propose only).

Flow: fetch multi-timeframe OHLCV -> Technical Analyst -> Fundamental (stub) ->
Orchestrator -> persist the TradeProposal + full reasoning -> run it through the
DETERMINISTIC Risk Manager -> persist the verdict and set status.

Nothing executes here. In Mode A an approved proposal lands in PENDING_APPROVAL awaiting the
user. (Auto-execution for Modes B/C is wired in Milestone 6.)
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.fundamental import run_fundamental
from app.agents.llm import llm_available
from app.agents.orchestrator import run_orchestrator
from app.agents.technical import run_technical
from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.data.ohlcv_cache import get_ohlcv_cached
from app.core.state import get_or_create_settings
from app.models.db import AgentRun, TradeProposalRecord
from app.models.enums import AssetClass, Direction, ProposalStatus, RiskDecisionType
from app.models.schemas import AnalyzeResponse, OHLCVSeries
from app.risk.service import assess

log = get_logger("agents.pipeline")

# Primary timeframe plus context timeframes; deduped, primary first.
_CONTEXT_TIMEFRAMES = ("1h", "1d")


def _timeframes_for(primary: str) -> list[str]:
    return list(dict.fromkeys([primary, *_CONTEXT_TIMEFRAMES]))


def preview_symbol(session: Session, symbol: str, asset_class: AssetClass, timeframe: str = "1h",
                   use_llm: bool = False, cache=None, read_llm: bool | None = None):
    """Analyse a symbol and run it through the Risk Manager WITHOUT persisting or executing —
    used to rank opportunities across the watchlist. Returns (proposal, risk_decision).

    ``cache`` (a ``risk.service.ScanCache``) lets a multi-symbol scan share one broker open-book +
    account fetch instead of one per symbol; leave it None for a single-symbol preview.

    ``read_llm`` decouples the technical/fundamental READ agents from the decision: the AI-led scan
    runs the reads DETERMINISTICALLY (read_llm=False) while the orchestrator still uses the LLM
    (use_llm=True), so it spends ONE AI call per pair (the decision) instead of three. Defaults to
    ``use_llm`` (reads follow the decision)."""
    now = datetime.now(timezone.utc)
    settings = get_or_create_settings(session)
    broker = get_broker_for(asset_class, settings.broker_map)
    series: list[OHLCVSeries] = []
    for tf in _timeframes_for(timeframe):
        try:
            series.append(get_ohlcv_cached(broker, symbol, tf, limit=200))
        except Exception as exc:  # noqa: BLE001
            log.warning("preview ohlcv failed", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
    review_on = settings.ai_review_enabled
    # AI-DECIDER (see analyze_symbol): 'AI decides' -> deterministic analyst + AI judge (open/arm/skip).
    ai_decides = (review_on and use_llm and llm_available() and not settings.st_band_mode)
    reads = (use_llm if read_llm is None else read_llm) and review_on and not ai_decides
    technical = run_technical(symbol, series, use_llm=reads)
    fundamental = run_fundamental(symbol, now=now, use_llm=(use_llm if read_llm is None else read_llm))
    proposal = run_orchestrator(symbol, asset_class, timeframe, technical, fundamental, now=now,
                                use_llm=use_llm, ai_review=review_on and not ai_decides,
                                trend_only=settings.trend_only_mode,
                                st_band=settings.st_band_mode)
    if ai_decides:
        from app.agents.ai_decider import ai_decide_trade
        proposal = ai_decide_trade(session, symbol, asset_class, timeframe, proposal,
                                   technical, fundamental, now)
    decision = assess(session, proposal, cache=cache)
    return proposal, decision


def _maybe_auto_execute(session: Session, record: TradeProposalRecord, broker) -> None:
    """Auto-execute under Modes B/C. Mode A leaves the proposal awaiting user approval."""
    from app.execution.executor import ExecutionBlocked, execute_proposal
    from app.models.enums import ExecutionMode

    settings = get_or_create_settings(session)
    mode = settings.execution_mode

    if mode == ExecutionMode.A_PROPOSE_APPROVE.value:
        return  # human-in-the-loop

    if mode == ExecutionMode.B_AUTO_PAPER.value and not broker.is_paper:
        # Mode B can never touch a live broker (safety: B is paper-only).
        log.warning("Mode B requested but broker is live; leaving for manual approval",
                    extra={"symbol": record.symbol})
        return

    try:
        execute_proposal(session, record)  # marks EXECUTED + opens a position on fill
        log.info("auto-executed proposal", extra={"symbol": record.symbol, "mode": mode})
    except ExecutionBlocked as exc:
        log.warning("auto-execute blocked; left for approval",
                    extra={"symbol": record.symbol, "reason": str(exc)})


def analyze_symbol(
    session: Session, symbol: str, asset_class: AssetClass, timeframe: str = "1h",
    use_llm: bool = True,
) -> AnalyzeResponse:
    """Run the full pipeline for one symbol. ``use_llm=False`` forces the deterministic
    agents (used by the auto-scanner to avoid burning LLM quota on every loop)."""
    now = datetime.now(timezone.utc)
    settings = get_or_create_settings(session)
    broker = get_broker_for(asset_class, settings.broker_map)

    # 1. Market data across timeframes.
    series: list[OHLCVSeries] = []
    for tf in _timeframes_for(timeframe):
        try:
            series.append(get_ohlcv_cached(broker, symbol, tf, limit=200))
        except Exception as exc:  # noqa: BLE001
            log.warning("ohlcv fetch failed", extra={"symbol": symbol, "tf": tf, "error": str(exc)})

    # 2. Agents.
    # AI review OFF (default): the AI is OUT of the trade decision — deterministic technical read, no
    # confirm/veto veto — but is KEPT for the fundamental bias (a soft nudge). ON restores full-AI
    # (LLM technical + confirm/veto). (Repeatability testing showed the reviewer isn't a stable filter;
    # a deterministic confidence>=70% gate matches it.)
    review_on = settings.ai_review_enabled
    # AI-DECIDER: when the "AI decides" toggle (ai_review_enabled) is ON, the deterministic engine is
    # the ANALYST (full analysis -> a decision brief) and the AI is the JUDGE (creates the scenarios,
    # picks the best tradeable one, decides open/arm/skip; levels anchored to the brief). The Risk
    # Manager still sizes/gates downstream. When the LLM is off/unavailable, we keep the deterministic
    # proposal. Mutually exclusive with st_band.
    ai_decides = (review_on and use_llm and llm_available() and not settings.st_band_mode)
    technical = run_technical(symbol, series, use_llm=use_llm and review_on and not ai_decides)
    fundamental = run_fundamental(symbol, now=now, use_llm=use_llm)  # AI kept for fundamentals
    proposal = run_orchestrator(symbol, asset_class, timeframe, technical, fundamental, now=now,
                                use_llm=use_llm, ai_review=review_on and not ai_decides,
                                trend_only=settings.trend_only_mode,
                                st_band=settings.st_band_mode)
    if ai_decides:
        from app.agents.ai_decider import ai_decide_trade
        det_proposal = proposal   # keep the deterministic call for the shadow scorecard
        proposal = ai_decide_trade(session, symbol, asset_class, timeframe, det_proposal,
                                   technical, fundamental, now)
        # SHADOW SCORECARD: log the AI decision alongside the deterministic one on this same setup,
        # so a grader can later score which was right (A/B proof + AI's own track record). Best-effort.
        from app.agents.shadow import record_shadow
        tf0 = (next((x for x in technical.timeframes if x.timeframe == timeframe),
                    technical.timeframes[0]) if technical.timeframes else None)
        ind0 = tf0.indicators if tf0 else {}
        record_shadow(session, symbol, asset_class.value, timeframe, now,
                      ind0.get("last_close"), ind0.get("atr14"), proposal, det_proposal)

    # 3. Persist the proposal + full reasoning bundle (audit trail).
    record = TradeProposalRecord(
        symbol=symbol,
        asset_class=asset_class.value,
        timeframe=timeframe,
        direction=proposal.direction.value,
        entry=proposal.entry,
        stop_loss=proposal.stop_loss,
        take_profit=proposal.take_profit,
        confidence=proposal.confidence,
        rationale=proposal.rationale,
        review_decision=proposal.review_decision,
        watch=proposal.watch,
        reasoning={
            "technical": technical.model_dump(mode="json"),
            "fundamental": fundamental.model_dump(mode="json"),
        },
        status=ProposalStatus.PENDING_RISK.value,
    )
    session.add(record)
    session.commit()

    # 4. Deterministic Risk Manager.
    decision = assess(session, proposal)
    record.risk_decision = decision.decision.value
    record.risk_reason = decision.reason
    record.approved_qty = decision.approved_qty
    record.risk_amount = decision.risk_amount

    if decision.approved:
        record.status = ProposalStatus.PENDING_APPROVAL.value
    else:
        record.status = ProposalStatus.RISK_VETOED.value
    session.add(record)
    session.commit()

    # 5. Execution mode: Mode A waits for the user; Modes B/C auto-execute risk-approved
    #    proposals (B paper-only, C live with the confirmation gate enforced in the executor).
    if decision.approved:
        _maybe_auto_execute(session, record, broker)

    session.add(AgentRun(
        agent="pipeline", symbol=symbol, event="analyze",
        detail={"direction": proposal.direction.value, "risk": decision.decision.value,
                "status": record.status},
    ))
    # Structured decision log (Task 12): the deciding gate + indicators + AI verdict for EVERY
    # evaluation (incl. no-trade), so "why didn't it fire?" is answerable after the fact.
    from app.agents.decision_log import record_decision
    record_decision(session, symbol, timeframe, proposal, decision)
    session.commit()

    log.info("analysis complete", extra={"symbol": symbol, "proposal_id": record.id,
                                         "direction": proposal.direction.value, "status": record.status})
    return AnalyzeResponse(proposal_id=record.id, status=record.status, proposal=proposal, risk=decision)
