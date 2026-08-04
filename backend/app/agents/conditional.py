"""Conditional ('armed' / pending) setups — wait for a price trigger, then re-check + open.

A conditional setup is a valid trade idea whose ENTRY is deferred until price clears a level
(e.g. a break of a support cluster). The Monitor calls :func:`check_conditional_setups` each pass:
it expires stale ones, and when a trigger is hit it RE-VALIDATES the setup (the double-check) and
opens only if it still passes — so an armed setup can never bypass any safety gate.

The double-check validates the setup on its OWN plan (trigger/stop/target), NOT a fresh trade
re-derived from the current price. A break entry sits, by definition, right at the level it is
breaking, so a fresh "from here" read would see the next level <1R away and reject almost every
valid break (its real reward is measured from the trigger). Instead the trigger fires only when:
(1) no hard mechanical invalidation (the break already failed back through the stop, or the move
already ran to target), (2) the AI reviewer doesn't veto the thesis as broken (trend flip /
imminent event), and (3) the deterministic Risk Manager approves the size on the armed levels.

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
_INVALIDATE_LIFE_FRAC = 0.5  # a STALE zombie kill: once an arm is past this fraction of its validity
#                            window AND price (confirmed close) sits on the LOSING side of its stop, the
#                            thesis has had ample time and the move went the other way -> cancel. A FRESH
#                            arm past its stop is NOT killed (a breakout legitimately waits counter-current
#                            for a while — the JP225 buy_stop / USOIL sell_stop bug was killing those
#                            SECONDS after arming). Only the combo of "old enough + still wrong" cancels.
_TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
# Max overshoot past the trigger, as a fraction of the planned risk, before a breakout arm refuses to
# fire. 0.25 = the fill may cost at most a quarter of R more than budgeted (so real risk <= 1.25x the
# approved amount). Chosen to tolerate the normal slippage of waiting for the break to confirm while
# still catching a level that has genuinely run away.
_MAX_DRIFT_R = 0.25


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


# Break-confirmation: a STOP (breakout) order fires only after the break HOLDS on a lower timeframe —
# the last _CONFIRM_BARS closed candles of _CONFIRM_TF[setup-tf] must all be beyond the trigger. This
# filters wick-driven false breaks (price tags the level then snaps back). Limit (pullback) orders are
# not breaks, so this doesn't apply to them.
_CONFIRM_TF = {"1d": "1h", "4h": "1h", "1h": "15m", "30m": "5m", "15m": "5m", "5m": "1m", "1m": "1m"}
_CONFIRM_BARS = 2


def _break_confirmed(broker, s: ConditionalSetup) -> bool:
    """True when the last _CONFIRM_BARS closed candles of the confirmation timeframe hold beyond the
    trigger in the break direction. Conservative: if the lower-TF data can't be read, do NOT confirm."""
    ltf = _CONFIRM_TF.get(s.timeframe, "15m")
    try:
        candles = get_ohlcv_cached(broker, s.symbol, ltf, limit=_CONFIRM_BARS + 3).candles
    except Exception:  # noqa: BLE001
        return False
    if not candles or len(candles) < _CONFIRM_BARS:
        return False
    closes = [c.close for c in candles[-_CONFIRM_BARS:]]
    if s.order_type == "buy_stop":
        return all(c >= s.trigger_price for c in closes)   # break UP held
    return all(c <= s.trigger_price for c in closes)        # sell_stop: break DOWN held


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


