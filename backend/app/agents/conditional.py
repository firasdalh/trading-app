"""Conditional ('armed' / pending) setups — wait for a price trigger, then re-check + open.

A conditional setup is a valid trade idea whose ENTRY is deferred until price clears a level
(e.g. a break of a support cluster). The Monitor calls :func:`check_conditional_setups` each pass:
it expires stale ones, and when a trigger is hit it RE-RUNS the full analysis (the double-check) —
the trade opens only if it still passes the Risk Manager + AI review at that moment. So an armed
setup can never bypass any safety gate; it just removes the "chase into structure" entry.

Auto-firing (Hybrid / Modes B-C) approves+executes on the trigger; in Mode A a manually-armed
setup is queued for the user's approval instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import (
    get_or_create_risk_config,
    get_or_create_settings,
    kill_switch_active,
)
from app.data.ohlcv_cache import get_ohlcv_cached
from app.models.db import AgentRun, ConditionalSetup, TradeProposalRecord
from app.models.enums import AssetClass, ConditionalStatus, Direction, ProposalStatus
from app.risk.service import _norm_symbol, live_broker_positions

log = get_logger("agents.conditional")

_DEFAULT_VALID_HOURS = 12
_MAX_RETRIES = 20          # give up auto-re-arming after this many declined re-checks
_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _cooldown_minutes(timeframe: str) -> int:
    """After a declined re-check, wait ~one bar of the setup's timeframe before re-checking — the
    momentum/RSI that caused the decline only updates per closed bar, so re-checking sooner is noise."""
    return _TF_MINUTES.get(timeframe, 60)


def _aware(dt: datetime) -> datetime:
    """SQLite may hand back naive datetimes; treat them as UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _crossed(order_type: str, ref: float, trigger: float) -> bool:
    """Has price reached the trigger for this order type?"""
    if order_type == "sell_stop":
        return ref <= trigger      # broke DOWN through support
    if order_type == "buy_stop":
        return ref >= trigger      # broke UP through resistance
    if order_type == "sell_limit":
        return ref >= trigger      # bounced UP into resistance
    if order_type == "buy_limit":
        return ref <= trigger      # dipped DOWN into support
    return False


def active_armed(session: Session) -> list[ConditionalSetup]:
    return list(session.scalars(
        select(ConditionalSetup).where(ConditionalSetup.status == ConditionalStatus.ARMED.value)
    ).all())


def list_conditionals(session: Session, *, limit: int = 100) -> list[ConditionalSetup]:
    """Active (armed) first, then most-recent others — for the Armed/Pending panel."""
    rows = list(session.scalars(
        select(ConditionalSetup).order_by(ConditionalSetup.id.desc()).limit(limit)
    ).all())
    rows.sort(key=lambda s: (s.status != ConditionalStatus.ARMED.value, -s.id))
    return rows


def clear_finished(session: Session) -> int:
    """Delete all non-armed (cancelled / rejected / expired / triggered) setups — they're terminal
    history, not active, so clearing them only tidies the panel. Returns how many were removed."""
    from sqlalchemy import delete

    res = session.execute(
        delete(ConditionalSetup).where(ConditionalSetup.status != ConditionalStatus.ARMED.value)
    )
    session.commit()
    return res.rowcount or 0


def cancel_conditional(session: Session, setup_id: int) -> bool:
    s = session.get(ConditionalSetup, setup_id)
    if s is None or s.status != ConditionalStatus.ARMED.value:
        return False
    s.status = ConditionalStatus.CANCELLED.value
    s.last_note = "cancelled by user"
    session.add(s)
    session.commit()
    return True


