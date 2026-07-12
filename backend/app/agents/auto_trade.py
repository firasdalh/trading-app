"""Per-pair AI auto-trader.

For each pair the user toggles ON, a scheduled tick (every ``interval_seconds``, default 15 min)
runs the AI analysis and, when the pair is FLAT and past the short per-pair ``cooldown_minutes``
(default 5), auto-opens a setup whose confidence clears ``min_confidence`` (default 60%) — following
the scenario's own levels. The monitor rides it to TP/SL; the next tick re-enters. It "learns" only
in the honest sense that the AI's decision brief already carries this pair's recent win/loss record
(calibration + the shadow scorecard), so its probabilities adjust from what actually happened.

PAPER-ONLY (a live broker needs the typed live-confirmation, which auto-open can't supply, so it's
skipped there). EVERY risk gate still applies via the deterministic Risk Manager + the executor
(3% cap, exposure, correlation, daily-loss breaker, anti-stacking, kill-switch).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pipeline import analyze_symbol
from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.db import AgentRun, AutoTradeConfig, Position, TradeProposalRecord
from app.models.enums import AssetClass, PositionStatus, ProposalStatus

log = get_logger("agents.auto_trade")


def get_or_create_auto_trade_config(session: Session) -> AutoTradeConfig:
    cfg = session.get(AutoTradeConfig, 1)
    if cfg is None:
        cfg = AutoTradeConfig(id=1, enabled=True, interval_seconds=900, min_confidence=0.60,
                              min_rr=1.2, min_profit_usd=20.0, cooldown_minutes=5, pairs=[])
        session.add(cfg)
        session.commit()
    return cfg


def _norm(sym: str | None) -> str:
    return (sym or "").upper()


def set_pair(session: Session, symbol: str, asset_class: str, on: bool,
             timeframe: str = "1h") -> AutoTradeConfig:
    """Toggle a pair's auto-trade on/off (keeps the rest of the list)."""
    cfg = get_or_create_auto_trade_config(session)
    pairs = [p for p in (cfg.pairs or []) if _norm(p.get("symbol")) != _norm(symbol)]
    if on:
        pairs.append({"symbol": symbol, "asset_class": asset_class, "timeframe": timeframe})
    cfg.pairs = pairs
    session.commit()
    log.warning("auto-trade pair toggled", extra={"symbol": symbol, "on": on})
    return cfg


def _has_open_position(session: Session, symbol: str) -> bool:
    rows = session.scalars(select(Position).where(Position.status == PositionStatus.OPEN.value)).all()
    return any(_norm(p.symbol) == _norm(symbol) for p in rows)


