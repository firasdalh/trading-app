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

from app.agents.llm import llm_available
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.data.ohlcv_cache import get_ohlcv_cached
from app.data.providers import get_calendar_provider
from app.models.db import AdvisorConfig, AgentRun, Position, TradeProposalRecord
from app.models.enums import PositionStatus
from app.models.schemas import PositionAdvice
from app.risk.service import _norm_symbol, live_broker_positions

log = get_logger("agents.advisor")

_IMMINENT_BEFORE_MIN = 90   # an event this soon is "imminent"
_IN_WINDOW_AFTER_MIN = 30   # still relevant up to 30 min after the release
_SEV_RANK = {"info": 0, "warn": 1, "danger": 2}

# --- precision: don't cry wolf on tiny moves ---
_MOM_ATR_FRAC = 0.10        # momentum counts as "against" only if |MACD hist| >= 10% of ATR
# The weakening->tighten action (2b) is gated tighter than the "weakening" LABEL: only auto-tighten a
# winner's stop when it's CLEARLY in profit AND the deterioration is REAL (meaningful counter-momentum
# or a change-of-character) — so a near-zero MACD blip on a barely-profitable trade no longer scratches
# it at breakeven. (The advice still SHOWS "weakening"; only the auto-action is more conservative.)
_WEAKEN_MIN_R = 0.5         # require >= +0.5R of profit before the weakening-tighten fires
_WEAKEN_MOM_ATR_FRAC = 0.25 # "meaningful" counter-momentum for the tighten: |MACD hist| >= 25% of ATR

# --- auto-manage thresholds (R = profit / planned risk) ---
_BREAKEVEN_R = 1.0          # at +1R, lock the stop to breakeven (trending/moderate regime)
_TRAIL_R = 1.5             # beyond +1.5R, trail the stop (trending/moderate regime)
_BREAKEVEN_R_FAST = 0.5     # volatile/ranging regime: bank sooner -> breakeven at +0.5R
_TRAIL_R_FAST = 1.0         # volatile/ranging regime: start trailing at +1R
_TRAIL_ATR_MULT = 1.0       # ATR trailing distance = 1 ATR behind price
_STRUCT_TRAIL_BUFFER_ATR = 0.2  # structural trail sits this far beyond the swing (wick allowance)
_PROTECT_ATR_MULT = 1.5     # protective stop for a naked position = 1.5 ATR from price

# --- partial profit-taking (scale out) ---
_PARTIAL_R = 1.5            # advice-text milestone + the FIRST-third laddered book (BE already at +1R)
_PARTIAL_FRACTION = 0.5     # fallback: take half off (a position with no planned size on record)
# LADDERED tranching (a pro pyramids OUT): book ~a third at +1.5R, another third at +3R, trail the
# runner. A tranche can also be pulled EARLY when price banks into a wall with fading momentum (below).
_LADDER_R1 = 1.5           # book the first third here
_LADDER_R2 = 3.0           # book the second third here
_LADDER_FRACTION = 1.0 / 3.0  # each tranche ~ a third of the ORIGINAL size
# BANK INTO A WALL: sell into strength — take the next tranche early when a real winner (>= this R)
# reaches a strong opposing S/R level and momentum is fading into it.
_WALL_MIN_R = 1.0
_WALL_NEAR_ATR = 0.6        # price within this many ATR of the opposing level = "at the wall"
_RSI_OB_EXIT = 70.0         # RSI overbought (long) rolling back = fading into the wall
_RSI_OS_EXIT = 30.0         # RSI oversold (short) rolling back = fading into the wall
_RUN_R = 1.8               # near the ~2R target in a strong intact trend, let the winner RUN:
#                            drop the fixed take-profit and ride a trailing stop instead of capping
# TIME-STOP: only close a stagnant trade that is still roughly FLAT (a winner is managed by the trail,
# a loser by its stop). "Flat" = the trade hasn't moved more than this many R either way.
_TIME_STOP_FLAT_R = 0.5
_PARTIAL_DONE: set[str] = set()  # symbols already scaled this position (reset when it goes flat)

# --- hysteresis + cooldown so auto-execute doesn't thrash ---
_CLOSE_CONFIRM = 2          # require N consecutive "invalidated" reads before auto-closing
_ACTION_COOLDOWN_S = 600    # min seconds between auto-CLOSE actions on the same symbol
_INVALID_STREAK: dict[str, int] = {}
_LAST_CLOSE_AT: dict[str, datetime] = {}


def _reset_auto_state() -> None:
    """Clear hysteresis/cooldown trackers (used by tests; also safe to call on restart)."""
    _INVALID_STREAK.clear()
    _LAST_CLOSE_AT.clear()
    _PARTIAL_DONE.clear()


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


