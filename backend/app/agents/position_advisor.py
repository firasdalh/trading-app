"""Open-position management advisor (read-only).

Separate from new-entry analysis: for each OPEN broker position it gives disciplined
guidance — is the trade still on track vs. the original plan, protect a winner, cut a loser,
set a missing stop — with special attention to imminent high-impact news events (the classic
"do I hold through the release?" decision).

It runs two checks per position:
  1. Thesis re-check — a fresh deterministic read of the symbol; does the current trend /
     momentum still agree with the side you're holding? (intact / weakening / invalidated)
  2. Event proximity — is a high-impact release imminent?

Advisory only: it suggests; the user acts via the positions table (Set SL/TP, Close). It
never moves money on its own. Can be run on demand or on a user-set auto-watch interval.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.data.providers import get_calendar_provider
from app.models.db import AdvisorConfig, AgentRun, TradeProposalRecord
from app.models.schemas import PositionAdvice
from app.risk.service import live_broker_positions

log = get_logger("agents.advisor")

_IMMINENT_BEFORE_MIN = 90   # an event this soon is "imminent"
_IN_WINDOW_AFTER_MIN = 30   # still relevant up to 30 min after the release
_SEV_RANK = {"info": 0, "warn": 1, "danger": 2}

# --- precision: don't cry wolf on tiny moves ---
_MOM_ATR_FRAC = 0.10        # momentum counts as "against" only if |MACD hist| >= 10% of ATR

# --- auto-manage thresholds (R = profit / planned risk) ---
_BREAKEVEN_R = 1.0          # at +1R, lock the stop to breakeven (trending/moderate regime)
_TRAIL_R = 1.5             # beyond +1.5R, trail the stop (trending/moderate regime)
_BREAKEVEN_R_FAST = 0.5     # volatile/ranging regime: bank sooner -> breakeven at +0.5R
_TRAIL_R_FAST = 1.0         # volatile/ranging regime: start trailing at +1R
_TRAIL_ATR_MULT = 1.0       # ATR trailing distance = 1 ATR behind price
_STRUCT_TRAIL_BUFFER_ATR = 0.2  # structural trail sits this far beyond the swing (wick allowance)
_PROTECT_ATR_MULT = 1.5     # protective stop for a naked position = 1.5 ATR from price

# --- hysteresis + cooldown so auto-execute doesn't thrash ---
_CLOSE_CONFIRM = 2          # require N consecutive "invalidated" reads before auto-closing
_ACTION_COOLDOWN_S = 600    # min seconds between auto-CLOSE actions on the same symbol
_INVALID_STREAK: dict[str, int] = {}
_LAST_CLOSE_AT: dict[str, datetime] = {}


def _reset_auto_state() -> None:
    """Clear hysteresis/cooldown trackers (used by tests; also safe to call on restart)."""
    _INVALID_STREAK.clear()
    _LAST_CLOSE_AT.clear()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _plan_proposal(session: Session, symbol: str, direction: str | None = None):
    """The proposal that actually OPENED the current position.

    Prefers the most recent EXECUTED proposal matching symbol + direction (the no-stacking rule
    guarantees one open trade per symbol+direction, so that's this trade's real entry plan).
    Falls back to the latest proposal for the symbol. This avoids using a *later* re-analysis's
    levels/timeframe — which the scanner/hybrid create constantly — for the R-multiple and the
    thesis re-check.
    """
    from app.models.enums import ProposalStatus
    from app.risk.service import _norm_symbol

    norm = _norm_symbol(symbol)
    rows = session.scalars(
        select(TradeProposalRecord).order_by(TradeProposalRecord.id.desc()).limit(120)
    ).all()
    same = [r for r in rows if _norm_symbol(r.symbol) == norm]
    if direction is not None:
        for r in same:
            if r.status == ProposalStatus.EXECUTED.value and r.direction == direction:
                return r
    return same[0] if same else None


def _planned_timeframe(session: Session, symbol: str, direction: str | None = None) -> str:
    """The timeframe the trade was opened on (so the re-check matches the actual plan)."""
    row = _plan_proposal(session, symbol, direction)
    return row.timeframe if (row and row.timeframe) else "1h"


def _plan_risk(session: Session, symbol: str, atr: float | None,
               direction: str | None = None) -> float | None:
    """The risk-per-unit the trade was planned with (|entry − stop|), used to express progress
    in R. Falls back to the engine's default ATR stop distance when no plan is on record."""
    try:
        row = _plan_proposal(session, symbol, direction)
        if row and row.entry and row.stop_loss:
            r = abs(row.entry - row.stop_loss)
            if r > 0:
                return r
    except Exception:  # noqa: BLE001
        pass
    return _PROTECT_ATR_MULT * atr if atr else None


