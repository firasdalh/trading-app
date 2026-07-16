"""Per-pair AI auto-trader (quick-win, market-only — it NEVER arms).

For each pair the user toggles ON, a scheduled tick (every ``interval_seconds``, default 15 min)
OPENS a market trade following the AI's recommendation — never a pending "armed" order. Order:
  1. ROOM gate — if the book is already full (open positions >= ``max_open_positions``, default 3),
     do nothing (checked before any LLM work).
  2. When the pair is FLAT and past the short per-pair ``cooldown_minutes`` (default 5):
     (a) if the AI DECIDER returns a decisive directional trade (risk-approved, conf >= min), open it;
     (b) else follow the AI's PRIMARY forward SCENARIO — open its immediate next move NOW at market
         toward the named level (support for a down-move, resistance for an up-move), a quick win.
The monitor rides it to TP/SL; the next tick re-enters. It "learns" only in the honest sense that the
AI's brief already carries this pair's recent win/loss record (calibration + the shadow scorecard).

PAPER-ONLY (a live broker needs the typed live-confirmation, which auto-open can't supply, so it's
skipped there). EVERY setup clears ``min_rr`` + the $ floor and is sized/gated by the deterministic
Risk Manager + the executor (3% cap, exposure, correlation, daily-loss breaker, anti-stacking,
kill-switch).
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

# The auto-trader takes the AI's next move at MARKET (quick-win) — it no longer fades a level with a
# pending order, so the old fade gates are gone. Two guards remain on the scenario open:
_MOM_THRU_ATR = 0.10       # skip if momentum > this * ATR is driving AGAINST the intended move
_SCEN_STOP_ATR = 1.0       # fallback stop distance (in ATR) when there's no opposite level to anchor to


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


def _open_positions(session: Session) -> list[Position]:
    return list(session.scalars(select(Position).where(Position.status == PositionStatus.OPEN.value)).all())


def _has_open_position(session: Session, symbol: str) -> bool:
    return any(_norm(p.symbol) == _norm(symbol) for p in _open_positions(session))


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


def _nearest_levels(technical, price: float) -> tuple[float | None, float | None]:
    """Nearest support BELOW and nearest resistance ABOVE the current price, across all timeframes on
    the read (so the scenario's cited HTF level — e.g. a 1D support — is available as a target)."""
    sups = [s for x in technical.timeframes for s in (x.support_levels or []) if s < price]
    ress = [r for x in technical.timeframes for r in (x.resistance_levels or []) if r > price]
    return (max(sups) if sups else None, min(ress) if ress else None)


def _open_scenario_move(session: Session, symbol: str, ac: str, tf: str, technical, cfg: AutoTradeConfig):
    """Follow the AI's PRIMARY forward scenario: OPEN a market trade NOW in the scenario's immediate
    direction toward its named target (nearest support for a down-move, nearest resistance for an
    up-move). It never arms — the auto-trader takes the next move as a quick win. The stop anchors to
    the opposite level (the structure the move is leaving), falling back to ~1xATR. Risk Manager sizes
    it (so $ profit = risk_amount * R:R); skips if momentum is driving AGAINST the move, if the primary
    scenario has no clear up/down lean above ``min_confidence``, or if the R:R / $ floors don't clear."""
    from app.agents.scenarios import ai_scenarios
    from app.models.enums import Direction
    from app.models.schemas import TradeProposal
    from app.risk.service import assess

    if technical is None or not technical.timeframes:
        return None
    tf0 = next((x for x in technical.timeframes if x.timeframe == tf), technical.timeframes[0])
    ind = tf0.indicators or {}
    price, atr, macd_hist = ind.get("last_close"), ind.get("atr14"), ind.get("macd_hist")
    if not price or not atr or atr <= 0:
        return None

    scen = ai_scenarios(session, symbol, AssetClass(ac))
    if not scen or not scen.get("scenarios"):
        return None
    primary = scen["scenarios"][0]
    sdir = str(primary.get("direction") or "").lower()
    conf = (primary.get("prob") or 0) / 100.0
    if sdir not in ("up", "down") or conf < cfg.min_confidence:
        return None

    sup, res = _nearest_levels(technical, price)
    buf = 0.5 * atr
    if sdir == "down":
        if not sup:
            return None                                        # no support below to move toward
        direction, target = Direction.SHORT, sup
        stop = round((res + buf) if res else (price + _SCEN_STOP_ATR * atr), 6)
    else:  # up
        if not res:
            return None                                        # no resistance above to move toward
        direction, target = Direction.LONG, res
        stop = round((sup - buf) if sup else (price - _SCEN_STOP_ATR * atr), 6)

    # Don't take the move if momentum is still driving AGAINST it (it may not reach the target / reverse).
    mom = _MOM_THRU_ATR * atr
    if macd_hist is not None and ((direction == Direction.LONG and macd_hist < -mom)
                                  or (direction == Direction.SHORT and macd_hist > mom)):
        return None

    risk = (price - stop) if direction == Direction.LONG else (stop - price)
    reward = (target - price) if direction == Direction.LONG else (price - target)
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < cfg.min_rr:
        return None
    prop = TradeProposal(symbol=symbol, asset_class=AssetClass(ac), timeframe=tf, direction=direction,
                         entry=round(price, 6), stop_loss=stop, take_profit=round(target, 6),
                         confidence=round(conf, 2),
                         rationale=f"Auto-trade: AI scenario '{primary.get('label', '')}' "
                                   f"({primary.get('prob')}%) — {sdir} to {round(target, 6)} (~{rr:.1f}R).",
                         strategy="auto_trade")
    dec = assess(session, prop, override_cooldown_minutes=cfg.cooldown_minutes)
    if not dec.approved or not dec.risk_amount:
        return None
    potential = dec.risk_amount * rr               # $ gained at the target (linear in the sized qty)
    if potential < cfg.min_profit_usd:
        return None
    if _open_market(session, prop, dec) is not None:
        log.warning("auto-trade opened (scenario)",
                    extra={"symbol": symbol, "dir": direction.value, "target": round(target, 6)})
        return {"symbol": symbol, "opened": direction.value,
                "note": f"AI {sdir} scenario → {round(target, 6)} (~${potential:.0f})"}
    return None


def _auto_trade_symbol(session: Session, cfg: AutoTradeConfig, pair: dict) -> dict:
    """Analyse one pair and auto-open if it qualifies. Returns a short note dict."""
    symbol = pair.get("symbol")
    ac = pair.get("asset_class", "forex")
    tf = pair.get("timeframe", "1h")

    open_now = _open_positions(session)
    if any(_norm(p.symbol) == _norm(symbol) for p in open_now):
        return {"symbol": symbol, "skipped": "position open — riding it"}
    # ROOM gate: never fire if the book is already full (respects the RISK.md max_open_positions).
    from app.core.state import get_or_create_risk_config

    max_pos = get_or_create_risk_config(session).max_open_positions
    if len(open_now) >= max_pos:
        return {"symbol": symbol, "skipped": f"no room ({len(open_now)}/{max_pos} positions open)"}
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

    # (b) The decider didn't hand us a decisive market trade (it armed or stood aside). We DON'T arm —
    # instead follow the AI's PRIMARY forward scenario and OPEN its next move NOW at market (quick win).
    scen = _open_scenario_move(session, symbol, ac, tf, prop.technical, cfg)
    if scen is not None:
        return scen

    return {"symbol": symbol, "skipped": f"no setup ({prop.direction.value}, {prop.confidence:.0%})"}


def run_auto_trade(session: Session) -> dict:
    """One pass over every enabled pair. Records to the audit log + stamps last_run."""
    cfg = get_or_create_auto_trade_config(session)
    results = [_auto_trade_symbol(session, cfg, p) for p in (cfg.pairs or [])]
    opened = [r for r in results if r.get("opened")]
    cfg.last_run_at = datetime.now(timezone.utc)
    acted = [f"{r['symbol']} {r['opened']}" for r in opened]
    cfg.last_result = "; ".join(acted) if acted else f"no action ({len(results)} pairs checked)"
    cfg.last_results = results   # per-pair outcome + reason, surfaced in the panel
    session.add(AgentRun(agent="auto_trade", event="tick",
                         detail={"pairs": len(results), "opened": len(opened), "results": results}))
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