def _position_opened_at(session: Session, symbol: str, direction: str | None = None) -> datetime | None:
    """When the app-tracked OPEN position for this symbol(+direction) was opened — for the time-stop.
    Returns None for a trade opened directly in the terminal (no app row to measure the hold from)."""
    from app.models.enums import PositionStatus
    from app.risk.service import _norm_symbol

    norm = _norm_symbol(symbol)
    rows = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value)
        .order_by(Position.opened_at.desc())
    ).all()
    for r in rows:
        if _norm_symbol(r.symbol) == norm and (direction is None or r.direction == direction):
            return _aware(r.opened_at) if r.opened_at else None
    return None


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
                series.append(get_ohlcv_cached(broker, p.symbol, t, limit=200))
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
        # Nearest opposing S/R across ALL timeframes (for the "bank into a wall" exit): the nearest
        # resistance ABOVE for a long / support BELOW for a short, tagged with the TF it came from.
        px = ind.get("last_close")
        near_res = near_sup = None
        if px:
            res_above = [(r, t.timeframe) for t in tech.timeframes for r in (t.resistance_levels or []) if r and r > px]
            sup_below = [(s, t.timeframe) for t in tech.timeframes for s in (t.support_levels or []) if s and s < px]
            near_res = min(res_above, key=lambda x: x[0]) if res_above else None
            near_sup = max(sup_below, key=lambda x: x[0]) if sup_below else None
        # Carry the same chart-reading signals the entry engine uses, so EXIT management is as
        # senior-trader as entry: market structure, change-of-character, the latest swing levels
        # (for structural trailing), the regime, RSI (for exhaustion), and the nearest wall.
        return {"tf": tf, "trend": _trend_from_indicators(ind, prim.trend),
                "macro": _macro_trend(tech), "macro_tf": macro_tf,
                "macd_hist": ind.get("macd_hist"), "atr": ind.get("atr14"),
                "last": ind.get("last_close"),
                "rsi": ind.get("rsi14"), "rsi_prev": ind.get("rsi14_prev"),
                "regime": _regime(ind), "structure": _structure_label(ind),
                "choch": bool(ind.get("choch")),
                "swing_high": ind.get("swing_high"), "swing_low": ind.get("swing_low"),
                "near_res": near_res, "near_sup": near_sup,
                "_tech": tech}   # the full read, so the AI thesis re-check can reuse it (no refetch)
    except Exception:  # noqa: BLE001 - the advisor must never crash the scan/endpoint
        return None


def _thesis_from_context(p, ctx: dict) -> dict:
    """Classify the plan vs. the current read: intact / weakening / invalidated. A trend FLIP
    (EMA structure) invalidates; a flattening trend or *meaningful* counter-momentum weakens;
    trivial counter-momentum (noise) is ignored."""
    tf, trend = ctx["tf"], ctx["trend"]
    macd_hist, atr = ctx.get("macd_hist"), ctx.get("atr")
    macro, macro_tf = ctx.get("macro"), ctx.get("macro_tf", tf)
    side = "buy" if p.direction == "long" else "sell"
    want = "up" if p.direction == "long" else "down"
    opp = "down" if p.direction == "long" else "up"

    if trend == opp:
        # Only call it INVALIDATED when the higher timeframe confirms the flip. An entry-TF flip
        # while the higher TF still supports the trade is usually a pullback, not a breakdown —
        # treat it as weakening so we don't auto-exit good trades on noise (the crypto lesson).
        if macro == opp:
            return {"label": "invalidated",
                    "note": (f"Why: the trend has turned against your {side} on both the {tf} chart and "
                             f"the bigger {macro_tf} chart. The reason you opened this trade is gone — "
                             "it's usually better to take the exit than to hope it comes back.")}
        return {"label": "weakening",
                "note": (f"Why: the short-term {tf} chart turned against your {side}, but the bigger "
                         f"{macro_tf} chart is still on your side — so this looks like a normal pullback, "
                         "not a real reversal. Tighten your stop a little, but don't panic-exit.")}

    # Change-of-character: the structure we're riding just cracked (price broke back through the
    # last swing). It's the EARLIEST reversal warning — before the trend even flips — so a pro
    # tightens / takes profit here rather than waiting. Treat as weakening.
    if ctx.get("choch") and ctx.get("structure") == want:
        sign = ("price just dipped below its last higher low (the last small dip up)"
                if p.direction == "long" else
                "price just pushed above its last lower high (the last small bounce down)")
        return {"label": "weakening",
                "note": (f"Why: an early warning sign on the {tf} chart — {sign}, which often comes "
                         "just before a turn. The move is losing its footing. Consider tightening your "
                         "stop or banking some profit.")}

    against = macd_hist is not None and (
        (p.direction == "long" and macd_hist < 0) or (p.direction == "short" and macd_hist > 0)
    )
    meaningful = against and atr and abs(macd_hist) >= _MOM_ATR_FRAC * atr
    if trend != want:  # sideways: trend no longer aligned with the position
        return {"label": "weakening",
                "note": (f"Why: the {tf} trend has gone flat (no clear direction) — the move you're "
                         "trading is stalling. Consider tightening your stop or trimming.")}
    if meaningful:
        return {"label": "weakening",
                "note": (f"Why: momentum is fading on the {tf} chart — the push behind your trade is "
                         "running out of steam. Consider tightening your stop or trimming.")}
    return {"label": "intact",
            "note": f"Good: the {tf} trend is still going your way. Nothing to fix — let it work."}