def _closed_within(session: Session, symbol: str, minutes: int) -> bool:
    """True if a position on this pair CLOSED within the last ``minutes`` — still cooling down."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = session.scalars(select(Position).where(Position.status == PositionStatus.CLOSED.value)).all()
    for p in rows:
        if _norm(p.symbol) != _norm(symbol) or p.closed_at is None:
            continue
        ca = p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=timezone.utc)
        if ca >= cutoff:
            return True
    return False


def _open_market(session: Session, prop, dec):
    """Persist an approved auto-trade proposal + open it now at market. Returns the record or None."""
    from app.execution.executor import ExecutionBlocked, execute_proposal

    record = TradeProposalRecord(
        symbol=prop.symbol, asset_class=prop.asset_class.value, timeframe=prop.timeframe,
        direction=prop.direction.value, entry=prop.entry, stop_loss=prop.stop_loss,
        take_profit=prop.take_profit, confidence=prop.confidence, rationale=prop.rationale,
        source="auto_trade", risk_decision=dec.decision.value, risk_reason=dec.reason,
        approved_qty=dec.approved_qty, risk_amount=dec.risk_amount,
        status=ProposalStatus.APPROVED.value,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    try:
        execute_proposal(session, record)  # kill-switch + drift guard + every gate
    except ExecutionBlocked:
        record.status = ProposalStatus.PENDING_APPROVAL.value
        session.commit()
        return None
    session.refresh(record)
    return record if record.status == ProposalStatus.EXECUTED.value else None


def _dip_or_open(session: Session, symbol: str, ac: str, tf: str, technical, cfg: AutoTradeConfig):
    """Follow the read: go WITH the higher-TF trend toward the next S/R. OPEN NOW at market if there's
    already >= min_profit_usd (and >= min_rr) of room from the current price to that target; otherwise
    ARM a limit at the dip/rally level for a better entry. Skips entirely if even the level entry can't
    clear the $ floor. Trend-gated + risk-gated (Risk Manager sizes it, so $ profit = risk_amount * R:R)."""
    from app.agents.conditional import arm_conditional
    from app.agents.orchestrator import _higher_trend
    from app.models.enums import Direction
    from app.models.schemas import TradeProposal
    from app.risk.service import assess

    if technical is None or not technical.timeframes:
        return None
    tf0 = next((x for x in technical.timeframes if x.timeframe == tf), technical.timeframes[0])
    ind = tf0.indicators or {}
    price, atr = ind.get("last_close"), ind.get("atr14")
    if not price or not atr or atr <= 0:
        return None
    htf_trend, htf_name = _higher_trend(technical, tf)
    sup = tf0.support_levels[0] if tf0.support_levels else None
    res = tf0.resistance_levels[0] if tf0.resistance_levels else None
    buf = 0.5 * atr

    if htf_trend == "up" and sup and res and sup < price < res:
        direction, stop, target, level = Direction.LONG, round(sup - buf, 6), res, sup
        order_type, kind = "buy_limit", "buy the dip at support"
    elif htf_trend == "down" and sup and res and sup < price < res:
        direction, stop, target, level = Direction.SHORT, round(res + buf, 6), sup, res
        order_type, kind = "sell_limit", "sell the rally at resistance"
    else:
        return None

    def _plan(entry: float):
        """Build + size a plan at ``entry``; return (proposal, decision, rr, $potential) if it clears
        the R:R floor AND the $ minimum (enough room to the target), else None."""
        risk = (entry - stop) if direction == Direction.LONG else (stop - entry)
        reward = (target - entry) if direction == Direction.LONG else (entry - target)
        if risk <= 0 or reward <= 0:
            return None
        rr = reward / risk
        if rr < cfg.min_rr:
            return None
        prop = TradeProposal(symbol=symbol, asset_class=AssetClass(ac), timeframe=tf, direction=direction,
                             entry=round(entry, 6), stop_loss=stop, take_profit=round(target, 6),
                             confidence=0.6, rationale=f"Auto-trade: {kind} (~{rr:.1f}R).", strategy="auto_trade")
        dec = assess(session, prop, override_cooldown_minutes=cfg.cooldown_minutes)
        if not dec.approved or not dec.risk_amount:
            return None
        potential = dec.risk_amount * rr           # $ gained at the target (linear in the sized qty)
        if potential < cfg.min_profit_usd:
            return None
        return prop, dec, rr, potential

    # 1) OPEN NOW at market if there's already enough room from the current price (why wait for a dip).
    now = _plan(price)
    if now is not None:
        prop, dec, rr, potential = now
        if _open_market(session, prop, dec) is not None:
            log.warning("auto-trade opened (room now)", extra={"symbol": symbol, "dir": direction.value})
            return {"symbol": symbol, "opened": direction.value, "note": f"~${potential:.0f} to {round(target, 6)}"}
    # 2) else ARM a limit at the dip/rally level for a better entry.
    at_level = _plan(level)
    if at_level is not None:
        prop, dec, rr, potential = at_level
        armed = arm_conditional(
            session, symbol=symbol, asset_class=ac, timeframe=tf, direction=direction.value,
            order_type=order_type, trigger_price=round(level, 6), stop_loss=stop, take_profit=round(target, 6),
            confidence=0.6, rr=round(rr, 2),
            rationale=f"Auto-trade: {kind} — with the {htf_name} {htf_trend}trend (~${potential:.0f} to the range edge).",
            source="auto_trade", auto_execute=True, require_close_confirm=False,
        )
        if armed is not None:
            log.warning("auto-trade dip-armed", extra={"symbol": symbol, "dir": direction.value, "trigger": level})
            return {"symbol": symbol, "armed": f"{direction.value} @ {round(level, 6)} ({kind}, ~${potential:.0f})"}
    return None


def _auto_trade_symbol(session: Session, cfg: AutoTradeConfig, pair: dict) -> dict:
    """Analyse one pair and auto-open if it qualifies. Returns a short note dict."""
    symbol = pair.get("symbol")
    ac = pair.get("asset_class", "forex")
    tf = pair.get("timeframe", "1h")

    if _has_open_position(session, symbol):
        return {"symbol": symbol, "skipped": "position open — riding it"}
    if _closed_within(session, symbol, cfg.cooldown_minutes):
        return {"symbol": symbol, "skipped": f"cooldown (<{cfg.cooldown_minutes}m since last close)"}

    settings = get_or_create_settings(session)
    broker = get_broker_for(AssetClass(ac), settings.broker_map)
    if not getattr(broker, "is_paper", False):
        return {"symbol": symbol, "skipped": "live broker — auto-open needs typed confirmation (paper-only)"}

    try:
        # force_ai_decide -> the AI decider decides on the pair's OWN scenario levels (follows the
        # scenario read), regardless of the global "AI decides" toggle. The short 5-min cooldown is
        # applied to the risk check via the override (not the global one).
        res = analyze_symbol(session, symbol, AssetClass(ac), tf, use_llm=True, source="auto_trade",
                             cooldown_override=cfg.cooldown_minutes, force_ai_decide=True,
                             min_rr=cfg.min_rr)
    except Exception as exc:  # noqa: BLE001 - one bad pair shouldn't stop the loop
        log.warning("auto-trade analyze failed", extra={"symbol": symbol, "error": str(exc)})
        return {"symbol": symbol, "error": str(exc)}

    record = session.get(TradeProposalRecord, res.proposal_id)
    if record and record.status == ProposalStatus.EXECUTED.value:  # Modes B/C already opened it
        return {"symbol": symbol, "opened": record.direction, "confidence": record.confidence}

    prop = res.proposal

    # (a) An immediate market OPEN — direction set, risk-approved, confident enough.
    if prop.direction.value in ("long", "short"):
        if not res.risk.approved:
            return {"symbol": symbol, "skipped": f"risk vetoed ({res.risk.reason})"}
        if prop.confidence < cfg.min_confidence:
            return {"symbol": symbol, "skipped": f"below {cfg.min_confidence:.0%} ({prop.confidence:.0%})"}
        from app.execution.executor import ExecutionBlocked, execute_proposal

        record.status = ProposalStatus.APPROVED.value
        session.commit()
        try:
            execute_proposal(session, record)  # every gate + kill-switch enforced here
        except ExecutionBlocked as exc:
            record.status = ProposalStatus.PENDING_APPROVAL.value
            session.commit()
            return {"symbol": symbol, "blocked": str(exc)}
        session.refresh(record)
        if record.status != ProposalStatus.EXECUTED.value:
            return {"symbol": symbol, "skipped": f"order not filled ({record.status})"}
        log.warning("auto-trade opened", extra={"symbol": symbol, "direction": record.direction})
        return {"symbol": symbol, "opened": record.direction, "confidence": record.confidence}

    # (b) An ARM — the AI wants to WAIT for the break/retest at a named level (buy the dip at support /
    # sell the rally at resistance). Place the pending order; the conditional watcher fires it on the
    # trigger, so the auto-trader follows the scenario's levels instead of forcing a market entry.
    cond = prop.conditional
    if cond is not None and cond.confidence >= cfg.min_confidence:
        from app.agents.conditional import arm_conditional

        direction = "long" if str(cond.order_type).startswith("buy") else "short"
        armed = arm_conditional(
            session, symbol=symbol, asset_class=ac, timeframe=tf, direction=direction,
            order_type=cond.order_type, trigger_price=cond.trigger_price, stop_loss=cond.stop_loss,
            take_profit=cond.take_profit, confidence=cond.confidence, rr=cond.rr,
            rationale=cond.reason, source="auto_trade", auto_execute=True,
        )
        if armed is not None:
            log.warning("auto-trade armed", extra={"symbol": symbol, "dir": direction,
                                                   "trigger": cond.trigger_price})
            return {"symbol": symbol, "armed": f"{direction} @ {cond.trigger_price}"}
        return {"symbol": symbol, "skipped": "arm already active / blocked"}

    # (c) The decider stood aside — follow the read like the user does manually: go with the higher-TF
    # trend toward the next S/R — OPEN NOW if there's enough room ($ floor), else arm the dip/rally.
    dip = _dip_or_open(session, symbol, ac, tf, prop.technical, cfg)
    if dip is not None:
        return dip

    return {"symbol": symbol, "skipped": f"no setup ({prop.direction.value}, {prop.confidence:.0%})"}


def run_auto_trade(session: Session) -> dict:
    """One pass over every enabled pair. Records to the audit log + stamps last_run."""
    cfg = get_or_create_auto_trade_config(session)
    results = [_auto_trade_symbol(session, cfg, p) for p in (cfg.pairs or [])]
    opened = [r for r in results if r.get("opened")]
    armed = [r for r in results if r.get("armed")]
    cfg.last_run_at = datetime.now(timezone.utc)
    acted = ([f"{r['symbol']} {r['opened']}" for r in opened]
             + [f"{r['symbol']} armed {r['armed']}" for r in armed])
    cfg.last_result = "; ".join(acted) if acted else f"no action ({len(results)} pairs checked)"
    session.add(AgentRun(agent="auto_trade", event="tick",
                         detail={"pairs": len(results), "opened": len(opened),
                                 "armed": len(armed), "results": results}))
    session.commit()
    return {"ran": True, "opened": len(opened), "results": results}


def auto_trade_tick(session: Session) -> dict:
    """Scheduler entrypoint: respect the master flag + interval, then run one pass."""
    cfg = get_or_create_auto_trade_config(session)
    if not cfg.enabled or not (cfg.pairs or []):
        return {"ran": False, "reason": "disabled or no pairs"}
    now = datetime.now(timezone.utc)
    if cfg.last_run_at is not None:
        last = cfg.last_run_at if cfg.last_run_at.tzinfo else cfg.last_run_at.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cfg.interval_seconds:
            return {"ran": False, "reason": "interval not elapsed"}
    return run_auto_trade(session)