def _macro_tf_label(tech, fallback: str) -> str:
    """Label of the highest-timeframe read available (the dominant context)."""
    from app.agents.orchestrator import _macro_tf

    m = _macro_tf(tech)
    return m.timeframe if m else fallback


def _position_context(session: Session, p) -> dict | None:
    """Fresh deterministic read for one open position (trend, momentum, ATR, last price). Best
    effort — returns ``None`` if data/broker is unavailable. No LLM, so it's free per tick."""
    try:
        from app.agents.orchestrator import (
            _macro_trend,
            _regime,
            _structure_label,
            _trend_from_indicators,
        )
        from app.agents.technical import run_technical
        from app.brokers.registry import get_broker_for
        from app.models.enums import AssetClass

        ac = AssetClass(p.asset_class)
        broker = get_broker_for(ac, get_or_create_settings(session).broker_map)
        tf = _planned_timeframe(session, p.symbol, p.direction)
        series = []
        for t in dict.fromkeys([tf, "1h", "1d"]):
            try:
                series.append(broker.get_ohlcv(p.symbol, t, limit=200))
            except Exception:  # noqa: BLE001
                pass
        if not series:
            return None
        tech = run_technical(p.symbol, series, use_llm=False)
        if not tech.timeframes:
            return None
        prim = next((x for x in tech.timeframes if x.timeframe == tf), tech.timeframes[0])
        macro_tf = _macro_tf_label(tech, tf)
        ind = prim.indicators
        # Carry the same chart-reading signals the entry engine uses, so EXIT management is as
        # senior-trader as entry: market structure, change-of-character, the latest swing levels
        # (for structural trailing), and the regime (for how aggressively to manage).
        return {"tf": tf, "trend": _trend_from_indicators(ind, prim.trend),
                "macro": _macro_trend(tech), "macro_tf": macro_tf,
                "macd_hist": ind.get("macd_hist"), "atr": ind.get("atr14"),
                "last": ind.get("last_close"),
                "regime": _regime(ind), "structure": _structure_label(ind),
                "choch": bool(ind.get("choch")),
                "swing_high": ind.get("swing_high"), "swing_low": ind.get("swing_low")}
    except Exception:  # noqa: BLE001 - the advisor must never crash the scan/endpoint
        return None


