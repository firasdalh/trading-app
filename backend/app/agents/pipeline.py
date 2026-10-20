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
from app.models.enums import AssetClass, ProposalStatus
from app.models.schemas import AnalyzeResponse, OHLCVSeries
from app.risk.service import assess

log = get_logger("agents.pipeline")

# Primary timeframe plus context timeframes; deduped, primary first. 4h is included so the engine can
# ladder through adjacent TFs (15m→1h→4h→1d) and respect the big-TF (4h/1d) support/resistance.
_CONTEXT_TIMEFRAMES = ("1h", "4h", "1d")


# Engine features that are BUILT but not PROVEN. They exist so the backtest harness can measure them
# (it calls the engine directly with its own `disable` set), but the live desk must never run them:
# the filter convention here is "on unless disabled", so without this a new feature would switch
# itself on in production the moment the code loaded - which is exactly how the order-block filters
# were briefly live-by-default on 2026-10-19 before a single result was in. A feature leaves this
# set only after it clears the bar in analysis/ and the user agrees.
_UNPROVEN_FILTERS = frozenset({"ob_wall", "ob_target"})


def _live_disable(settings) -> frozenset[str]:
    """The live `disable` set: the user's switched-off filters PLUS everything not yet proven."""
    return frozenset(settings.disabled_filters or ()) | _UNPROVEN_FILTERS


def _timeframes_for(primary: str) -> list[str]:
    return list(dict.fromkeys([primary, *_CONTEXT_TIMEFRAMES]))


_MIN_CONTEXT_BARS = 60


def _missing_context(series: list[OHLCVSeries], timeframe: str) -> str | None:
    """Which timeframe the decision would be made WITHOUT, if any.

    A failed fetch was dropped and an empty one (MT5 returns no rows for a few seconds after a
    terminal restart) read as trend 'sideways' - which PASSES the next-timeframe gate (only up/down
    block) and removes that timeframe's levels from the htf_level check. So a decision made on
    partial context was LOOSER than the full engine, not more cautious (full review 2026-10-20)."""
    have = {s.timeframe: len(s.candles or []) for s in series}
    short = [tf for tf in _timeframes_for(timeframe) if have.get(tf, 0) < _MIN_CONTEXT_BARS]
    if not short:
        return None
    return ", ".join(f"{tf} ({have.get(tf, 0)} bars)" for tf in short)


def _no_trade_for_missing_context(proposal, missing: str):
    """Keep the read-outs, drop the trade: the setup is not judged on partial data."""
    from app.models.enums import Direction

    return proposal.model_copy(update={
        "direction": Direction.NO_TRADE, "entry": None, "stop_loss": None, "take_profit": None,
        "confidence": 0.0, "watch": False, "conditional": None,
        "rationale": f"Context data missing ({missing}) - not deciding on a partial picture. "
                     f"Retry in a moment.",
    })