def _position_thesis(session: Session, p, ctx: dict | None = None) -> dict | None:
    """Thesis re-check; reuses a precomputed context when given (avoids a second broker fetch)."""
    if ctx is None:
        ctx = _position_context(session, p)
    return _thesis_from_context(p, ctx) if ctx else None


def _ai_thesis_review(session: Session, p, ctx: dict) -> tuple[bool, str] | None:
    """Ask the AI whether the ORIGINAL thesis that opened this position is still valid, so exit
    management follows the AI's read — not just deterministic momentum. Reuses the same confirm/veto
    reviewer the armed setups use. Returns ``(still_valid, reason)`` or ``None`` when the AI decider is
    off / the LLM is unavailable (then the deterministic thesis stands alone). Best-effort, never raises."""
    try:
        if not get_or_create_settings(session).ai_review_enabled or not llm_available():
            return None
        tech = ctx.get("_tech")
        if tech is None:
            return None
        from app.agents.orchestrator import review_armed_setup
        from app.models.enums import AssetClass, Direction
        from app.models.schemas import TradeProposal

        proposal = TradeProposal(
            symbol=p.symbol, asset_class=AssetClass(p.asset_class), timeframe=ctx.get("tf", "1h"),
            direction=Direction(p.direction), entry=p.entry_price, stop_loss=p.stop_loss,
            take_profit=p.take_profit, confidence=0.6,
            rationale=f"Holding an OPEN {p.direction} from {p.entry_price}; judge whether the thesis still holds.",
        )
        return review_armed_setup(proposal, tech, use_llm=True)
    except Exception:  # noqa: BLE001 - the advisor must never crash on the AI re-check
        return None


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
    """Event-proximity + protection advice, in plain language (before folding in the thesis note)."""
    side = "buy" if p.direction == "long" else "sell"
    lock = "up" if p.direction == "long" else "down"
    if ev_label is not None:
        when_txt = f"in about {ev_mins} min" if (ev_mins or 0) > 0 else "any moment now"
        if not has_stop:
            return ("danger", f"{p.symbol} — no stop set, and big news is coming",
                    f"High-impact news ({ev_label}) is due {when_txt}, and this trade has NO stop-loss. "
                    "Set a stop right now, or close the trade — holding through news with no protection "
                    "can cost a lot very fast.")
        if winning:
            return ("warn", f"{p.symbol} — winning, but big news is coming",
                    f"You're in profit and high-impact news ({ev_label}) is due {when_txt}. Consider "
                    f"locking it in: move your stop {lock} to your entry price ({p.entry_price}) so the "
                    "worst case is no loss, or take some/all of the profit. News can flip a winner in seconds.")
        return ("warn", f"{p.symbol} — losing, and big news is coming",
                f"You're down and high-impact news ({ev_label}) is due {when_txt}. A news spike could "
                "deepen the loss — consider closing or trimming now, or at least keep a tight stop.")
    if not has_stop:
        return ("warn", f"{p.symbol} — no stop set",
                "This trade has no stop-loss, so a sharp move could cost a lot. Set a stop to cap your risk.")
    if winning:
        return ("info", f"{p.symbol} — in profit, nothing urgent",
                f"You're in profit and no major news is due soon. You can move your stop {lock} to lock "
                "in some gain, or simply hold and let it work toward your target.")
    return ("info", f"{p.symbol} — open, nothing urgent",
            "No major news is due and your stop is set. Nothing to do — let your stop and target do "
            f"their job on this {side}.")