def _thesis_from_context(p, ctx: dict) -> dict:
    """Classify the plan vs. the current read: intact / weakening / invalidated. A trend FLIP
    (EMA structure) invalidates; a flattening trend or *meaningful* counter-momentum weakens;
    trivial counter-momentum (noise) is ignored."""
    tf, trend = ctx["tf"], ctx["trend"]
    macd_hist, atr = ctx.get("macd_hist"), ctx.get("atr")
    macro, macro_tf = ctx.get("macro"), ctx.get("macro_tf", tf)
    want = "up" if p.direction == "long" else "down"
    opp = "down" if p.direction == "long" else "up"

    if trend == opp:
        # Only call it INVALIDATED when the higher timeframe confirms the flip. An entry-TF flip
        # while the higher TF still supports the trade is usually a pullback, not a breakdown —
        # treat it as weakening so we don't auto-exit good trades on noise (the crypto lesson).
        if macro == opp:
            return {"label": "invalidated",
                    "note": (f"Plan check: the trend now reads {trend.upper()} against your "
                             f"{p.direction} on BOTH {tf} and the higher {macro_tf} timeframe. "
                             "The setup no longer holds — consider exiting rather than hoping.")}
        return {"label": "weakening",
                "note": (f"Plan check: {tf} flipped {trend.upper()} against your {p.direction}, "
                         f"but the higher {macro_tf} timeframe is still "
                         f"{(macro or 'mixed').upper()} — likely a pullback, not a breakdown. "
                         "Tighten the stop; don't bail blindly.")}

    # Change-of-character: the structure we're riding just cracked (price broke back through the
    # last swing). It's the EARLIEST reversal warning — before the EMA trend even flips — so a pro
    # tightens / takes profit here rather than waiting. Treat as weakening.
    if ctx.get("choch") and ctx.get("structure") == want:
        broke = "higher-low" if p.direction == "long" else "lower-high"
        return {"label": "weakening",
                "note": (f"Plan check: change-of-character on {tf} — price broke the last {broke} "
                         "swing. The structure is cracking; tighten the stop or take profit.")}

    against = macd_hist is not None and (
        (p.direction == "long" and macd_hist < 0) or (p.direction == "short" and macd_hist > 0)
    )
    meaningful = against and atr and abs(macd_hist) >= _MOM_ATR_FRAC * atr
    if trend != want:  # sideways: EMAs no longer aligned with the position
        return {"label": "weakening",
                "note": (f"Plan check: the {tf} trend is flattening (now {trend}). "
                         "Thesis weakening — tighten the stop or consider trimming.")}
    if meaningful:
        return {"label": "weakening",
                "note": (f"Plan check: momentum is rolling over on {tf} (MACD hist {macd_hist}). "
                         "Thesis weakening — tighten the stop or consider trimming.")}
    return {"label": "intact",
            "note": f"Plan check: thesis intact — the {tf} trend is still {trend}, in your favour."}


def _position_thesis(session: Session, p, ctx: dict | None = None) -> dict | None:
    """Thesis re-check; reuses a precomputed context when given (avoids a second broker fetch)."""
    if ctx is None:
        ctx = _position_context(session, p)
    return _thesis_from_context(p, ctx) if ctx else None


def _r_multiple(session: Session, p, ctx: dict | None) -> float | None:
    if not ctx:
        return None
    last = ctx.get("last") or p.last_price
    risk = _plan_risk(session, p.symbol, ctx.get("atr"), p.direction)
    if not last or not risk:
        return None
    profit = (last - p.entry_price) if p.direction == "long" else (p.entry_price - last)
    return round(profit / risk, 2)


def _base_advice(p, ev_label, ev_mins, winning, has_stop) -> tuple[str, str, str]:
    """Event-proximity + protection advice (before folding in the thesis re-check)."""
    if ev_label is not None:
        when_txt = f"in ~{ev_mins}m" if (ev_mins or 0) > 0 else "now"
        if not has_stop:
            return ("danger", f"Protect {p.symbol} before {ev_label} ({when_txt})",
                    "No stop is set and a high-impact event is imminent. Set a stop now or close — "
                    "holding through news unprotected is high risk.")
        if winning:
            return ("warn", f"{p.symbol} is winning into {ev_label} ({when_txt})",
                    f"Lock it in: move the stop to breakeven (entry {p.entry_price}) or take "
                    "partial/full profit before the release. News can reverse a winner fast.")
        return ("warn", f"{p.symbol} is losing into {ev_label} ({when_txt})",
                "Consider closing or reducing before the release — a spike can deepen the loss. "
                "At minimum keep a tight stop.")
    if not has_stop:
        return ("warn", f"{p.symbol}: no stop set", "Add a stop to cap risk on this open position.")
    if winning:
        return ("info", f"{p.symbol} in profit",
                "No imminent events. Consider trailing the stop to lock gains; otherwise hold and "
                "let it work toward target.")
    return ("info", f"{p.symbol} open",
            "No imminent events and a stop is in place — hold and let stop/target manage it.")


def advise_positions(session: Session) -> list[PositionAdvice]:
    """Advisories for the panel/endpoint (the per-position fresh read is discarded)."""
    return _advise_with_context(session)[0]