def preview_symbol(session: Session, symbol: str, asset_class: AssetClass, timeframe: str = "1h",
                   use_llm: bool = False, cache=None, read_llm: bool | None = None,
                   rsi_over: bool = False, rsi_confirm: bool = True, rsi_macd: bool = False,
                   rsi_div: bool = False, rsi_trend_filter: bool = True,
                   rsi_rej: bool = False, rsi_at_level: bool = False, rsi_pa: bool = False):
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
    # RSI-Over is a mechanical scan strategy -> keep the AI out (ai_decides False) like st_band.
    ai_decides = (review_on and use_llm and llm_available() and not settings.st_band_mode and not rsi_over)
    reads = (use_llm if read_llm is None else read_llm) and review_on and not ai_decides
    technical = run_technical(symbol, series, use_llm=reads)
    fundamental = run_fundamental(symbol, now=now, use_llm=(use_llm if read_llm is None else read_llm),
                                  asset_class=asset_class.value)
    _entry_series = next((x for x in series if x.timeframe == timeframe), None)
    proposal = run_orchestrator(symbol, asset_class, timeframe, technical, fundamental, now=now,
                                entry_candles=list(_entry_series.candles) if _entry_series else None,
                                use_llm=use_llm, ai_review=review_on and not ai_decides,
                                trend_only=settings.trend_only_mode,
                                st_band=settings.st_band_mode, rsi_over=rsi_over,
                                rsi_confirm=rsi_confirm, rsi_macd=rsi_macd, rsi_div=rsi_div,
                                rsi_trend_filter=rsi_trend_filter, rsi_rej=rsi_rej,
                                rsi_at_level=rsi_at_level, rsi_pa=rsi_pa,
                                disable=_live_disable(settings),
                                momentum_ai=(settings.ai_momentum_read and use_llm
                                             and llm_available() and not ai_decides),
                                regime_ai=(settings.ai_regime_read and use_llm
                                           and llm_available() and not ai_decides),
                                priceaction_ai=(settings.ai_priceaction_read and use_llm
                                                and llm_available() and not ai_decides),
                                # "Wait, don't chase": 0 = enter at market (default).
                                wait_entry=getattr(settings, "wait_entry_atr", 0.0) or 0.0)
    if ai_decides:
        from app.agents.ai_decider import ai_decide_trade
        proposal = ai_decide_trade(session, symbol, asset_class, timeframe, proposal,
                                   technical, fundamental, now)
    missing = _missing_context(series, timeframe)
    if missing:
        proposal = _no_trade_for_missing_context(proposal, missing)
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
    use_llm: bool = True, rsi_over: bool = False, rsi_confirm: bool = True, rsi_macd: bool = False,
    rsi_div: bool = False, rsi_trend_filter: bool = True, rsi_rej: bool = False,
    rsi_at_level: bool = False, rsi_pa: bool = False, source: str | None = None,
    cooldown_override: int | None = None, force_ai_decide: bool = False,
    min_rr: float | None = None, no_execute: bool = False, st_band: bool | None = None,
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
    # ``force_ai_decide`` (the per-pair AI auto-trader) turns on the decider for THIS call regardless
    # of the global toggle — that feature is AI-driven by definition (follows the scenario read).
    # ``st_band`` overrides the global SuperTrend toggle for THIS call (None = follow settings) — the
    # per-pair auto-trader uses it to run the mechanical SuperTrend strategy without flipping the whole
    # system into SuperTrend mode.
    st_band_eff = settings.st_band_mode if st_band is None else st_band
    ai_decides = ((review_on or force_ai_decide) and use_llm and llm_available()
                  and not st_band_eff and not rsi_over)
    technical = run_technical(symbol, series, use_llm=use_llm and (review_on or force_ai_decide) and not ai_decides)
    # asset_class reaches the calendar so a single-stock ticker resolves to the US calendar —
    # without it AAPLm matched no country and got no news blackout at all.
    fundamental = run_fundamental(symbol, now=now, use_llm=use_llm, asset_class=asset_class.value)
    _entry_series = next((x for x in series if x.timeframe == timeframe), None)
    proposal = run_orchestrator(symbol, asset_class, timeframe, technical, fundamental, now=now,
                                entry_candles=list(_entry_series.candles) if _entry_series else None,
                                use_llm=use_llm, ai_review=review_on and not ai_decides,
                                trend_only=settings.trend_only_mode,
                                st_band=st_band_eff, rsi_over=rsi_over,
                                rsi_confirm=rsi_confirm, rsi_macd=rsi_macd, rsi_div=rsi_div,
                                rsi_trend_filter=rsi_trend_filter, rsi_rej=rsi_rej,
                                rsi_at_level=rsi_at_level, rsi_pa=rsi_pa,
                                disable=_live_disable(settings),
                                momentum_ai=(settings.ai_momentum_read and use_llm
                                             and llm_available() and not ai_decides),
                                regime_ai=(settings.ai_regime_read and use_llm
                                           and llm_available() and not ai_decides),
                                priceaction_ai=(settings.ai_priceaction_read and use_llm
                                                and llm_available() and not ai_decides),
                                # "Wait, don't chase": 0 = enter at market (default).
                                wait_entry=getattr(settings, "wait_entry_atr", 0.0) or 0.0)
    if ai_decides:
        from app.agents.ai_decider import ai_decide_trade
        det_proposal = proposal   # keep the deterministic call for the shadow scorecard
        proposal = ai_decide_trade(session, symbol, asset_class, timeframe, det_proposal,
                                   technical, fundamental, now, min_rr=min_rr)
        # SHADOW SCORECARD: log the AI decision alongside the deterministic one on this same setup,
        # so a grader can later score which was right (A/B proof + AI's own track record). Best-effort.
        from app.agents.shadow import record_shadow
        tf0 = (next((x for x in technical.timeframes if x.timeframe == timeframe),
                    technical.timeframes[0]) if technical.timeframes else None)
        ind0 = tf0.indicators if tf0 else {}
        record_shadow(session, symbol, asset_class.value, timeframe, now,
                      ind0.get("last_close"), ind0.get("atr14"), proposal, det_proposal)

    # Fail closed on partial context (see _missing_context).
    missing = _missing_context(series, timeframe)
    if missing:
        proposal = _no_trade_for_missing_context(proposal, missing)

    # 2b. MEASURED ODDS: what this plan's stop/target distances have historically been worth on this
    # symbol (agents/odds.py). Read-out only — it never changes the decision, it just stops a plan whose
    # stop sits closer than its target from looking better than it is.
    if proposal.direction.value in ("long", "short"):
        from app.agents.odds import odds_for_proposal

        proposal.odds = odds_for_proposal(broker, proposal, timeframe)

    # 2b-ii. CHART STATE: what price is DOING to the last swing right now - pullback, rejection,
    # break, range or impulse - plus the two prices that settle it (a trigger and an invalidation).
    # Read-out only: it is shown on the chart and in the analysis, and gates nothing.
    try:
        from app.agents.chart_state import read_chart_state

        entry_series = next((x for x in series if x.timeframe == timeframe), None)
        if entry_series is not None and entry_series.candles:
            proposal.chart_state = read_chart_state(list(entry_series.candles)).as_dict()
    except Exception as exc:  # noqa: BLE001 - a read-out must never break the analysis
        log.warning("chart state unavailable", extra={"symbol": symbol, "error": str(exc)})

    # 2c. MACRO / NEWS / POSITIONING (app/macro/). Sits AFTER the deterministic engine has already
    # chosen a direction, and deliberately cannot change it: the only thing this layer may do is
    # delay ("wait") or annotate. A scheduled release is a volatility event of unknown sign, so a
    # macro layer that produced a side would be inventing information it does not have.
    macro_ctx = None
    try:
        from app.macro.service import macro_context

        macro_ctx = macro_context(session, symbol, asset_class.value, now=now)
        proposal.macro = macro_ctx
        if macro_ctx.get("blocked") and proposal.is_actionable:   # a property - calling it raised TypeError and the hold never happened
            # Keep the candidate VISIBLE (watch=True) instead of discarding it, so the desk can see
            # a good setup was found AND why it was held. Levels, direction and confidence survive.
            proposal.macro_wait = True
            proposal.watch = True
            proposal.rationale = (
                f"HELD - {macro_ctx.get('gate_reason', 'high-impact news nearby')}. "
                f"The setup itself still stands: {proposal.rationale}"
            )
            log.warning("macro gate held a setup",
                        extra={"symbol": symbol, "reason": macro_ctx.get("gate_reason"),
                               "score": (macro_ctx.get("news") or {}).get("score")})
    except Exception as exc:  # noqa: BLE001 - the macro layer must never break the trade path
        log.warning("macro context unavailable", extra={"symbol": symbol, "error": str(exc)})

    # 3. Persist the proposal + full reasoning bundle (audit trail). The SOURCE (who opened it) is the
    # caller's explicit hint (hybrid/rsi_over/manual) or, failing that, derived from the active mode so
    # the journal can compare win rate + P&L by mechanism.
    origin = source or ("rsi_over" if rsi_over else "supertrend" if settings.st_band_mode
                        else "ai" if ai_decides else "deterministic")
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
        source=origin,
        watch=proposal.watch,
        reasoning={
            "technical": technical.model_dump(mode="json"),
            "fundamental": fundamental.model_dump(mode="json"),
        },
        status=ProposalStatus.PENDING_RISK.value,
    )
    session.add(record)
    session.commit()

    # 3b. Freeze the macro state behind THIS decision. Without a per-decision snapshot there is no
    # way to ask later whether the news layer improved expectancy — only whether it felt like it
    # did, which is how the previous filters in this project got shipped unmeasured.
    if macro_ctx is not None:
        from app.macro.service import record_trade_macro

        record_trade_macro(session, symbol=symbol, context=macro_ctx, proposal_id=record.id,
                           direction=proposal.direction.value,
                           technical_confidence=proposal.confidence)

    # 4. Deterministic Risk Manager.
    decision = assess(session, proposal, override_cooldown_minutes=cooldown_override)
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
    #    ``no_execute`` runs the FULL analysis (persisted + shadow-logged, identical to the "Run
    #    analysis" button) but suppresses the auto-open — used when a caller wants the analysis's
    #    conditional to ARM from (Hybrid's arm step), not to fire a market trade as a side effect.
    if decision.approved and not no_execute:
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
    try:
        market_open = broker.market_open(symbol)
    except Exception:  # noqa: BLE001 - a status probe must never fail the analysis
        market_open = None
    return AnalyzeResponse(proposal_id=record.id, status=record.status, proposal=proposal,
                           risk=decision, analyzed_at=now, market_open=market_open)