def reconcile_triggered(session: Session) -> int:
    """Sync TRIGGERED Mode-A setups to the terminal state of the proposal they queued.

    At trigger time a Mode-A setup is marked TRIGGERED with the note "queued for your approval";
    that note is set once and never revisited. So once the user approves (-> proposal executed) or
    rejects it, the panel would otherwise keep claiming the setup is still waiting for approval — a
    stale, misleading row (the USDJPYm case: one already opened, one was rejected, yet both still
    read "queued for your approval"). Reconcile from the linked proposal:
      executed          -> note "break confirmed → opened" (matches the auto-execute convention)
      rejected/expired  -> the setup is REJECTED (you declined it) so it drops into "finished".
    Only touches rows still on the pending note, so it never clobbers an already-correct state.
    """
    rows = session.scalars(
        select(ConditionalSetup).where(
            ConditionalSetup.status == ConditionalStatus.TRIGGERED.value,
            ConditionalSetup.result_proposal_id.is_not(None),
            ConditionalSetup.last_note == "break confirmed → queued for your approval",
        )
    ).all()
    changed = 0
    for s in rows:
        p = session.get(TradeProposalRecord, s.result_proposal_id)
        if not p:
            continue
        if p.status == ProposalStatus.EXECUTED.value:
            s.last_note = "break confirmed → opened"
            session.add(s)
            changed += 1
        elif p.status in (ProposalStatus.REJECTED.value, ProposalStatus.EXPIRED.value):
            s.status = ConditionalStatus.REJECTED.value
            s.last_note = "you declined the triggered setup — not opened"
            session.add(s)
            changed += 1
    if changed:
        session.commit()
    return changed


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
    """Arm a conditional setup. Returns None if it has NO STOP-LOSS, a duplicate is already armed,
    the symbol is already open at the broker (never stack a pending on a live position), or the
    broker won't let us OPEN this symbol/direction (a disabled / close-only instrument would just
    get stuck re-arming and being vetoed at the trigger)."""
    # A stopless arm is a ZOMBIE: position size is derived from the stop distance, so the Risk
    # Manager's first gate vetoes it ("proposal missing entry/stop or not actionable") every single
    # time it triggers. It then re-arms, triggers, is vetoed again... until it expires, all while
    # LOOKING active in the panel. Refuse it at the door instead. (ETHUSDm #26 and XAUUSDm #39 both
    # burned their whole validity window this way; #39 also blocked nothing, so the Hybrid opened
    # the same idea at market underneath it.)
    if stop_loss is None:
        log.info("arm refused — no stop-loss (would be vetoed at every trigger)",
                 extra={"symbol": symbol, "order_type": order_type, "source": source})
        return None
    norm = _norm_symbol(symbol)
    for e in active_armed(session):
        if _norm_symbol(e.symbol) == norm and e.direction == direction:
            return None
    try:
        if any(_norm_symbol(p.symbol) == norm for p in live_broker_positions(session)):
            return None
    except Exception:  # noqa: BLE001 - if we can't read the book, still allow arming (no open)
        pass
    # Don't arm a setup the broker won't open (e.g. Exness has India 50 disabled) — it would only
    # trigger, get vetoed at the double-check, and re-arm in a loop until it expires.
    try:
        from app.brokers.registry import get_broker_for

        bm = get_or_create_settings(session).broker_map
        ok_open, why = get_broker_for(AssetClass(asset_class), bm).can_open(symbol, direction)
        if not ok_open:
            log.info("arm refused — not tradeable", extra={"symbol": symbol, "reason": why})
            return None
    except Exception:  # noqa: BLE001 - a tradeability lookup failure shouldn't block arming
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


def _decline(session: Session, s: ConditionalSetup, why: str, *, terminal: bool) -> int:
    """A re-check at the trigger did NOT open. By default keep the setup ARMED with a cooldown so a
    still-valid level can fire again when conditions realign (a decline is usually a TIMING miss, not
    a dead idea) — give up only when the idea is genuinely gone (``terminal``) or the retry cap is hit
    (the validity window also bounds it). Always returns 0."""
    s.retries += 1
    if terminal:
        s.status = ConditionalStatus.REJECTED.value
        s.last_note = f"{why} — rejected"
        log.info("conditional rejected", extra={"symbol": s.symbol, "note": s.last_note})
    elif s.retries >= _MAX_RETRIES:
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


# NOTE on pre-trigger invalidation: the ONLY pre-trigger kill is the STALE-ZOMBIE check in
# check_conditional_setups — an arm past HALF its validity window whose confirmed CLOSE still sits on the
# losing side of its stop is cancelled (the move went the other way and had its chance). Earlier versions
# were more eager and WRONGLY killed valid setups SECONDS after arming: a confirmed close back through a
# trend EMA(50) (Task 5), or ANY touch of the setup's own stop — a pending BREAKOUT legitimately waits
# counter-current on the 'wrong' side of its (post-entry) stop for a while (the JP225 buy_stop / USOIL
# sell_stop bug). The TIME gate is what makes the current check safe: a fresh arm is never killed for
# waiting; only a stale one that's still wrong. A target-based check stays redundant (reaching the target
# crosses the trigger, so the trigger-time re-check catches it). Otherwise an armed setup WAITS for its
# trigger, bounded by `valid_until`; the trigger-time double-check (_mechanical_invalidation + AI review +
# Risk Manager) prevents a broken idea from opening.