def arm_conditional(
    session: Session, *, symbol: str, asset_class: str, timeframe: str, direction: str,
    order_type: str, trigger_price: float, stop_loss: float | None, take_profit: float | None,
    confidence: float, rr: float | None, rationale: str = "", source: str = "manual",
    auto_execute: bool = False, valid_hours: int = _DEFAULT_VALID_HOURS,
    require_close_confirm: bool = True,
) -> ConditionalSetup | None:
    """Arm a conditional setup. Returns None if a duplicate is already armed or the symbol is
    already open at the broker (so we never stack a pending on top of a live position)."""
    norm = _norm_symbol(symbol)
    for e in active_armed(session):
        if _norm_symbol(e.symbol) == norm and e.direction == direction:
            return None
    try:
        if any(_norm_symbol(p.symbol) == norm for p in live_broker_positions(session)):
            return None
    except Exception:  # noqa: BLE001 - if we can't read the book, still allow arming (no open)
        pass

    s = ConditionalSetup(
        symbol=symbol, asset_class=asset_class, timeframe=timeframe, direction=direction,
        order_type=order_type, trigger_price=trigger_price, stop_loss=stop_loss,
        take_profit=take_profit, confidence=confidence, rr=rr, rationale=rationale,
        status=ConditionalStatus.ARMED.value, source=source, auto_execute=auto_execute,
        require_close_confirm=require_close_confirm,
        valid_until=datetime.now(timezone.utc) + timedelta(hours=valid_hours),
    )
    session.add(s)
    session.add(AgentRun(agent="conditional", symbol=symbol, event="armed",
                         detail={"order_type": order_type, "trigger": trigger_price,
                                 "source": source, "auto_execute": auto_execute}))
    session.commit()
    session.refresh(s)
    log.info("conditional armed", extra={"symbol": symbol, "order_type": order_type,
                                         "trigger": trigger_price, "source": source})
    return s


def _has_room(session: Session) -> bool:
    """Room to open a new position (broker-truth open count < cap). Conservative: if the open book
    can't be read, report no room so we never fire blind."""
    max_pos = get_or_create_risk_config(session).max_open_positions
    try:
        return len(live_broker_positions(session)) < max_pos
    except Exception:  # noqa: BLE001
        return False


def _fire(session: Session, s: ConditionalSetup) -> int:
    """Trigger hit -> re-run the full analysis (double-check) and open if it still qualifies.
    Returns 1 if it became a real trade/approval, else 0 (and marks the setup rejected)."""
    from app.agents.pipeline import analyze_symbol

    s.triggered_at = datetime.now(timezone.utc)
    try:
        res = analyze_symbol(session, s.symbol, AssetClass(s.asset_class), s.timeframe, use_llm=True)
    except Exception as exc:  # noqa: BLE001
        s.last_note = f"re-check error: {exc}"
        session.add(s)
        log.warning("conditional re-check error", extra={"symbol": s.symbol, "error": str(exc)})
        return 0

    s.result_proposal_id = res.proposal_id
    record = session.get(TradeProposalRecord, res.proposal_id)

    # The double-check must still want the SAME direction, be tradeable, and pass risk.
    if (res.proposal.direction.value != s.direction
            or res.proposal.direction not in (Direction.LONG, Direction.SHORT)
            or not res.risk.approved):
        # Decline is usually a TIMING miss (e.g. momentum bouncing at the break). Keep the setup
        # ARMED with a cooldown so it can fire again when momentum realigns — rather than killing a
        # still-valid level. Give up only after the retry cap (validity window also bounds it).
        s.retries += 1
        why = f"{res.proposal.direction.value} / {res.risk.decision.value}"
        if s.retries >= _MAX_RETRIES:
            s.status = ConditionalStatus.REJECTED.value
            s.last_note = f"re-check declined {s.retries}× — giving up ({why})"
            log.info("conditional rejected (retry cap)", extra={"symbol": s.symbol, "note": s.last_note})
        else:
            cd = _cooldown_minutes(s.timeframe)
            s.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=cd)
            s.last_note = f"re-check declined ({why}); re-armed — retry after ~{cd}m (try {s.retries})"
            log.info("conditional re-armed after decline",
                     extra={"symbol": s.symbol, "retries": s.retries, "cooldown_min": cd})
        session.add(s)
        return 0

    # Honor the user's chosen lot (re-clamped to the 3% cap) for the size it opens at.
    if s.desired_lots and record is not None and record.status == ProposalStatus.PENDING_APPROVAL.value:
        from app.risk.service import size_preview
        try:
            out = size_preview(session, record, desired_lots=s.desired_lots)
            dec = out["risk"]
            if dec.approved and dec.approved_qty and dec.approved_qty > 0:
                record.approved_qty = dec.approved_qty
                record.risk_amount = dec.risk_amount
                session.commit()
        except Exception as exc:  # noqa: BLE001 - fall back to the default size on any sizing error
            log.warning("conditional resize failed; using default size",
                        extra={"symbol": s.symbol, "error": str(exc)})

    # Modes B/C already auto-executed inside analyze_symbol.
    if record is not None and record.status == ProposalStatus.EXECUTED.value:
        s.status = ConditionalStatus.TRIGGERED.value
        s.last_note = "break confirmed → opened (auto mode)"
        session.add(s)
        log.warning("conditional opened", extra={"symbol": s.symbol})
        return 1

    # Auto-execute (Hybrid-armed): approve + execute now, like the Hybrid pick.
    if s.auto_execute and record is not None and record.status == ProposalStatus.PENDING_APPROVAL.value:
        from app.execution.executor import ExecutionBlocked, execute_proposal

        record.status = ProposalStatus.APPROVED.value
        session.commit()
        try:
            execute_proposal(session, record)
        except ExecutionBlocked as exc:
            s.status = ConditionalStatus.REJECTED.value
            s.last_note = f"execution blocked: {exc}"
            session.add(s)
            return 0
        session.refresh(record)
        if record.status == ProposalStatus.EXECUTED.value:
            s.status = ConditionalStatus.TRIGGERED.value
            s.last_note = "break confirmed → opened"
            session.add(s)
            log.warning("conditional opened", extra={"symbol": s.symbol})
            return 1
        s.status = ConditionalStatus.TRIGGERED.value
        s.last_note = "approved but broker did not fill — check terminal"
        session.add(s)
        return 0

    # Mode A, manually armed: leave the fresh proposal awaiting the user's approval.
    s.status = ConditionalStatus.TRIGGERED.value
    s.last_note = "break confirmed → queued for your approval"
    session.add(s)
    log.info("conditional queued for approval", extra={"symbol": s.symbol})
    return 1


