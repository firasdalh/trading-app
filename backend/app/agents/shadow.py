"""SHADOW SCORECARD — prove whether the AI actually decides better than the deterministic engine.

Every AI decision is logged with BOTH the AI's call and the deterministic engine's call on the SAME
setup + entry price (``record_shadow``). Later, ``evaluate_shadows`` replays the following candles and
scores each side (win/loss/timeout in R; no_fill for un-triggered arms; missed-move for stand-asides).
``scorecard`` aggregates the head-to-head. Nothing here ever touches a live order — it's measurement.

The AI's own graded record is also fed back into the decision brief (see ai_decider._shadow_note), so
the AI learns from what actually happened rather than reasoning from a blank history.
"""
from __future__ import annotations

import bisect
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.db import ShadowDecision
from app.models.enums import Direction
from app.models.schemas import TradeProposal

log = get_logger("agents.shadow")

_HORIZON_BARS = 48   # grade a decision over the next N bars of its timeframe
_MISS_ATR = 2.0      # a stand-aside "missed" a move if price ran >= 2 ATR one way before the other


def _dir_from_order(order_type: str) -> str:
    return "long" if order_type.startswith("buy") else "short"


def record_shadow(session: Session, symbol: str, asset_class: str, timeframe: str, now: datetime,
                  price: float | None, atr: float | None, ai: TradeProposal,
                  det: TradeProposal) -> None:
    """Log one AI decision + the deterministic call on the same setup. Best-effort (never raises)."""
    try:
        if price is None:
            return
        # --- AI side ---
        if ai.conditional is not None:
            ai_action = f"arm_{_dir_from_order(ai.conditional.order_type)}"
            ai_dir = _dir_from_order(ai.conditional.order_type)
            ai_entry, ai_stop, ai_tgt = (ai.conditional.trigger_price, ai.conditional.stop_loss,
                                         ai.conditional.take_profit)
            ai_conf = ai.conditional.confidence
        elif ai.direction in (Direction.LONG, Direction.SHORT):
            ai_dir = ai.direction.value
            ai_action = f"open_{ai_dir}"
            ai_entry, ai_stop, ai_tgt, ai_conf = ai.entry, ai.stop_loss, ai.take_profit, ai.confidence
        else:
            ai_action, ai_dir = "stand_aside", None
            ai_entry = ai_stop = ai_tgt = ai_conf = None
        # first scenario label the AI chose (carried in the rationale) — best-effort short tag
        ai_scen = None
        if ai.rationale and "CHOSE '" in ai.rationale:
            ai_scen = ai.rationale.split("CHOSE '", 1)[1].split("'", 1)[0][:120]

        det_dir = det.direction.value
        row = ShadowDecision(
            created_at=now, symbol=symbol, asset_class=asset_class, timeframe=timeframe,
            price_at=price, atr_at=atr, regime=ai.regime or det.regime, horizon_bars=_HORIZON_BARS,
            ai_action=ai_action, ai_direction=ai_dir, ai_entry=ai_entry, ai_stop=ai_stop,
            ai_target=ai_tgt, ai_conf=ai_conf, ai_scenario=ai_scen,
            det_direction=det_dir,
            det_entry=det.entry, det_stop=det.stop_loss, det_target=det.take_profit,
            det_conf=det.confidence,
        )
        session.add(row)
        session.commit()
        log.info("shadow recorded", extra={"symbol": symbol, "ai": ai_action, "det": det_dir})
    except Exception as exc:  # noqa: BLE001 - logging must never break the pipeline
        log.warning("shadow record failed", extra={"symbol": symbol, "error": str(exc)})
        session.rollback()