def _drift_too_far(s: ConditionalSetup, ref: float) -> str | None:
    """Has price run so far past the trigger that opening now would blow the risk budget?

    Size is derived from the PLANNED risk ``|trigger - stop|``, but a breakout arm deliberately waits
    for ``_CONFIRM_BARS`` closes beyond the trigger before firing, and then fills at MARKET. Every
    point of that overshoot is added to the real risk while the size stays as budgeted: trigger 100 /
    stop 99 filling at 100.4 is a 1.4-point loss on a position sized for 1.0 — 40% over the cap the
    Risk Manager just approved.

    So cap the overshoot. Beyond ``_MAX_DRIFT_R`` of planned R past the trigger the break has already
    run without us: don't chase our own breakout. NON-terminal — the level may still be worth taking
    on a pullback, and re-arming keeps that option.

    Only applies to STOP (breakout) orders. A limit arm fills on the FAVOURABLE side of its trigger
    (a buy_limit fires at or below it), so its drift only ever reduces risk."""
    if s.order_type not in ("buy_stop", "sell_stop") or s.stop_loss is None:
        return None
    planned_r = abs(s.trigger_price - s.stop_loss)
    if planned_r <= 0:
        return None
    # Overshoot = how far BEYOND the trigger price has gone, in the trade's direction.
    drift = (ref - s.trigger_price) if s.direction == Direction.LONG.value else (s.trigger_price - ref)
    if drift <= 0:
        return None                       # at or better than the trigger — nothing to chase
    frac = drift / planned_r
    if frac <= _MAX_DRIFT_R:
        return None
    return (f"break already ran {frac:.0%} of R past the trigger ({round(ref, 6)} vs "
            f"{round(s.trigger_price, 6)}) — entering here would risk ~{1 + frac:.1f}x the planned "
            f"amount; not chasing")


def _mechanical_invalidation(s: ConditionalSetup, ref: float) -> tuple[str | None, bool]:
    """Hard, no-judgement invalidations checked at the trigger. Returns ``(reason, terminal)``:

    - the move already ran to/through the TARGET -> missed; terminal (re-arming the same level is
      pointless — price is already at the destination), so reject.
    - price already snapped back through the protective STOP -> the break failed; NON-terminal, so
      re-arm (the level may set up and break cleanly later).

    ``None`` means no mechanical block; the thesis/risk checks decide from here."""
    if s.take_profit is not None and (
        (s.direction == Direction.LONG.value and ref >= s.take_profit)
        or (s.direction == Direction.SHORT.value and ref <= s.take_profit)
    ):
        return "price already reached the target — move played out", True
    if s.stop_loss is not None and (
        (s.direction == Direction.LONG.value and ref <= s.stop_loss)
        or (s.direction == Direction.SHORT.value and ref >= s.stop_loss)
    ):
        return "price already through the stop — break failed", False
    return None, False


def _reread_technical(session: Session, s: ConditionalSetup):
    """Re-read the market (deterministic, no LLM) for the reviewer's context + the audit trail.
    Returns a ``TechnicalRead`` or ``None`` if market data can't be fetched."""
    from app.agents.pipeline import _timeframes_for
    from app.agents.technical import run_technical
    from app.models.schemas import OHLCVSeries

    settings = get_or_create_settings(session)
    broker = get_broker_for(AssetClass(s.asset_class), settings.broker_map)
    series: list[OHLCVSeries] = []
    for tf in _timeframes_for(s.timeframe):
        try:
            series.append(get_ohlcv_cached(broker, s.symbol, tf, limit=200))
        except Exception:  # noqa: BLE001
            pass
    if not series:
        return None
    try:
        return run_technical(s.symbol, series, use_llm=False)
    except Exception:  # noqa: BLE001
        return None