def _scenario_awareness(p, ctx: dict | None) -> tuple[str, str] | None:
    """Judge the open position against the DETERMINISTIC engine's OWN read — the plan it decided on —
    NOT an AI scenario. The deterministic engine is the decider, so the advisor checks the position
    against that engine's bigger-picture (macro / higher-timeframe) trend: is the trade still following
    the plan (bigger trend still with it -> a pullback here is expected, don't panic), or has the read
    turned against it? Reuses the already-computed ``ctx`` (no refetch, no LLM). Returns (note, lean)
    with lean in {'with','against','neutral'}, or None when there's no context."""
    if not ctx:
        return None
    macro = ctx.get("macro")
    macro_tf = ctx.get("macro_tf") or ctx.get("tf") or "higher-timeframe"
    long_ = p.direction == "long"
    inval = ctx.get("swing_low") if long_ else ctx.get("swing_high")   # the swing the trade is riding
    if (macro == "up" and long_) or (macro == "down" and not long_):
        lean, lean_txt = "with", (f"on plan — the deterministic read on the bigger {macro_tf} chart still "
                                  f"favours your {p.direction}, so a pullback here is expected, not a failure.")
    elif (macro == "down" and long_) or (macro == "up" and not long_):
        lean, lean_txt = "against", (f"heads-up — the bigger {macro_tf} chart's deterministic trend now "
                                     f"leans AGAINST your {p.direction}.")
    else:
        lean, lean_txt = "neutral", f"the bigger {macro_tf} chart reads mixed here."
    inval_txt = (f" Structure to watch: the {'swing low' if long_ else 'swing high'} at {round(inval, 6)}."
                 if inval else "")
    note = f"Plan check ({macro_tf} trend {macro or 'flat'}): {lean_txt}{inval_txt}"
    return note, lean


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

    # Match each live position to OUR record (by symbol+direction) for its open-time + source. A trade
    # opened directly in the terminal won't match, so opened_at/source stay None (shown as blank).
    db_open: dict = {}
    if session is not None:
        db_open = {
            (_norm_symbol(pos.symbol), pos.direction): pos
            for pos in session.scalars(
                select(Position).where(Position.status == PositionStatus.OPEN.value)).all()
        }

    for p in live_broker_positions(session):
        dbp = db_open.get((_norm_symbol(p.symbol), p.direction))
        try:
            events = cal.get_events(p.symbol, lookahead_hours=12, include_medium=True)
        except Exception:  # noqa: BLE001
            events = []

        ev_label: str | None = None
        ev_mins: int | None = None
        soft: list[tuple[int, str]] = []
        for e in events:
            mins = int((_aware(e.when) - now).total_seconds() / 60)
            if str(e.importance).lower() == "high":
                if -_IN_WINDOW_AFTER_MIN <= mins <= _IMMINENT_BEFORE_MIN:
                    if ev_mins is None or mins < ev_mins:
                        ev_label, ev_mins = e.label, mins
            elif str(e.importance).lower() == "medium" and 0 <= mins <= 480:
                # SOFT heads-up only (e.g. a Fed speech) — never gates; high-impact drives the hard
                # event warning above.
                soft.append((mins, e.label))
        soft.sort(key=lambda x: x[0])
        events_soon = " · ".join(
            f"{lbl} {('~%dm' % m) if m < 90 else ('~%.1fh' % (m / 60))}" for m, lbl in soft[:3]
        ) or None

        winning = p.unrealized_pnl > 0
        has_stop = p.stop_loss is not None and p.stop_loss != 0

        severity, headline, detail = _base_advice(p, ev_label, ev_mins, winning, has_stop)

        ctx = _position_context(session, p)
        if ctx:
            contexts[p.symbol] = ctx
        thesis = _position_thesis(session, p, ctx)
        # AI thesis re-check: follow the AI's read on whether the ORIGINAL decision still holds. A veto
        # ESCALATES the thesis (at most to 'weakening' -> tighten the stop), so a single AI opinion can't
        # market-close a trade on its own — a hard exit still needs the deterministic invalidation (both
        # timeframes flipped) + the consecutive-read confirm gate. Only runs when the AI decider is on.
        if thesis is not None and ctx:
            ai_v = _ai_thesis_review(session, p, ctx)
            if ai_v is not None:
                ok, reason = ai_v
                if not ok and thesis["label"] == "intact":
                    thesis = {"label": "weakening",
                              "note": ("Why: the AI re-checked this trade and thinks the reason you opened "
                                       f"it is fading — {reason} Consider tightening your stop or trimming.")}
                elif not ok and thesis["label"] == "weakening":
                    thesis = {**thesis, "note": thesis["note"] + f" The AI agrees the thesis is slipping — {reason}"}
                elif ok and thesis["label"] == "intact":
                    thesis = {**thesis, "note": thesis["note"] + " The AI re-check still backs this trade."}
        # PLAN awareness: fold in the DETERMINISTIC engine's own read (the plan it decided on) — are we
        # following the plan, is a pullback expected (don't panic), or has the bigger-picture read turned
        # against the position? No AI. It can ESCALATE at most to 'weakening' (tighten the stop) — never
        # auto-close.
        scen_note: str | None = None
        scen = _scenario_awareness(p, ctx)
        if scen is not None:
            scen_note, lean = scen
            if lean == "against" and thesis is not None and thesis["label"] == "intact":
                thesis = {"label": "weakening",
                          "note": ("Why: the bigger-picture deterministic read now leans against this "
                                   "trade — consider tightening your stop or trimming.")}
        r_mult = _r_multiple(session, p, ctx)
        thesis_label = thesis["label"] if thesis else "unknown"
        # Scale-out suggestion (Mode A): a pro pyramids OUT — bank a third at +1.5R, another third near
        # +3R (or earlier into a wall), and trail the runner. (The auto-advisor does this when enabled.)
        if (r_mult is not None and r_mult >= _LADDER_R1
                and not _already_scaled(session, p.symbol, p.qty, p.direction)):
            detail = (f"{detail} You're past +{r_mult:.1f}x your risk — a pro would bank about a third "
                      f"now (and another third near +{_LADDER_R2:.0f}R, or earlier if price stalls at a "
                      "level), trailing the runner to let it work.")
            if severity == "info":
                severity = "warn"
        if thesis is not None:
            detail = f"{detail} {thesis['note']}"
            if r_mult is not None:
                word = "up" if r_mult >= 0 else "down"
                detail = (f"{detail} (You're {word} about {abs(r_mult):.1f}x the amount you risked on "
                          f"this trade — {r_mult:+.1f}R.)")
            # The thesis can escalate urgency. News keeps its headline (it's the nearer concern);
            # otherwise the thesis drives the headline too.
            if thesis_label == "invalidated":
                severity = "danger"
                if ev_label is None:
                    headline = f"{p.symbol} — trend flipped against you, consider exiting"
            elif thesis_label == "weakening" and _SEV_RANK[severity] < _SEV_RANK["warn"]:
                severity = "warn"
                if ev_label is None:
                    headline = f"{p.symbol} — losing momentum"

        if scen_note:
            detail = f"{detail} {scen_note}"

        out.append(PositionAdvice(
            symbol=p.symbol, direction=p.direction, unrealized_pnl=round(p.unrealized_pnl, 2),
            has_stop=has_stop, severity=severity, headline=headline, detail=detail,
            opened_at=(dbp.opened_at if dbp else None), source=(dbp.source if dbp else None),
            thesis=thesis_label, r_multiple=r_mult, event_label=ev_label, minutes_to_event=ev_mins,
            events_soon=events_soon,
        ))
    return out, contexts