def _grade_directional(direction: str, entry: float, stop: float, target: float,
                       bars: list, is_arm: bool, order_type: str | None) -> tuple[str, float]:
    """Replay ``bars`` (candles after the decision) and score one directional call.

    Returns (outcome, r_multiple). outcome in win|loss|timeout|no_fill|invalid."""
    risk = abs(entry - stop)
    if risk <= 0:
        return "invalid", 0.0
    is_long = direction == "long"
    seq = bars

    # ARM: first find the fill bar (price reaches the trigger), else no_fill.
    if is_arm and order_type:
        fill_i = None
        for i, c in enumerate(bars):
            up = c.high >= entry
            dn = c.low <= entry
            hit = (up if order_type in ("buy_stop", "sell_limit") else dn)  # stop=cross toward, limit=pullback
            if hit:
                fill_i = i
                break
        if fill_i is None:
            return "no_fill", 0.0
        seq = bars[fill_i:]

    for c in seq:
        if is_long:
            if c.low <= stop:                       # stop first (conservative if both in one bar)
                return "loss", -1.0
            if c.high >= target:
                return "win", (target - entry) / risk
        else:
            if c.high >= stop:
                return "loss", -1.0
            if c.low <= target:
                return "win", (entry - target) / risk
    # neither hit within the horizon -> mark to the last close
    last = seq[-1].close if seq else entry
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return "timeout", round(r, 2)


def _missed_move(price: float, atr: float | None, bars: list) -> str:
    """For a stand-aside: did price run >= _MISS_ATR ATR one way BEFORE the other? up|down|none."""
    if not atr or atr <= 0 or not bars:
        return "none"
    thr = _MISS_ATR * atr
    for c in bars:
        if c.high - price >= thr:
            return "up"
        if price - c.low >= thr:
            return "down"
    return "none"


def evaluate_shadows(session: Session, limit: int = 300) -> int:
    """Grade un-evaluated shadow rows whose horizon has enough forward candles. Returns #graded."""
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.data.ohlcv_cache import get_ohlcv_cached
    from app.models.enums import AssetClass

    settings = get_or_create_settings(session)
    rows = session.scalars(
        select(ShadowDecision).where(ShadowDecision.evaluated.is_(False))
        .order_by(ShadowDecision.created_at).limit(limit)
    ).all()
    graded = 0
    candle_cache: dict[tuple[str, str], list] = {}
    for r in rows:
        key = (r.symbol, r.timeframe)
        if key not in candle_cache:
            try:
                broker = get_broker_for(AssetClass(r.asset_class), settings.broker_map)
                candle_cache[key] = get_ohlcv_cached(broker, r.symbol, r.timeframe, 400).candles
            except Exception as exc:  # noqa: BLE001
                log.warning("shadow eval fetch failed", extra={"symbol": r.symbol, "error": str(exc)})
                candle_cache[key] = []
        candles = candle_cache[key]
        if not candles:
            continue
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        ts = [c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc) for c in candles]
        i = bisect.bisect_right(ts, created)          # first bar strictly after the decision
        after = candles[i:i + r.horizon_bars]
        # Not enough forward data AND the horizon hasn't fully elapsed yet -> leave pending.
        if len(after) < 3:
            continue
        terminal_possible = len(after) >= r.horizon_bars

        def _grade_side(direction, entry, stop, target, is_arm, order_type):
            if direction in (None, "no_trade") or entry is None or stop is None or target is None:
                mv = _missed_move(r.price_at, r.atr_at, after)
                return ("stand_aside", 0.0, mv)
            outcome, rr = _grade_directional(direction, entry, stop, target, after, is_arm, order_type)
            return (outcome, rr, None)

        is_arm_ai = r.ai_action.startswith("arm")
        ai_ot = None
        if is_arm_ai:  # reconstruct the order type from direction + trigger vs price
            ai_ot = ("buy_stop" if r.ai_entry >= r.price_at else "buy_limit") if r.ai_direction == "long" \
                else ("sell_stop" if r.ai_entry <= r.price_at else "sell_limit")
        ai_out, ai_r, ai_missed = _grade_side(r.ai_direction, r.ai_entry, r.ai_stop, r.ai_target,
                                              is_arm_ai, ai_ot)
        det_out, det_r, det_missed = _grade_side(r.det_direction, r.det_entry, r.det_stop, r.det_target,
                                                 False, None)

        # A side is "definitive" once it hit a win/loss, or a stand-aside already saw a clean 2-ATR move
        # (we know it was missed — no need to wait). Otherwise finalize only when the full horizon elapsed.
        def _definitive(outcome: str, missed: str | None) -> bool:
            return outcome in ("win", "loss") or (outcome == "stand_aside" and missed in ("up", "down"))

        if not (terminal_possible or _definitive(ai_out, ai_missed) or _definitive(det_out, det_missed)):
            continue
        r.ai_outcome, r.ai_r = ai_out, ai_r
        r.det_outcome, r.det_r = det_out, det_r
        r.missed_move = ai_missed if r.ai_action == "stand_aside" else (det_missed if det_out == "stand_aside" else None)
        r.evaluated = True
        r.evaluated_at = datetime.now(timezone.utc)
        graded += 1
    if graded:
        session.commit()
    log.info("shadow evaluated", extra={"graded": graded, "pending": len(rows) - graded})
    return graded


