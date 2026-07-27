"""Per-pair auto-trader (quick-win, market-only — it NEVER arms).

For each pair the user toggles ON, a scheduled tick (every ``interval_seconds``, default 15 min)
OPENS a market trade — never a pending "armed" order. The pair's ``strategy`` picks the engine:
  * ``"scenario"`` (default) — AI-driven: follows the AI decider / primary forward scenario.
  * ``"supertrend"`` — mechanical (no LLM): the SuperTrend + EMA20-band breakout signal only.
Order:
  1. ROOM gate — if the book is already full (open positions >= ``max_open_positions``, default 3),
     do nothing (checked before any analysis work).
  2. When the pair is FLAT and past the short per-pair ``cooldown_minutes`` (default 5):
     (a) if the engine returns a decisive directional trade (risk-approved, conf >= min), open it;
     (b) SCENARIO only — else follow the AI's PRIMARY forward SCENARIO, opening its immediate next
         move NOW at market toward the named level (a quick win). SuperTrend has no fallback: a
         NO_TRADE just means no fresh band break, so it waits for the next tick.
The monitor rides it to TP/SL; the next tick re-enters.

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

# --- "reversal" strategy (a mechanical level-bounce scalp, like a human fading S/R) ---
_REV_NEAR_ATR = 0.5        # price within this many ATR of a level = "at the level"
_REV_RSI_HI = 55.0         # at resistance: RSI >= this AND rolling over (rsi < rsi_prev) = a rejection
_REV_RSI_LO = 45.0         # at support:    RSI <= this AND turning up (rsi > rsi_prev) = a rejection
_REV_STOP_ATR = 0.5        # stop sits this many ATR BEYOND the rejected level (tight, scalp-style)
_REV_MIN_RR = 1.0          # small quick-win target is fine (>= this R to the opposite level)


def get_or_create_auto_trade_config(session: Session) -> AutoTradeConfig:
    cfg = session.get(AutoTradeConfig, 1)
    if cfg is None:
        cfg = AutoTradeConfig(id=1, enabled=True, interval_seconds=900, min_confidence=0.60,
                              min_rr=1.2, min_profit_usd=20.0, cooldown_minutes=5, strategy="scenario",
                              timeframe="1h", pairs=[])
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
    from app.agents.orchestrator import _higher_trend
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

    # DON'T fight the immediate higher-TF trend — a scenario SHORT into an uptrend (or a LONG into a
    # downtrend) is the counter-trend fade that kept getting run over (the ETHUSDm/USOIL losses). Only
    # take the move when the higher TF agrees or is neutral.
    htf_trend, _htf_name = _higher_trend(technical, tf)
    if (direction == Direction.LONG and htf_trend == "down") or (direction == Direction.SHORT and htf_trend == "up"):
        return None

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


def _open_reversal_move(session: Session, symbol: str, ac: str, tf: str, technical, cfg: AutoTradeConfig):
    """MECHANICAL LEVEL-BOUNCE SCALP — what a human does watching the chart: when price rejects a nearby
    S/R level (tags it, prints a REAL rejection candle, and momentum turns off it), open the quick move
    toward the OPPOSITE level.
    Sell a RESISTANCE rejection down to the nearest support; buy a SUPPORT rejection up to the nearest
    resistance. Small, fast, no LLM. Trend-filtered so it BUYS DIPS in an uptrend and SELLS RALLIES in a
    downtrend (won't fade a strong higher-TF trend), and fades both edges in a range. Stop sits just
    beyond the rejected level (tight); target is the opposite level (a modest, real quick-win)."""
    from app.agents.orchestrator import _ADX_STRONG, _higher_trend
    from app.models.enums import Direction
    from app.models.schemas import TradeProposal
    from app.risk.service import assess

    if technical is None or not technical.timeframes:
        return None
    tf0 = next((x for x in technical.timeframes if x.timeframe == tf), technical.timeframes[0])
    ind = tf0.indicators or {}
    price, atr = ind.get("last_close"), ind.get("atr14")
    rsi, rsi_prev = ind.get("rsi14"), ind.get("rsi14_prev")
    rej_bull, rej_bear = ind.get("rej_bull") or 0.0, ind.get("rej_bear") or 0.0  # rejection candle
    fb_bull, fb_bear = ind.get("fbreak_bull") or 0.0, ind.get("fbreak_bear") or 0.0  # failed break / trap
    reject_bear = rej_bear == 1.0 or fb_bear == 1.0   # a rejection candle OR a failed upside break
    reject_bull = rej_bull == 1.0 or fb_bull == 1.0   # a rejection candle OR a failed downside break
    adx = ind.get("adx")
    if not price or not atr or atr <= 0 or rsi is None or rsi_prev is None:
        return None

    sup, res = _nearest_levels(technical, price)          # nearest support below / resistance above
    htf_trend, _htf_name = _higher_trend(technical, tf)
    strong_htf = adx is not None and adx >= _ADX_STRONG
    near = _REV_NEAR_ATR * atr

    direction = target = stop = level = None
    kind = ""
    # REJECTION AT RESISTANCE -> short toward support (skip if fighting a strong uptrend). Requires a
    # REAL bearish rejection at the level — a candle (engulfing / long upper wick) OR a failed upside
    # break (swept above then closed back below = bull trap) — not just RSI ticking down.
    if (res is not None and (res - price) <= near and rsi >= _REV_RSI_HI and rsi < rsi_prev
            and reject_bear
            and sup is not None and sup < price and not (htf_trend == "up" and strong_htf)):
        direction, target, level, kind = Direction.SHORT, sup, res, "resistance"
        stop = round(res + _REV_STOP_ATR * atr, 6)
    # REJECTION AT SUPPORT -> long toward resistance (skip if fighting a strong downtrend). Requires a
    # REAL bullish rejection at the level — a candle (engulfing / long lower wick) OR a failed downside
    # break (swept below then closed back above = bear trap).
    elif (sup is not None and (price - sup) <= near and rsi <= _REV_RSI_LO and rsi > rsi_prev
          and reject_bull
          and res is not None and res > price and not (htf_trend == "down" and strong_htf)):
        direction, target, level, kind = Direction.LONG, res, sup, "support"
        stop = round(sup - _REV_STOP_ATR * atr, 6)
    else:
        return None

    risk = (price - stop) if direction == Direction.LONG else (stop - price)
    reward = (target - price) if direction == Direction.LONG else (price - target)
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < max(_REV_MIN_RR, cfg.min_rr):
        return None
    prop = TradeProposal(
        symbol=symbol, asset_class=AssetClass(ac), timeframe=tf, direction=direction,
        entry=round(price, 6), stop_loss=stop, take_profit=round(target, 6), confidence=0.65,
        rationale=(f"Auto-trade reversal: price rejected {kind} ~{round(level, 6)} "
                   f"({'failed break/trap' if (fb_bear or fb_bull) else 'rejection candle'}, "
                   f"RSI {rsi:.0f} turning) — {direction.value} to the opposite level "
                   f"{round(target, 6)} (~{rr:.1f}R)."),
        strategy="auto_trade")
    dec = assess(session, prop, override_cooldown_minutes=cfg.cooldown_minutes)
    if not dec.approved or not dec.risk_amount:
        return None
    potential = dec.risk_amount * rr
    if potential < cfg.min_profit_usd:
        return None
    if _open_market(session, prop, dec) is not None:
        log.warning("auto-trade opened (reversal)",
                    extra={"symbol": symbol, "dir": direction.value, "level": round(level, 6)})
        return {"symbol": symbol, "opened": direction.value,
                "note": f"reversal off {kind} → {round(target, 6)} (~${potential:.0f})"}
    return None


def _auto_trade_symbol(session: Session, cfg: AutoTradeConfig, pair: dict) -> dict:
    """Analyse one pair and auto-open if it qualifies. Returns a short note dict."""
    symbol = pair.get("symbol")
    ac = pair.get("asset_class", "forex")
    # ONE timeframe for every auto-traded pair (the panel's Timeframe selector); the per-pair value is
    # only a fallback for a config saved before the global field existed.
    tf = (cfg.timeframe or pair.get("timeframe") or "1h")

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
    if not broker.market_open(symbol):
        return {"symbol": symbol, "skipped": "market closed"}
    if not getattr(broker, "is_paper", False):
        return {"symbol": symbol, "skipped": "live broker — auto-open needs typed confirmation (paper-only)"}

    supertrend = (cfg.strategy or "scenario") == "supertrend"
    reversal = (cfg.strategy or "scenario") == "reversal"
    try:
        if supertrend:
            # SUPERTREND strategy — mechanical (no LLM = no tokens): st_band=True forces the deterministic
            # SuperTrend + EMA20-band decision for THIS call only (the rest of the system is unchanged).
            # A directional signal fires below via path (a); no signal -> NO_TRADE -> skip (no fallback).
            res = analyze_symbol(session, symbol, AssetClass(ac), tf, use_llm=False, source="auto_trade",
                                 cooldown_override=cfg.cooldown_minutes, st_band=True, min_rr=cfg.min_rr)
        elif reversal:
            # REVERSAL strategy — a mechanical level-bounce scalp (no LLM). Just fetch the deterministic
            # read (no_execute so the trend engine can't open its own trade); MY level-bounce logic below
            # (`_open_reversal_move`) is the only thing that opens.
            res = analyze_symbol(session, symbol, AssetClass(ac), tf, use_llm=False, source="auto_trade",
                                 cooldown_override=cfg.cooldown_minutes, no_execute=True, min_rr=cfg.min_rr)
        else:
            # SCENARIO strategy — force_ai_decide lets the AI decider decide on the pair's OWN scenario
            # levels (follows the scenario read), regardless of the global "AI decides" toggle. The short
            # cooldown is applied to the risk check via the override (not the global one).
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

    # (a) An immediate market OPEN — direction set, risk-approved, confident enough. Skipped for the
    # REVERSAL strategy, which ignores the deterministic trend proposal and uses its own level-bounce.
    if not reversal and prop.direction.value in ("long", "short"):
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

    # (b) Strategy-specific open. REVERSAL: a mechanical level-bounce scalp (sell a resistance rejection
    # to support / buy a support rejection to resistance). SUPERTREND: no fallback (a NO_TRADE = no fresh
    # band break, wait). SCENARIO: follow the AI's primary forward scenario at market (quick win).
    if reversal:
        rev = _open_reversal_move(session, symbol, ac, tf, prop.technical, cfg)
        if rev is not None:
            return rev
        return {"symbol": symbol, "skipped": "no reversal setup (price not rejecting a level)"}
    if not supertrend:
        scen = _open_scenario_move(session, symbol, ac, tf, prop.technical, cfg)
        if scen is not None:
            return scen

    return {"symbol": symbol, "skipped": f"no setup ({prop.direction.value}, {prop.confidence:.0%})"}


# Circuit-breaker: the per-pair auto-trader is the one net-negative source (its counter-trend "quick
# win" scenario moves get run over). After a losing RUN we pause auto-opening for a cooldown, then let
# a single probe through — so it never locks on permanently, and it slows down exactly when it's cold.
_BREAKER_LOOKBACK = 8       # judge the auto-trader on its last N closed trades
_BREAKER_MIN = 5            # need at least this many before the NET-negative trip can fire
_BREAKER_STREAK = 3         # ...but N losses IN A ROW trips it even if a stray win nets the window positive
_BREAKER_PAUSE_HOURS = 4    # pause window from the most recent close after a losing run


def _auto_trade_breaker(session: Session) -> str | None:
    """Returns a reason string when the auto-trader should be paused, else None. Two trips (both within
    the cooldown from the most recent close): a losing STREAK (``_BREAKER_STREAK`` losses in a row —
    catches a biased run that a stray win would hide from a net test) OR the last ``_BREAKER_MIN`` being
    net-negative. Self-resets: once the cooldown elapses a probe is allowed; if it loses, the window
    updates and it re-trips (so a bad run stays slow without ever hard-locking)."""
    rows = list(session.scalars(
        select(Position).where(Position.status == PositionStatus.CLOSED.value,
                               Position.source == "auto_trade",
                               Position.realized_pnl.is_not(None))
        .order_by(Position.closed_at.desc()).limit(_BREAKER_LOOKBACK)))
    if len(rows) < _BREAKER_STREAK:
        return None
    last = rows[0].closed_at            # rows are newest-first, so [0] is the most recent close
    if last is None:
        return None
    last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    hrs = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    if hrs >= _BREAKER_PAUSE_HOURS:
        return None  # cooldown elapsed -> let a probe trade through
    left = f"paused ~{_BREAKER_PAUSE_HOURS - hrs:.1f}h for conditions to change"
    streak = 0
    for p in rows:
        if (p.realized_pnl or 0) < 0:
            streak += 1
        else:
            break
    if streak >= _BREAKER_STREAK:
        return f"circuit-breaker: {streak} auto-trades lost in a row — {left}"
    net = sum(p.realized_pnl for p in rows)
    if len(rows) >= _BREAKER_MIN and net < 0:
        wins = sum(1 for p in rows if (p.realized_pnl or 0) > 0)
        return f"circuit-breaker: last {len(rows)} auto-trades net ${net:.0f} ({wins}/{len(rows)} win) — {left}"
    return None


def run_auto_trade(session: Session) -> dict:
    """One pass over every enabled pair. Records to the audit log + stamps last_run."""
    cfg = get_or_create_auto_trade_config(session)
    breaker = _auto_trade_breaker(session)
    if breaker:
        cfg.last_run_at = datetime.now(timezone.utc)
        cfg.last_result = breaker
        cfg.last_results = [{"symbol": p.get("symbol"), "skipped": "circuit-breaker (recent losses)"}
                            for p in (cfg.pairs or [])]
        session.add(AgentRun(agent="auto_trade", event="tick",
                             detail={"breaker": breaker, "pairs": len(cfg.pairs or [])}))
        session.commit()
        log.warning("auto-trade paused by circuit-breaker", extra={"reason": breaker})
        return {"ran": True, "opened": 0, "breaker": breaker, "results": cfg.last_results}
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