def _advise_with_context(session: Session) -> tuple[list[PositionAdvice], dict[str, dict]]:
    """Compute advisories AND return the per-symbol fresh context, so the auto-execute pass can
    reuse it instead of re-fetching the broker/indicators for every position a second time."""
    now = datetime.now(timezone.utc)
    cal = get_calendar_provider()
    out: list[PositionAdvice] = []
    contexts: dict[str, dict] = {}

    for p in live_broker_positions(session):
        try:
            events = cal.get_events(p.symbol, lookahead_hours=12)
        except Exception:  # noqa: BLE001
            events = []

        ev_label: str | None = None
        ev_mins: int | None = None
        for e in events:
            if str(e.importance).lower() != "high":
                continue
            mins = int((_aware(e.when) - now).total_seconds() / 60)
            if -_IN_WINDOW_AFTER_MIN <= mins <= _IMMINENT_BEFORE_MIN:
                if ev_mins is None or mins < ev_mins:
                    ev_label, ev_mins = e.label, mins

        winning = p.unrealized_pnl > 0
        has_stop = p.stop_loss is not None and p.stop_loss != 0

        severity, headline, detail = _base_advice(p, ev_label, ev_mins, winning, has_stop)

        ctx = _position_context(session, p)
        if ctx:
            contexts[p.symbol] = ctx
        thesis = _position_thesis(session, p, ctx)
        r_mult = _r_multiple(session, p, ctx)
        thesis_label = thesis["label"] if thesis else "unknown"
        if thesis is not None:
            detail = f"{detail} {thesis['note']}"
            if r_mult is not None:
                detail = f"{detail} (currently {r_mult:+.1f}R)"
            # The thesis can escalate urgency. News keeps its headline (it's the nearer concern);
            # otherwise the thesis drives the headline too.
            if thesis_label == "invalidated":
                severity = "danger"
                if ev_label is None:
                    headline = f"{p.symbol}: thesis broken — consider exiting"
            elif thesis_label == "weakening" and _SEV_RANK[severity] < _SEV_RANK["warn"]:
                severity = "warn"
                if ev_label is None:
                    headline = f"{p.symbol}: thesis weakening"

        out.append(PositionAdvice(
            symbol=p.symbol, direction=p.direction, unrealized_pnl=round(p.unrealized_pnl, 2),
            has_stop=has_stop, severity=severity, headline=headline, detail=detail,
            thesis=thesis_label, r_multiple=r_mult, event_label=ev_label, minutes_to_event=ev_mins,
        ))
    return out, contexts


# --------------------------------------------------------------------------- #
#  Auto-watch config + scheduled tick
# --------------------------------------------------------------------------- #


def get_or_create_advisor_config(session: Session) -> AdvisorConfig:
    cfg = session.get(AdvisorConfig, 1)
    if cfg is None:
        cfg = AdvisorConfig(id=1, enabled=False, auto_execute=False, auto_reenter=False,
                            interval_seconds=300)
        session.add(cfg)
        session.commit()
    return cfg


def _maybe_reenter(session: Session, symbol: str, asset_class: str) -> dict:
    """After auto-closing an invalidated trade, re-run the FULL analysis and — only if the
    deterministic engine + risk manager approve a fresh setup — open it properly (risk-sized,
    with SL/TP attached by the executor). Never forces a trade; stays flat otherwise."""
    from app.agents.pipeline import analyze_symbol
    from app.execution.executor import ExecutionBlocked, execute_proposal
    from app.models.enums import AssetClass, Direction, ProposalStatus

    tf = _planned_timeframe(session, symbol)
    try:
        res = analyze_symbol(session, symbol, AssetClass(asset_class), tf, use_llm=False)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "action": "reenter", "kind": "open", "ok": False,
                "reason": f"re-analysis failed: {exc}"}

    prop = res.proposal
    if prop.direction not in (Direction.LONG, Direction.SHORT) or not res.risk.approved:
        return {"symbol": symbol, "action": "reenter_skip", "kind": "open", "ok": False,
                "reason": f"no fresh setup ({prop.direction.value} / risk {res.risk.decision.value})"}

    record = session.get(TradeProposalRecord, res.proposal_id)
    detail = (f"opened {prop.direction.value} @ {prop.entry} "
              f"(SL {prop.stop_loss} / TP {prop.take_profit})")
    # Modes B/C may have already auto-executed it inside analyze_symbol.
    if record and record.status == ProposalStatus.EXECUTED.value:
        return {"symbol": symbol, "action": "reenter", "kind": "open", "ok": True,
                "reason": detail, "stop": prop.stop_loss}
    try:
        execute_proposal(session, record)
        session.commit()
        ok = record.status == ProposalStatus.EXECUTED.value
        return {"symbol": symbol, "action": "reenter", "kind": "open", "ok": ok,
                "reason": detail if ok else "order did not fill", "stop": prop.stop_loss}
    except ExecutionBlocked as exc:
        return {"symbol": symbol, "action": "reenter_blocked", "kind": "open", "ok": False,
                "reason": str(exc)}