def check_conditional_setups(session: Session) -> dict:
    """One pass: expire stale armed setups, cancel any whose symbol is already open at the broker,
    then fire any whose trigger is hit (with re-check)."""
    armed = active_armed(session)
    if not armed:
        return {"checked": 0, "triggered": 0, "expired": 0, "cancelled": 0}

    settings = get_or_create_settings(session)
    now = datetime.now(timezone.utc)
    expired = 0
    for s in armed:
        if s.valid_until and _aware(s.valid_until) <= now:
            s.status = ConditionalStatus.EXPIRED.value
            s.last_note = "validity window elapsed without a trigger"
            session.add(s)
            expired += 1
    if expired:
        session.commit()
        armed = [s for s in armed if s.status == ConditionalStatus.ARMED.value]

    # A symbol already open at the broker makes its armed setup redundant — you're in the trade, and
    # firing it would only get blocked by anti-stacking. Auto-cancel it so it can't double up and the
    # panel stays honest. (live_broker_positions returns [] on an outage -> we never cancel blindly.)
    try:
        open_syms = {_norm_symbol(p.symbol) for p in live_broker_positions(session)}
    except Exception:  # noqa: BLE001
        open_syms = set()
    cancelled = 0
    for s in armed:
        if _norm_symbol(s.symbol) in open_syms:
            s.status = ConditionalStatus.CANCELLED.value
            s.last_note = "auto-cancelled — a position is already open for this symbol"
            session.add(s)
            cancelled += 1
    if cancelled:
        session.commit()
        armed = [s for s in armed if s.status == ConditionalStatus.ARMED.value]

    ks = kill_switch_active(session)
    triggered = 0
    for s in armed:
        # Auto-re-arm cooldown: after a declined re-check, wait ~one bar before re-checking (skip
        # the broker call entirely while cooling down).
        if s.cooldown_until and _aware(s.cooldown_until) > now:
            continue

        broker = get_broker_for(AssetClass(s.asset_class), settings.broker_map)
        try:
            price = broker.get_quote(s.symbol).price
        except Exception as exc:  # noqa: BLE001
            log.warning("conditional quote failed", extra={"symbol": s.symbol, "error": str(exc)})
            continue

        ref = price
        if s.require_close_confirm:
            try:  # confirm on the latest candle close (avoid wick-driven false breaks)
                candles = get_ohlcv_cached(broker, s.symbol, s.timeframe, limit=3).candles
                if candles:
                    ref = candles[-1].close
            except Exception:  # noqa: BLE001 - fall back to the live quote
                pass

        if not _crossed(s.order_type, ref, s.trigger_price):
            continue  # trigger not reached yet — keep waiting (a break order sits on the far side
            # of its stop until the break, so there is no "price reached the stop" invalidation here)

        # Trigger hit.
        if ks:
            s.last_note = "trigger hit but kill-switch active — not opening"
            session.add(s)
            continue
        if not _has_room(session):
            s.last_note = "trigger hit but no room (position cap) — still armed"
            session.add(s)
            continue
        triggered += _fire(session, s)

    session.commit()
    return {"checked": len(armed), "triggered": triggered, "expired": expired, "cancelled": cancelled}