# --------------------------------------------------------------------------- #
#  Auto-watch config + scheduled tick
# --------------------------------------------------------------------------- #


def get_or_create_advisor_config(session: Session) -> AdvisorConfig:
    cfg = session.get(AdvisorConfig, 1)
    if cfg is None:
        cfg = AdvisorConfig(id=1, enabled=False, auto_execute=False, interval_seconds=300)
        session.add(cfg)
        session.commit()
    return cfg


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


# Stop-modify rejections that are BENIGN + temporary: the existing broker-side stop still protects
# the trade and the advisor will retry, so they're recorded as "deferred", not failures.
_DEFERRABLE = ("market closed", "no changes", "off quotes", "no prices",
               "trading disabled", "autotrading disabled")


def _is_deferrable(err: str | None) -> bool:
    e = (err or "").lower()
    return any(k in e for k in _DEFERRABLE)


def _already_scaled(session: Session, symbol: str, qty: float | None, direction: str | None) -> bool:
    """Has this position already been partially closed (scaled out)?

    Prefer DERIVING it from the live remaining size vs the planned size, so it stays correct
    across app restarts and symbol re-entries — unlike the in-memory ``_PARTIAL_DONE`` set, which
    is wiped on restart and keyed only by symbol. The set is kept as a same-process fast path and
    as the fallback for a position with no plan on record (e.g. opened directly in the terminal),
    where the planned size is unknown.
    """
    if symbol in _PARTIAL_DONE:
        return True
    try:
        row = _plan_proposal(session, symbol, direction)
        if row and row.approved_qty and qty is not None:
            # A 50% scale leaves ~half the original size; treat as scaled once the remaining
            # volume is materially below plan (tolerant of lot-step rounding on the remainder).
            return qty < row.approved_qty * (1 - _PARTIAL_FRACTION / 2)
    except Exception:  # noqa: BLE001
        pass
    return False


def _scaled_tranche(session: Session, symbol: str, qty: float | None, direction: str | None) -> tuple[int, bool]:
    """How many laddered tranches have already been booked (0/1/2) + whether a planned size is on
    record. DERIVED from the live remaining size vs plan so it survives restarts and re-entries:
    ~full = 0 booked, ~2/3 left = 1 booked, ~1/3 left = 2 booked (done). With no plan on record (a
    terminal-opened position) it can't ladder — returns (0/2, False) and the caller falls back to a
    single 50% partial gated by the in-memory ``_PARTIAL_DONE`` flag."""
    if symbol in _PARTIAL_DONE:
        return 2, False              # already scaled (single-partial fallback) / min-lot: no more
    try:
        row = _plan_proposal(session, symbol, direction)
        if not row or not row.approved_qty or qty is None:
            return 0, False
        frac = qty / row.approved_qty
        if frac <= 0.55:
            return 2, True           # ~a third left -> both tranches booked
        if frac <= 0.90:
            return 1, True           # ~two thirds left -> one tranche booked
        return 0, True               # ~full size -> none booked yet
    except Exception:  # noqa: BLE001
        return 0, False


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