def _stop_worse_than_entry(p) -> bool:
    """True if the stop is on the losing side of entry (i.e. not yet locked to breakeven)."""
    if p.stop_loss is None:
        return True
    return p.stop_loss < p.entry_price if p.direction == "long" else p.stop_loss > p.entry_price


def _tightens(direction: str, current: float | None, new: float) -> bool:
    """A new stop is only ever accepted if it REDUCES risk (moves toward price). Never loosen."""
    if current is None:
        return True
    return new > current if direction == "long" else new < current


def _trail_stop(direction: str, last: float, atr: float, ctx: dict, regime: str) -> tuple[float, str]:
    """Where to trail the stop. In a TRENDING regime, trail behind the last swing (structure) to
    give the move room — like a trend trader riding it; otherwise (volatile/ranging/moderate) use a
    tighter ATR trail to bank gains. Returns (level, basis)."""
    swing = ctx.get("swing_low") if direction == "long" else ctx.get("swing_high")
    if regime == "trending" and swing is not None:
        buf = _STRUCT_TRAIL_BUFFER_ATR * atr
        return ((swing - buf) if direction == "long" else (swing + buf)), "behind structure"
    atr_trail = (last - _TRAIL_ATR_MULT * atr) if direction == "long" else (last + _TRAIL_ATR_MULT * atr)
    return atr_trail, "ATR"


def _auto_decision(a: PositionAdvice, p, ctx: dict, plan_risk: float | None) -> dict | None:
    """The bounded, deterministic set of actions the advisor may take autonomously — only
    highest-confidence, RISK-REDUCING moves: close an invalidated trade, attach a protective
    stop, lock to breakeven, or trail. It never opens, sizes up, flips, or loosens a stop.

    Regime-aware: a clean trend gets room (trail behind structure, normal R thresholds); a
    volatile/ranging tape banks sooner (tighter ATR trail, lower breakeven/trail R)."""
    if a.thesis == "invalidated":
        return {"action": "close", "kind": "close",
                "reason": "thesis invalidated — the trend has flipped against the position"}

    atr = (ctx or {}).get("atr")
    last = (ctx or {}).get("last") or p.last_price
    d = p.direction
    regime = (ctx or {}).get("regime") or "moderate"

    # 1) Naked position -> attach an ATR protective stop (always risk-reducing).
    if (p.stop_loss is None or p.stop_loss == 0) and atr and last:
        stop = (last - _PROTECT_ATR_MULT * atr) if d == "long" else (last + _PROTECT_ATR_MULT * atr)
        return {"action": "set_stop", "kind": "protect", "stop": round(stop, 5),
                "reason": "no stop set — attaching an ATR protective stop"}

    # 2) R-based management once we have a risk reference and a price. In a choppy/volatile tape we
    # take profit protection earlier (lower R), and the trail style follows the regime.
    bank_fast = regime in ("volatile", "ranging")
    trail_r = _TRAIL_R_FAST if bank_fast else _TRAIL_R
    be_r = _BREAKEVEN_R_FAST if bank_fast else _BREAKEVEN_R
    if plan_risk and last and atr:
        profit = (last - p.entry_price) if d == "long" else (p.entry_price - last)
        r = profit / plan_risk
        if r >= trail_r:
            trail, basis = _trail_stop(d, last, atr, ctx or {}, regime)
            if _tightens(d, p.stop_loss, trail):
                return {"action": "set_stop", "kind": "trail", "stop": round(trail, 5),
                        "reason": f"+{r:.1f}R ({regime}) — trailing the stop ({basis}) to lock gains"}
        if r >= be_r and _stop_worse_than_entry(p):
            return {"action": "set_stop", "kind": "breakeven", "stop": round(p.entry_price, 5),
                    "reason": f"reached +{r:.1f}R — moving the stop to breakeven"}

    # 2b) Thesis weakening while in profit -> tighten the stop NOW (don't wait for +1.5R). Locks
    # gains exactly when the read is deteriorating; only ever risk-reducing.
    if a.thesis == "weakening" and atr and last:
        profit = (last - p.entry_price) if d == "long" else (p.entry_price - last)
        if profit > 0:
            tight = (last - _TRAIL_ATR_MULT * atr) if d == "long" else (last + _TRAIL_ATR_MULT * atr)
            if _tightens(d, p.stop_loss, tight):
                return {"action": "set_stop", "kind": "tighten", "stop": round(tight, 5),
                        "reason": "thesis weakening while in profit — tightening the stop to lock gains"}

    # 3) Winning into imminent news -> lock breakeven even before +1R.
    if a.event_label is not None and p.unrealized_pnl > 0 and _stop_worse_than_entry(p):
        return {"action": "set_stop", "kind": "breakeven", "stop": round(p.entry_price, 5),
                "reason": f"winning into {a.event_label} — locking the stop to breakeven"}
    return None