def _fire(session: Session, s: ConditionalSetup, ref: float) -> int:
    """Trigger hit -> RE-VALIDATE THE ARMED SETUP ON ITS OWN LEVELS and open if it still qualifies.

    Unlike a fresh from-market analysis (which would reject almost every break entry because the
    level it is breaking sits <1R away), this validates the setup's OWN plan: reject only a hard
    mechanical invalidation, let the AI reviewer veto a broken thesis, then size + gate through the
    deterministic Risk Manager on the armed entry/stop/target. Returns 1 if it became a real
    trade/approval, else 0."""
    from app.agents.orchestrator import review_armed_setup
    from app.models.schemas import TradeProposal
    from app.risk.service import assess

    s.triggered_at = datetime.now(timezone.utc)

    # 1. Hard mechanical invalidations (no judgement, no LLM/data needed).
    reason, terminal = _mechanical_invalidation(s, ref)
    if reason:
        return _decline(session, s, reason, terminal=terminal)

    # 1b. Don't chase our own breakout: the fill is at market, but the SIZE was budgeted from the
    #     trigger, so an overshoot silently raises the real risk above the approved cap.
    drift = _drift_too_far(s, ref)
    if drift:
        return _decline(session, s, drift, terminal=False)

    # 2. Build the proposal from the ARMED levels (trigger = entry) — the whole point: the R:R is
    #    measured from the trigger, where it is real.
    technical = _reread_technical(session, s)
    proposal = TradeProposal(
        symbol=s.symbol, asset_class=AssetClass(s.asset_class), timeframe=s.timeframe,
        direction=Direction(s.direction), entry=s.trigger_price, stop_loss=s.stop_loss,
        take_profit=s.take_profit, confidence=s.confidence or 0.6,
        rationale=s.rationale or f"Armed {s.order_type} triggered at {s.trigger_price}.",
        technical=technical,
    )

    # 3. AI second opinion on the ARMED thesis (confirm/veto; vetoes only a clear invalidation such
    #    as a higher-timeframe trend flip or an imminent high-impact event). Needs a market read.
    if technical is not None:
        ok, why = review_armed_setup(proposal, technical, use_llm=True)
        proposal.review_decision = "veto" if not ok else "confirm"
        if not ok:
            return _decline(session, s, why, terminal=False)

    # 4. Deterministic Risk Manager — final gate, sized on the armed levels.
    try:
        decision = assess(session, proposal)
    except Exception as exc:  # noqa: BLE001
        log.warning("conditional risk assess error", extra={"symbol": s.symbol, "error": str(exc)})
        return _decline(session, s, f"risk re-check error: {exc}", terminal=False)

    # 5. Persist the proposal + verdict (audit trail + Mode-A approval queue).
    record = TradeProposalRecord(
        symbol=s.symbol, asset_class=s.asset_class, timeframe=s.timeframe,
        direction=proposal.direction.value, entry=proposal.entry, stop_loss=proposal.stop_loss,
        take_profit=proposal.take_profit, confidence=proposal.confidence,
        rationale=proposal.rationale, review_decision=proposal.review_decision,
        reasoning={"technical": technical.model_dump(mode="json")} if technical is not None else {},
        source="armed",  # opened from a triggered armed/conditional break setup
        risk_decision=decision.decision.value, risk_reason=decision.reason,
        approved_qty=decision.approved_qty, risk_amount=decision.risk_amount,
        status=(ProposalStatus.PENDING_APPROVAL.value if decision.approved
                else ProposalStatus.RISK_VETOED.value),
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    s.result_proposal_id = record.id

    if not decision.approved:
        return _decline(session, s, f"risk: {decision.reason}", terminal=False)

    # Honor the user's chosen lot (re-clamped to the 3% cap) for the size it opens at.
    if s.desired_lots:
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

    # 6. Auto-execute (Hybrid / Modes B-C) opens now; Mode A queues for the user's approval.
    if s.auto_execute:
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

    # Mode A, manually armed: leave the proposal awaiting the user's approval.
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
    invalidated = 0
    for s in armed:
        # Auto-re-arm cooldown: after a declined re-check, wait ~one bar before re-checking (skip
        # the broker call entirely while cooling down).
        if s.cooldown_until and _aware(s.cooldown_until) > now:
            continue

        broker = get_broker_for(AssetClass(s.asset_class), settings.broker_map)

        # MARKET HOURS: a closed session still returns the LAST tick, so every price test below would
        # run on a stale Friday quote — an arm could "trigger" on hours-old data and try to open into
        # the reopen gap. Every other engine (Hybrid / auto-trade / RSI-Over / analysis) already skips
        # a closed market; this one didn't. Just WAIT — never cancel, because the setup is still
        # perfectly valid, it simply can't be judged (or filled) until the session reopens.
        # Conservative on failure: if we can't tell, keep the old behaviour and evaluate.
        try:
            if not broker.market_open(s.symbol):
                continue
        except Exception:  # noqa: BLE001 - a market-hours lookup failure shouldn't freeze the arm
            pass

        try:
            price = broker.get_quote(s.symbol).price
        except Exception as exc:  # noqa: BLE001
            log.warning("conditional quote failed", extra={"symbol": s.symbol, "error": str(exc)})
            continue

        # One candle window per armed symbol/pass (cached): serves BOTH the close-confirm ref and the
        # trend-EMA invalidation below. Enough bars for EMA(50).
        closes: list[float] = []
        try:
            candles = get_ohlcv_cached(broker, s.symbol, s.timeframe, limit=60).candles
            closes = [c.close for c in candles] if candles else []
        except Exception:  # noqa: BLE001 - fall back to the live quote
            closes = []

        ref = price
        if s.require_close_confirm and closes:  # confirm on the latest candle close (avoid wick-driven false breaks)
            ref = closes[-1]

        # STALE-ZOMBIE INVALIDATION: a breakout order legitimately waits counter-current (on the wrong
        # side of its post-entry stop) for a WHILE, so we don't kill a fresh one. But once an arm is past
        # half its validity window AND price (confirmed CLOSE, never a wick) still sits on the LOSING side
        # of its stop, the thesis has had its chance and the market went the other way — cancel the zombie
        # instead of dangling to expiry (the AUDNZDm short that watched price make new highs for ~11h).
        inval_ref = closes[-1] if closes else price
        past_stop = s.stop_loss is not None and (
            (s.direction == Direction.SHORT.value and inval_ref >= s.stop_loss)
            or (s.direction == Direction.LONG.value and inval_ref <= s.stop_loss))
        stale = (s.created_at is not None and s.valid_until is not None
                 and now >= _aware(s.created_at) + (_aware(s.valid_until) - _aware(s.created_at)) * _INVALIDATE_LIFE_FRAC)
        if past_stop and stale:
            s.status = ConditionalStatus.CANCELLED.value
            s.last_note = (f"auto-cancelled — {int(_INVALIDATE_LIFE_FRAC * 100)}%+ through its window and "
                           f"price {round(inval_ref, 6)} is past the stop {round(s.stop_loss, 6)}; the move "
                           "went the other way (thesis broken).")
            session.add(s)
            invalidated += 1
            continue

        if not _crossed(s.order_type, ref, s.trigger_price):
            continue  # trigger not reached yet — keep waiting

        # BREAK CONFIRMATION (stop/breakout orders): the price tagged the trigger, but require the break
        # to HOLD — the last 2 closed candles of a lower timeframe must both be beyond the trigger — so
        # a wick that tags the level then snaps back does NOT fire. Limit (pullback) fills are unaffected.
        if s.order_type in ("buy_stop", "sell_stop") and not _break_confirmed(broker, s):
            s.last_note = (f"trigger {s.trigger_price} tagged — waiting for {_CONFIRM_BARS} "
                           f"{_CONFIRM_TF.get(s.timeframe, '15m')} closes beyond it to confirm the break")
            session.add(s)
            continue

        # Trigger hit + break confirmed.
        if ks:
            s.last_note = "trigger hit but kill-switch active — not opening"
            session.add(s)
            continue
        if not _has_room(session):
            s.last_note = "trigger hit but no room (position cap) — still armed"
            session.add(s)
            continue
        triggered += _fire(session, s, ref)

    session.commit()
    return {"checked": len(armed), "triggered": triggered, "expired": expired,
            "cancelled": cancelled, "invalidated": invalidated}