def _structure_stop(direction: str, ctx: dict, atr: float | None) -> float | None:
    """The protective stop that sits just BEYOND the last swing (structure) — where the trend would
    actually break — instead of at your (arbitrary) entry price. The market moves by structure, not
    by where you happened to get in, so a normal pullback to entry no longer scratches the trade at
    0. Returns None when no swing / ATR is available (caller then falls back to breakeven)."""
    swing = (ctx or {}).get("swing_low") if direction == "long" else (ctx or {}).get("swing_high")
    if swing is None or not atr:
        return None
    buf = _STRUCT_TRAIL_BUFFER_ATR * atr
    return (swing - buf) if direction == "long" else (swing + buf)


def _auto_decision(a: PositionAdvice, p, ctx: dict, plan_risk: float | None,
                   tranche: int = 0, has_plan: bool = True,
                   opened_at: datetime | None = None, max_hold_hours: float = 0.0,
                   scale_out: bool = True) -> dict | None:
    """The bounded, deterministic set of actions the advisor may take autonomously — only
    highest-confidence, RISK-REDUCING moves: close an invalidated trade, attach a protective
    stop, LADDER out partial profit (a third at +1.5R, a third at +3R, or early into a wall), lock
    to breakeven, or trail. It never opens, sizes up, flips, or loosens a stop.

    Regime-aware: a clean trend gets room (trail behind structure, normal R thresholds); a
    volatile/ranging tape banks sooner. ``tranche`` = tranches already booked (0/1/2); ``has_plan``
    = a planned size is on record (so we can ladder; else a single 50% fallback)."""
    if a.thesis == "invalidated":
        return {"action": "close", "kind": "close",
                "reason": "thesis invalidated — the trend has flipped against the position"}

    atr = (ctx or {}).get("atr")
    last = (ctx or {}).get("last") or p.last_price
    d = p.direction
    regime = (ctx or {}).get("regime") or "moderate"

    # Time-stop: a trade held past the max hold that's STILL roughly flat (neither target nor stop has
    # resolved it) is dead money tying up the exposure slot — close it. Only when we know the open time
    # AND a risk reference AND price; a winner (past +flat R) rides the trail, a loser its stop.
    if max_hold_hours and max_hold_hours > 0 and opened_at is not None and plan_risk and last:
        held_h = (datetime.now(timezone.utc) - _aware(opened_at)).total_seconds() / 3600.0
        if held_h >= max_hold_hours:
            profit = (last - p.entry_price) if d == "long" else (p.entry_price - last)
            r = profit / plan_risk
            if abs(r) < _TIME_STOP_FLAT_R:
                return {"action": "close", "kind": "time_stop",
                        "reason": (f"held {held_h:.0f}h and still flat ({r:+.1f}R) — closing the "
                                   "stagnant trade to free the slot")}

    # 0) LADDERED scale-out — book ~a third at its R milestone, OR early when price banks into a strong
    # opposing level with fading momentum (sell into strength), whichever comes first. Done before the
    # trail/breakeven below (take money off the table first); the executor then de-risks the runner.
    # ``scale_out=False`` hands the WHOLE position to the trailing stop instead. Banking a third
    # at +1.5R and another at +3R caps exactly the outlier trades the book depends on: only 3%
    # of closed trades ever passed +3R, and those 9 produced +78.6R while the other 97% lost
    # -109.1R. The ladder trades that tail away for a smoother equity curve.
    if scale_out and plan_risk and last and atr and tranche < 2:
        profit = (last - p.entry_price) if d == "long" else (p.entry_price - last)
        r = profit / plan_risk
        r_trigger = _LADDER_R1 if tranche == 0 else _LADDER_R2
        # "At a wall, momentum fading" — the pro's early bank.
        wall = (ctx or {}).get("near_res") if d == "long" else (ctx or {}).get("near_sup")
        wall_note = ""
        at_wall = False
        if wall is not None and r >= _WALL_MIN_R:
            wall_px, wall_tf = wall
            if abs(wall_px - last) <= _WALL_NEAR_ATR * atr:
                mh, rsi, rsi_prev = (ctx or {}).get("macd_hist"), (ctx or {}).get("rsi"), (ctx or {}).get("rsi_prev")
                mom_fading = (mh is not None and ((d == "long" and mh <= 0) or (d == "short" and mh >= 0))) or (
                    rsi is not None and rsi_prev is not None and (
                        (d == "long" and rsi >= _RSI_OB_EXIT and rsi < rsi_prev)
                        or (d == "short" and rsi <= _RSI_OS_EXIT and rsi > rsi_prev)))
                if mom_fading:
                    at_wall = True
                    kind_txt = "resistance" if d == "long" else "support"
                    rsi_txt = f", RSI {rsi:.0f} rolling over" if rsi is not None else ", momentum fading"
                    wall_note = f" into {wall_tf.upper()} {kind_txt} {round(wall_px, 5)}{rsi_txt}"
        if r >= r_trigger or at_wall:
            # Fraction of the CURRENT position that removes ~a third of the ORIGINAL (or a single 50%
            # when there's no plan to ladder from).
            if has_plan:
                frac_open = max(_LADDER_FRACTION, 1.0 - tranche * _LADDER_FRACTION)
                frac = min(0.9, _LADDER_FRACTION / frac_open)
            else:
                frac = _PARTIAL_FRACTION
            nth = "first" if tranche == 0 else "second"
            why = (f"+{r:.1f}R — banking the {nth} third early, selling into strength{wall_note}"
                   if at_wall else
                   f"+{r:.1f}R — booking the {nth} third and de-risking the rest to breakeven")
            return {"action": "take_partial", "kind": "partial", "fraction": round(frac, 3),
                    "has_plan": has_plan, "reason": why}

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
        # LET WINNERS RUN: near the planned target in a strong, intact trend, drop the fixed
        # take-profit and ride a trailing stop — so a real trend isn't capped at ~2R. Risk-neutral:
        # the stop is moved to breakeven-or-better and never loosened. Fires once (TP then gone).
        # With the ladder off, the fixed take-profit is the ONLY remaining cap — so lift it as
        # soon as the trade is meaningfully in profit rather than waiting for ~2R, otherwise
        # turning the ladder off just swaps one ceiling for another.
        run_r = _RUN_R if scale_out else _BREAKEVEN_R
        if r >= run_r and regime == "trending" and a.thesis == "intact" and p.take_profit is not None:
            trail, basis = _trail_stop(d, last, atr, ctx or {}, regime)
            stop = trail if _tightens(d, p.stop_loss, trail) else (p.stop_loss if p.stop_loss is not None else trail)
            return {"action": "run_target", "kind": "run", "stop": round(stop, 5),
                    "reason": (f"+{r:.1f}R in a strong trend — letting it run: removing the target "
                               f"and trailing the stop ({basis})")}
        if r >= trail_r:
            trail, basis = _trail_stop(d, last, atr, ctx or {}, regime)
            if _tightens(d, p.stop_loss, trail):
                return {"action": "set_stop", "kind": "trail", "stop": round(trail, 5),
                        "reason": f"+{r:.1f}R ({regime}) — trailing the stop ({basis}) to lock gains"}
        if r >= be_r:
            # Protect behind the last swing (STRUCTURE), not at your entry price — the market moves
            # by structure, so a normal pullback to entry no longer scratches the trade at 0. Only
            # applied if it TIGHTENS (never loosens). Falls back to a plain breakeven at entry when
            # no swing is available, so a winner is never left unprotected.
            struct = _structure_stop(d, ctx or {}, atr)
            if struct is not None and _tightens(d, p.stop_loss, struct):
                return {"action": "set_stop", "kind": "structure", "stop": round(struct, 5),
                        "reason": (f"+{r:.1f}R — protecting behind the last swing (structure), "
                                   "not your entry")}
            if _stop_worse_than_entry(p):
                return {"action": "set_stop", "kind": "breakeven", "stop": round(p.entry_price, 5),
                        "reason": f"reached +{r:.1f}R — no swing to use; moving the stop to breakeven"}

    # 2b) Thesis weakening while CLEARLY in profit (>= +0.5R) AND with a REAL deterioration signal
    # (meaningful counter-momentum or a change-of-character) -> tighten the stop to lock gains. Gated
    # this way so a tiny near-zero MACD blip on a barely-profitable trade no longer scratches it at
    # breakeven (the USDCHF case); only ever risk-reducing.
    if a.thesis == "weakening" and atr and last and plan_risk:
        profit = (last - p.entry_price) if d == "long" else (p.entry_price - last)
        r = profit / plan_risk
        macd_hist = (ctx or {}).get("macd_hist")
        mom_against = macd_hist is not None and (
            (d == "long" and macd_hist < 0) or (d == "short" and macd_hist > 0)
        ) and abs(macd_hist) >= _WEAKEN_MOM_ATR_FRAC * atr
        real_deterioration = mom_against or bool((ctx or {}).get("choch"))
        if r >= _WEAKEN_MIN_R and real_deterioration:
            tight = (last - _TRAIL_ATR_MULT * atr) if d == "long" else (last + _TRAIL_ATR_MULT * atr)
            if _tightens(d, p.stop_loss, tight):
                why = "meaningful momentum against" if mom_against else "change-of-character"
                return {"action": "set_stop", "kind": "tighten", "stop": round(tight, 5),
                        "reason": (f"+{r:.1f}R and thesis weakening ({why}) — tightening the stop "
                                   "to lock gains")}

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