def _reconcile_closed_symbol(session: Session, symbol: str) -> None:
    """Book any app-tracked open position for this symbol as closed (mirrors live_close)."""
    from app.core.state import get_or_create_daily_state
    from app.models.db import Position
    from app.models.enums import PositionStatus

    rows = session.scalars(
        select(Position).where(Position.symbol == symbol,
                               Position.status == PositionStatus.OPEN.value)
    ).all()
    if not rows:
        return
    daily = get_or_create_daily_state(session)
    for r in rows:
        r.status = PositionStatus.CLOSED.value
        r.closed_at = datetime.now(timezone.utc)
        r.realized_pnl = r.unrealized_pnl or 0.0
        daily.realized_pnl = round(daily.realized_pnl + (r.unrealized_pnl or 0.0), 2)


def _auto_execute(session: Session, advice: list[PositionAdvice],
                  contexts: dict[str, dict] | None = None) -> list[dict]:
    """Act on the bounded auto-decisions. Hard safety gates: kill switch halts everything; live
    brokers require this session's live-confirmation; paper acts freely. Closing requires the
    invalidation to persist (hysteresis) and is rate-limited per symbol (cooldown); protective
    stop moves act immediately (they only ever reduce risk)."""
    from app.brokers.registry import get_broker_for
    from app.core.state import (
        get_or_create_settings,
        kill_switch_active,
        live_execution_allowed,
    )
    from app.models.enums import AssetClass

    actions: list[dict] = []
    if kill_switch_active(session):
        log.warning("advisor auto-execute skipped: kill switch active")
        return actions

    now = datetime.now(timezone.utc)
    settings = get_or_create_settings(session)
    positions = {p.symbol: p for p in live_broker_positions(session)}
    contexts = contexts or {}

    for a in advice:
        p = positions.get(a.symbol)
        if p is None:
            continue

        # Track consecutive invalidations for the close-hysteresis gate.
        if a.thesis == "invalidated":
            _INVALID_STREAK[a.symbol] = _INVALID_STREAK.get(a.symbol, 0) + 1
        else:
            _INVALID_STREAK[a.symbol] = 0

        ctx = contexts.get(a.symbol) or _position_context(session, p)
        plan_risk = _plan_risk(session, a.symbol, (ctx or {}).get("atr"), a.direction)
        decision = _auto_decision(a, p, ctx or {}, plan_risk)
        if decision is None:
            continue
        action, kind, reason = decision["action"], decision["kind"], decision["reason"]

        if action == "close":
            if _INVALID_STREAK.get(a.symbol, 0) < _CLOSE_CONFIRM:
                actions.append({"symbol": a.symbol, "action": "close_pending", "kind": "close",
                                "ok": False, "reason": (
                                    f"awaiting confirmation "
                                    f"({_INVALID_STREAK[a.symbol]}/{_CLOSE_CONFIRM} checks)")})
                continue
            last_close = _LAST_CLOSE_AT.get(a.symbol)
            if last_close and (now - last_close).total_seconds() < _ACTION_COOLDOWN_S:
                continue

        broker = get_broker_for(AssetClass(p.asset_class), settings.broker_map)
        if not broker.is_paper and not live_execution_allowed(settings):
            log.warning("advisor auto-execute blocked (live not confirmed)",
                        extra={"symbol": a.symbol, "intended": action, "kind": kind})
            actions.append({"symbol": a.symbol, "action": "blocked_live_unconfirmed", "kind": kind,
                            "intended": action, "ok": False, "reason": reason})
            continue

        try:
            if action == "close":
                result = broker.close_position(p.symbol)
                ok = result.status.value not in ("error", "rejected")
                if ok:
                    _reconcile_closed_symbol(session, p.symbol)
                    _LAST_CLOSE_AT[a.symbol] = now
                    _INVALID_STREAK[a.symbol] = 0
            else:  # set_stop (protect | breakeven | trail)
                result = broker.set_sl_tp(p.symbol, decision["stop"], p.take_profit)
                ok = result.status.value not in ("error", "rejected")
            actions.append({"symbol": a.symbol, "action": action, "kind": kind, "ok": ok,
                            "reason": reason, "stop": decision.get("stop"),
                            "asset_class": p.asset_class,
                            "error": None if ok else (result.error or "broker rejected")})
            log.warning("advisor AUTO-EXECUTED", extra={"symbol": a.symbol, "action": action,
                        "kind": kind, "stop": decision.get("stop"), "ok": ok, "paper": broker.is_paper})
        except Exception as exc:  # noqa: BLE001
            actions.append({"symbol": a.symbol, "action": action, "kind": kind, "ok": False,
                            "reason": reason, "error": str(exc)})
            log.warning("advisor auto-execute failed", extra={"symbol": a.symbol, "error": str(exc)})
    return actions