def _agg(rows: list[ShadowDecision], side: str) -> dict:
    """Aggregate one side (ai|det) over evaluated rows: directional win rate + expectancy in R."""
    out = {"decisions": len(rows), "directional": 0, "wins": 0, "losses": 0, "no_fill": 0,
           "stand_aside": 0, "win_rate": None, "avg_r": None, "expectancy_r": None,
           "stand_aside_missed": 0}
    rs = []
    for r in rows:
        outcome = getattr(r, f"{side}_outcome")
        rv = getattr(r, f"{side}_r")
        if outcome in ("win", "loss", "timeout"):
            out["directional"] += 1
            if outcome == "win":
                out["wins"] += 1
            elif outcome == "loss":
                out["losses"] += 1
            if rv is not None:
                rs.append(rv)
        elif outcome == "no_fill":
            out["no_fill"] += 1
        elif outcome == "stand_aside":
            out["stand_aside"] += 1
            if r.missed_move in ("up", "down"):
                out["stand_aside_missed"] += 1
    if out["directional"]:
        out["win_rate"] = round(out["wins"] / out["directional"], 3)
    if rs:
        out["avg_r"] = round(sum(rs) / len(rs), 3)
        out["expectancy_r"] = out["avg_r"]
    return out


def _since(session: Session):
    """The journal-reset marker: 'start fresh' resets the shadow scorecard too (rows before it are
    excluded), so a cleared journal gives a clean AI-vs-deterministic record."""
    from app.core.state import get_or_create_settings

    return get_or_create_settings(session).journal_reset_at


def _evaluated_rows(session: Session) -> list[ShadowDecision]:
    conds = [ShadowDecision.evaluated.is_(True)]
    since = _since(session)
    if since is not None:
        conds.append(ShadowDecision.created_at >= since)
    return list(session.scalars(select(ShadowDecision).where(*conds)).all())


def scorecard(session: Session) -> dict:
    """Head-to-head AI vs deterministic over EVALUATED shadow decisions since the journal reset."""
    rows = _evaluated_rows(session)
    pconds = [ShadowDecision.evaluated.is_(False)]
    since = _since(session)
    if since is not None:
        pconds.append(ShadowDecision.created_at >= since)
    pending = session.scalar(select(ShadowDecision.id).where(*pconds).limit(1))
    by_regime: dict[str, dict] = {}
    for reg in sorted({(r.regime or "unknown") for r in rows}):
        sub = [r for r in rows if (r.regime or "unknown") == reg]
        by_regime[reg] = {"ai": _agg(sub, "ai"), "deterministic": _agg(sub, "det")}
    return {
        "evaluated": len(rows),
        "pending": bool(pending),
        "ai": _agg(rows, "ai"),
        "deterministic": _agg(rows, "det"),
        "by_regime": by_regime,
    }


def shadow_note(session: Session) -> str | None:
    """One-line AI track record for the decision brief, or None when there's nothing graded yet."""
    a = _agg(_evaluated_rows(session), "ai")
    if not a["directional"] and not a["stand_aside"]:
        return None
    bits = []
    if a["directional"]:
        wr = f"{a['win_rate'] * 100:.0f}%" if a["win_rate"] is not None else "n/a"
        er = f"{a['expectancy_r']:+.2f}R" if a["expectancy_r"] is not None else "n/a"
        bits.append(f"{a['directional']} AI trades graded: {wr} win, {er} avg")
    if a["stand_aside"]:
        bits.append(f"{a['stand_aside']} stand-asides ({a['stand_aside_missed']} missed a 2-ATR move)")
    return "AI shadow record — " + "; ".join(bits) + "."
