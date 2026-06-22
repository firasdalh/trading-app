"""Hybrid auto-pilot mode.

One toggle. When enabled, a scheduled tick (every 30-45 min) does what a disciplined trader
would on a check-in:
  1. Is there room? (open positions < max_open_positions) — if not, do nothing.
  2. Scan the watchlist and rank setups (deterministic preview).
  3. Take ONLY the single best one whose confidence exceeds the threshold (default 70%),
     re-run the FULL analysis (LLM review can still veto / lower confidence), and auto-open it.

It opens at most one trade per tick. Every hard safety gate still applies downstream
(kill-switch, live-confirmation, daily-loss pause, exposure budget, anti-stacking, min lot),
so Hybrid can never bypass the Risk Manager — it only removes the manual approve click for a
high-conviction setup.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pipeline import analyze_symbol, preview_symbol
from app.core.logging import get_logger
from app.core.state import (
    get_or_create_risk_config,
    get_or_create_settings,
    kill_switch_active,
)
from app.models.db import AgentRun, HybridConfig, TradeProposalRecord, WatchItem
from app.models.enums import AssetClass, ProposalStatus
from app.risk.service import ScanCache, _norm_symbol, live_broker_positions

log = get_logger("agents.hybrid")

# Ranging fades are lower-conviction (engine caps them below trend setups), so the Hybrid takes one
# on a LOWER bar than trend trades — but only when it's the single best available (a stronger trend
# setup always outranks it). Trend setups still need the full min_confidence threshold.
_MR_MIN_CONFIDENCE = 0.60


def _effective_min_conf(strategy: str | None, threshold: float) -> float:
    """The confidence bar a setup must clear to be a Hybrid candidate — lower for a ranging fade."""
    if strategy == "mean_reversion":
        return min(threshold, _MR_MIN_CONFIDENCE)
    return threshold


def get_or_create_hybrid_config(session: Session) -> HybridConfig:
    cfg = session.get(HybridConfig, 1)
    if cfg is None:
        cfg = HybridConfig(id=1, enabled=False, interval_seconds=2100, min_confidence=0.70)
        session.add(cfg)
        session.commit()
    return cfg


def _arm_from(session: Session, cfg: HybridConfig, armable: list, exclude: str | None = None) -> None:
    """Arm the strongest blocked-but-valid candidates as conditional break-entries, up to the
    max_armed cap and skipping the symbol just opened at market. Hybrid-armed setups auto-execute
    on the trigger (after the re-check), so the gate is the double-check, not a manual click."""
    from app.agents.conditional import active_armed, arm_conditional

    room = max(0, cfg.max_armed - len(active_armed(session)))
    if room <= 0:
        return
    armed = 0
    for _conf, it, cond, direction in sorted(armable, key=lambda x: -x[0]):
        if armed >= room:
            break
        if exclude and _norm_symbol(it.symbol) == _norm_symbol(exclude):
            continue
        res = arm_conditional(
            session, symbol=it.symbol, asset_class=it.asset_class, timeframe=it.timeframe,
            direction=direction, order_type=cond.order_type, trigger_price=cond.trigger_price,
            stop_loss=cond.stop_loss, take_profit=cond.take_profit, confidence=cond.confidence,
            rr=cond.rr, rationale=cond.reason, source="hybrid", auto_execute=True,
        )
        if res is not None:
            armed += 1


def run_hybrid(session: Session) -> dict:
    """Scan + auto-open the single best qualifying setup (if any). Returns a short summary."""
    from app.agents.scanner import expire_stale_proposals

    expire_stale_proposals(session)  # clear stale pendings that would otherwise block symbols
    cfg = get_or_create_hybrid_config(session)
    threshold = cfg.min_confidence

    # Blocked-but-valid candidates worth ARMING as conditional break-entries (see _arm_from). Armed
    # on every exit path via done(), excluding any symbol we opened at market this tick.
    armable: list[tuple[float, WatchItem, object, str]] = []

    def done(reason: str, opened: dict | None = None) -> dict:
        if cfg.conditional_enabled and armable:
            _arm_from(session, cfg, armable, exclude=(opened or {}).get("symbol"))
        cfg.last_result = reason
        session.add(cfg)
        session.add(AgentRun(agent="hybrid", event="tick",
                             detail={"opened": opened, "reason": reason}))
        session.commit()
        return {"ran": True, "opened": opened, "reason": reason}

    # --- safety: kill-switch halts everything ---
    if kill_switch_active(session):
        return done("kill-switch active — no auto-open")

    # --- 1. room check (open positions < max_open_positions) ---
    max_pos = get_or_create_risk_config(session).max_open_positions
    try:
        open_positions = live_broker_positions(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("hybrid: broker positions failed", extra={"error": str(exc)})
        return done("could not read open positions")
    if len(open_positions) >= max_pos:
        return done(f"no room ({len(open_positions)}/{max_pos} open)")
    open_syms = {_norm_symbol(p.symbol) for p in open_positions}
    # Reuse this single open-book fetch for every per-symbol risk check in the scan below (primed
    # into a ScanCache), so ranking the watchlist doesn't make one broker round-trip per symbol.
    cache = ScanCache(open_book=[(p.symbol, p.direction) for p in open_positions])

    # --- 2. scan watchlist, rank deterministically; keep only qualifying candidates ---
    items = session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    pending_syms = {
        _norm_symbol(r.symbol) for r in session.scalars(
            select(TradeProposalRecord).where(
                TradeProposalRecord.status == ProposalStatus.PENDING_APPROVAL.value
            )
        )
    }
    candidates: list[tuple[float, WatchItem]] = []
    for it in items:
        if _norm_symbol(it.symbol) in open_syms or _norm_symbol(it.symbol) in pending_syms:
            continue
        try:
            # Rank deterministically (cheap, no LLM quota) — the engine's confidence is now
            # well-calibrated. The chosen best is then re-run with the LLM reviewer below before
            # it actually opens, so the LLM judgment still gates every auto-trade.
            prop, dec = preview_symbol(session, it.symbol, AssetClass(it.asset_class),
                                       it.timeframe, use_llm=False, cache=cache)
        except Exception as exc:  # noqa: BLE001 - one bad pair shouldn't stop the loop
            log.warning("hybrid preview failed", extra={"symbol": it.symbol, "error": str(exc)})
            continue
        if (prop.direction.value in ("long", "short") and dec.approved
                and prop.confidence > _effective_min_conf(prop.strategy, threshold)):
            candidates.append((prop.confidence, it))
        # A blocked-but-valid setup carries a 'wait for the break' conditional — collect it to arm.
        if prop.conditional is not None:
            cond_dir = "long" if prop.conditional.order_type.startswith("buy") else "short"
            armable.append((prop.conditional.confidence, it, prop.conditional, cond_dir))

    if not candidates:
        return done(f"no risk-approved setup above {threshold:.0%} confidence "
                    f"(ranging fades above {_MR_MIN_CONFIDENCE:.0%})")

    candidates.sort(key=lambda x: -x[0])
    _, best = candidates[0]

    # --- 3. full analysis (LLM review can veto / lower confidence) then auto-open ---
    res = analyze_symbol(session, best.symbol, AssetClass(best.asset_class), best.timeframe,
                         use_llm=True)
    record = session.get(TradeProposalRecord, res.proposal_id)
    if record is None:
        return done(f"{best.symbol}: proposal vanished")

    # Modes B/C may have already auto-executed it inside analyze_symbol.
    if record.status == ProposalStatus.EXECUTED.value:
        opened = {"symbol": best.symbol, "direction": record.direction,
                  "confidence": record.confidence}
        return done(f"opened {best.symbol} {record.direction} @ {record.confidence:.0%}", opened)

    if (not res.risk.approved
            or res.proposal.confidence <= _effective_min_conf(res.proposal.strategy, threshold)
            or res.proposal.direction.value not in ("long", "short")):
        return done(f"best ({best.symbol}) no longer qualifies after full review "
                    f"({res.proposal.confidence:.0%}, {res.risk.decision.value})")

    # Approve + execute (every gate enforced inside the executor).
    from app.execution.executor import ExecutionBlocked, execute_proposal

    record.status = ProposalStatus.APPROVED.value
    session.commit()
    try:
        execute_proposal(session, record)
    except ExecutionBlocked as exc:
        return done(f"{best.symbol}: execution blocked — {exc}")
    session.refresh(record)

    if record.status != ProposalStatus.EXECUTED.value:
        return done(f"{best.symbol}: order not filled ({record.status})")

    opened = {"symbol": best.symbol, "direction": record.direction, "confidence": record.confidence}
    log.warning("hybrid auto-opened a trade", extra=opened)
    return done(f"opened {best.symbol} {record.direction} @ {record.confidence:.0%}", opened)


def hybrid_tick(session: Session) -> dict:
    """Scheduler entrypoint: respect the enabled flag + interval, then run one hybrid pass."""
    cfg = get_or_create_hybrid_config(session)
    if not cfg.enabled:
        return {"ran": False, "reason": "disabled"}

    now = datetime.now(timezone.utc)
    if cfg.last_run_at is not None:
        last = cfg.last_run_at if cfg.last_run_at.tzinfo else cfg.last_run_at.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cfg.interval_seconds:
            return {"ran": False, "reason": "interval not elapsed"}

    cfg.last_run_at = now
    session.add(cfg)
    session.commit()
    return run_hybrid(session)