def run_advisor(session: Session) -> dict:
    """Compute advisories now, optionally auto-execute, record to the audit log, stamp last_run."""
    advice, contexts = _advise_with_context(session)
    cfg = get_or_create_advisor_config(session)
    cfg.last_run_at = datetime.now(timezone.utc)

    actions = _auto_execute(session, advice, contexts) if cfg.auto_execute else []

    # After closing an invalidated trade, optionally re-analyse and open a fresh, properly-sized
    # setup in whatever direction the engine now supports (opt-in, separate toggle).
    if cfg.auto_execute and cfg.auto_reenter:
        for a in [x for x in list(actions) if x.get("action") == "close" and x.get("ok")]:
            actions.append(_maybe_reenter(session, a["symbol"], a.get("asset_class", "forex")))

    actionable = [a for a in advice if a.severity in ("warn", "danger")]
    for a in actionable:
        log.warning("position advisory", extra={"symbol": a.symbol, "severity": a.severity,
                                                 "thesis": a.thesis, "advice": a.headline})
    session.add(AgentRun(agent="advisor", event="check",
                         detail={"positions": len(advice), "auto_execute": cfg.auto_execute,
                                 "advisories": [a.model_dump(mode="json") for a in actionable],
                                 "actions": actions}))
    session.commit()
    return {"last_run_at": cfg.last_run_at.isoformat(),
            "advice": [a.model_dump(mode="json") for a in advice], "actions": actions}


def advisor_tick(session: Session) -> dict:
    """Scheduler entrypoint: respect the enabled flag + interval, then run the advisor."""
    cfg = get_or_create_advisor_config(session)
    if not cfg.enabled:
        return {"ran": False, "reason": "disabled"}

    now = datetime.now(timezone.utc)
    if cfg.last_run_at is not None:
        last = cfg.last_run_at if cfg.last_run_at.tzinfo else cfg.last_run_at.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cfg.interval_seconds:
            return {"ran": False, "reason": "interval not elapsed"}

    summary = run_advisor(session)
    return {"ran": True, **summary}