def _clear_db_take_profit(session: Session, symbol: str) -> None:
    """Remove the take-profit on the app-tracked open position(s) for this symbol, so the Monitor
    no longer auto-closes at the old target once the advisor has switched the trade to a trailing
    exit ('let winners run'). The broker-side TP is cleared separately in the executor."""
    from app.models.db import Position
    from app.models.enums import PositionStatus

    rows = session.scalars(
        select(Position).where(Position.symbol == symbol,
                               Position.status == PositionStatus.OPEN.value)
    ).all()
    for r in rows:
        r.take_profit = None
        session.add(r)


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
    _adv_cfg = get_or_create_advisor_config(session)
    max_hold_hours = _adv_cfg.max_hold_hours or 0.0
    scale_out = getattr(_adv_cfg, "scale_out_enabled", True)
    positions = {p.symbol: p for p in live_broker_positions(session)}
    contexts = contexts or {}
    # A symbol that has gone flat can scale out again next time it's entered.
    for sym in list(_PARTIAL_DONE):
        if sym not in positions:
            _PARTIAL_DONE.discard(sym)

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
        tranche, has_plan = _scaled_tranche(session, p.symbol, p.qty, p.direction)
        opened_at = _position_opened_at(session, p.symbol, p.direction)
        decision = _auto_decision(a, p, ctx or {}, plan_risk, tranche=tranche, has_plan=has_plan,
                                  opened_at=opened_at, max_hold_hours=max_hold_hours,
                                  scale_out=scale_out)
        if decision is None:
            continue
        action, kind, reason = decision["action"], decision["kind"], decision["reason"]

        if action == "close":
            # A time-stop is a slow, definite condition (held N hours + flat), so it doesn't need the
            # multi-read invalidation hysteresis — only the invalidation close does. Both respect the
            # per-symbol close cooldown so we never double-fire.
            if kind != "time_stop" and _INVALID_STREAK.get(a.symbol, 0) < _CLOSE_CONFIRM:
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
            elif action == "take_partial":
                result = broker.close_partial(p.symbol, decision.get("fraction", _PARTIAL_FRACTION))
                ok = result.status.value not in ("error", "rejected")
                if ok:
                    if not decision.get("has_plan"):
                        _PARTIAL_DONE.add(a.symbol)   # single-partial fallback (no plan) -> don't re-scale
                    # De-risk the runner: trail behind the last swing (STRUCTURE) when we can — the
                    # market respects structure, not your entry — else fall back to breakeven. Only
                    # if it TIGHTENS: a stop already trailed past entry (locked in profit) is left
                    # alone rather than loosened back.
                    struct = _structure_stop(p.direction, ctx or {}, (ctx or {}).get("atr"))
                    be = struct if (struct is not None and _tightens(p.direction, p.stop_loss, struct)) \
                        else round(p.entry_price, 5)
                    if _tightens(p.direction, p.stop_loss, be):
                        try:
                            be_res = broker.set_sl_tp(p.symbol, be, p.take_profit)
                            if be_res.status.value in ("error", "rejected"):
                                log.warning("advisor: breakeven move after partial rejected",
                                            extra={"symbol": a.symbol, "error": be_res.error})
                        except Exception as exc:  # noqa: BLE001
                            log.warning("advisor: breakeven move after partial failed",
                                        extra={"symbol": a.symbol, "error": str(exc)})
                elif "too small" in (result.error or "").lower() or "min lot" in (result.error or "").lower():
                    # Position is at the broker minimum lot — it can't be split. Stop retrying the
                    # partial every tick (which just spams failed actions); mark it so the next pass
                    # manages the WHOLE position via breakeven/trail instead. Re-checked when flat.
                    _PARTIAL_DONE.add(a.symbol)
                    action, kind = "partial_skipped", "partial"
                    reason = "position at min lot — can't scale; managing the whole position instead"
                    ok = True  # not a failure: a partial just isn't applicable to a min-lot position
            elif action == "run_target":
                # Let the winner run: trail the stop AND clear the take-profit (0.0 clears it on
                # MT5) so neither the broker nor the Monitor caps the trade at the planned target.
                # The trailing stop — broker-side, enforced between ticks — becomes the exit.
                result = broker.set_sl_tp(p.symbol, decision["stop"], 0.0)
                ok = result.status.value not in ("error", "rejected")
                if ok:
                    _clear_db_take_profit(session, p.symbol)  # so the Monitor won't close at the old TP
                elif _is_deferrable(result.error):
                    action, kind = "stop_deferred", "deferred"
                    reason = f"deferred — {result.error} (target/stop unchanged; will retry)"
            else:  # set_stop (protect | breakeven | trail)
                result = broker.set_sl_tp(p.symbol, decision["stop"], p.take_profit)
                ok = result.status.value not in ("error", "rejected")
                if not ok and _is_deferrable(result.error):
                    # Benign + temporary: the market is closed, or the stop is already where we
                    # want it. The ORIGINAL broker-side stop still protects the trade, and the
                    # advisor will retry and apply the move once the market reopens. Surface it as
                    # a calm "deferred", not a red ✗, so a real ✗ always means a real problem.
                    action, kind = "stop_deferred", "deferred"
                    reason = f"deferred — {result.error} (original stop still protects; will retry)"
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
