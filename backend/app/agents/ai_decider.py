"""AI DECIDER (your architecture) — the deterministic engine is the ANALYST, the AI is the JUDGE.

Flow:
  1. The deterministic engine has already done ALL the analysis (regime, structure, S/R, the wall
     factor, its own proposal + confidence, and the two scenarios).
  2. We hand the AI a complete, factual DECISION BRIEF built from that output — real levels, level
     strength, the two scenarios, trend maturity, and the engine's own historical hit-rate.
  3. The AI picks the better scenario and DECIDES: open now (long/short), ARM a pending order at a
     better price (breakout or pullback), or stand aside. It uses ONLY the levels in the brief.
  4. Thin capital-protective guardrails validate it; the deterministic Risk Manager sizes/gates it
     downstream. The AI NEVER sizes and NEVER touches risk limits.

Reproducibility: use a NON-reasoning model (gpt-4.1) at temperature=0 so the same brief gives the same
decision (llm.py pins it). Reasoning models can't be pinned and will flip — not recommended here.
When the LLM is unavailable/fails, we return the deterministic proposal unchanged (safe fallback).
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.enums import AssetClass, ConditionalOrderType, Direction, PositionStatus
from app.models.schemas import ConditionalSuggestion, FundamentalRead, TechnicalRead, TradeProposal

log = get_logger("agents.ai_decider")

_ARM_MIN_RR = 1.5   # reject an AI arm below this reward:risk (arms bypass the open-path min-R:R gate)


class _AiScenario(BaseModel):
    label: str = Field(description="a short name YOU coin for this scenario")
    direction: str = Field(description="up | down | sideways")
    probability: int = Field(description="0-100, YOUR estimated likelihood; the two should sum to ~100")
    action: str = Field(description='how you would TRADE this scenario: "open_long" | "open_short" | '
                                    '"arm_long" | "arm_short" | "none" (mark none if its only entry is thin/untradeable)')
    trigger_price: float | None = Field(default=None, description="ARM actions only: the price that activates the pending order")
    stop_loss: float | None = Field(default=None, description="this scenario's protective stop (a real level from the brief)")
    take_profit: float | None = Field(default=None, description="this scenario's target (a real level from the brief)")
    path: str = Field(description="the trigger -> target path in one line, using the REAL levels from the brief")
    reasoning: str = Field(description="why this could happen, citing the deterministic facts (levels, structure, momentum, volume)")


class _DecisionLLM(BaseModel):
    scenarios: list[_AiScenario] = Field(description="the TWO forward scenarios YOU CREATE, EACH with its own concrete trade plan")
    rationale: str = Field(description="desk-head reasoning across the two scenarios")
    key_risks: list[str] = Field(default_factory=list, description="what would make the read wrong")


_SYSTEM = (
    "You are the desk head running the book (FX, indices, commodities, crypto). A deterministic analyst "
    "has done the HOMEWORK for you and handed you a complete FACTS brief: the multi-timeframe alignment "
    "(1h/4h/1d trend+structure), the support/resistance levels and how many times each was tested, the "
    "level LADDER of targets each way, upcoming high-impact events with time-to-event, market structure, "
    "regime, momentum/price-action, RSI, volume, the trend's maturity, the invalidation level, and the "
    "engine's own mechanical read plus its historical hit-rate. These are FACTS and numbers — NOT "
    "scenarios. Weigh higher-timeframe agreement heavily, use the ladder for realistic targets, and if a "
    "high-impact event is imminent prefer arming/waiting over opening into it.\n"
    "YOUR job, in order:\n"
    "  1. CREATE the two most plausible forward scenarios YOURSELF from these facts (you write them — "
    "usually one continuation and one pullback/rejection), each with a probability, a path using the "
    "REAL levels, and reasoning.\n"
    "  2. For EACH scenario, give its CONCRETE TRADE PLAN: the action (open_long/open_short = enter now "
    "at market; arm_long/arm_short = a PENDING order at a better price, give trigger_price; or none = "
    "not tradeable), plus stop_loss and take_profit anchored to real structure. Aim for >=2R to the "
    "target before opposing structure; if a scenario's only entry is thin (target too close, or into a "
    "wall), mark its action 'none' HONESTLY rather than forcing a bad trade.\n"
    "IMPORTANT: you do NOT pick the winner. The desk's deterministic risk engine picks the scenario with "
    "the best TRADEABLE reward:risk — so a 45% scenario with a clean 2R+ setup is chosen over a 55% one "
    "that is only ~0.7R. Make each plan realistic; that is how you get traded.\n"
    "Rules: use ONLY the price levels in the brief — NEVER invent numbers. Stop and target on the correct "
    "side, sane stop distance (roughly 0.5-3x ATR) — not a hair-trigger. Prefer ARM over chasing into a "
    "nearby wall. Do NOT fight a strong higher-timeframe trend. Standing aside (both actions 'none') is a "
    "professional answer. You NEVER size the trade — a separate deterministic Risk Manager does that.\n"
    "ARM level placement (get this right):\n"
    "  arm_long breakout : trigger = ABOVE resistance; stop = just BELOW that level; target = a higher level.\n"
    "  arm_long pullback : trigger = at support BELOW price; stop = BELOW support; target = a higher level.\n"
    "  arm_short breakdown: trigger = BELOW support; stop = just ABOVE that level; target = a lower level.\n"
    "  arm_short pullback : trigger = at resistance ABOVE price; stop = ABOVE resistance; target = a lower level."
)


def _calibration_note(session: Session, confidence: float) -> str:
    """The engine's REAL history: overall win/expectancy + the same-confidence bucket, so the AI can
    ground its probability in what actually happened rather than vibes. Empty-safe."""
    from app.core.state import get_or_create_settings
    from app.models.db import Position

    conds = [Position.status == PositionStatus.CLOSED.value,
             Position.realized_pnl.is_not(None), Position.risk_amount.is_not(None)]
    reset = get_or_create_settings(session).journal_reset_at
    if reset is not None:
        conds.append(Position.closed_at >= reset)
    rows = session.scalars(select(Position).where(*conds)).all()
    if not rows:
        return "No closed-trade history yet (fresh journal) — no calibration available."

    def _stats(sample):
        n = len(sample)
        if not n:
            return None
        wins = sum(1 for r in sample if (r.realized_pnl or 0.0) > 0)
        rs = [(r.realized_pnl or 0.0) / r.risk_amount for r in sample if r.risk_amount]
        exp = sum(rs) / len(rs) if rs else 0.0
        return n, wins / n, exp

    overall = _stats(rows)
    lo = (int(confidence * 10) / 10)              # bucket floor, e.g. 0.7
    bucket = _stats([r for r in rows if lo <= (r.confidence or 0.0) < lo + 0.1])
    parts = [f"overall {overall[0]} trades: {overall[1]*100:.0f}% win, {overall[2]:+.2f}R avg"]
    if bucket:
        parts.append(f"in the {int(lo*100)}-{int(lo*100)+10}% confidence bucket: {bucket[0]} trades, "
                     f"{bucket[1]*100:.0f}% win, {bucket[2]:+.2f}R avg")
    return "Engine history — " + "; ".join(parts) + "."


def _maturity_note(ind: dict, entry: float | None) -> str:
    """How mature/extended the move is: bars since the SuperTrend flip + distance from value (EMA20)
    in ATRs. Young + near value -> favour open now; old + stretched -> favour arming a pullback."""
    bits = []
    since = ind.get("supertrend_bars_since_flip")
    if since is not None:
        age = "fresh" if since <= 6 else "maturing" if since <= 20 else "late/extended"
        bits.append(f"{int(since)} bars since the last SuperTrend flip ({age})")
    ema20, atr = ind.get("ema20"), ind.get("atr14")
    if entry and ema20 and atr:
        d = abs(entry - ema20) / atr
        loc = "at value" if d <= 1.0 else "stretched" if d >= 2.5 else "getting extended"
        bits.append(f"{d:.1f} ATR from EMA20 value ({loc})")
    return "; ".join(bits) if bits else "n/a"


def _mtf_line(technical: TechnicalRead) -> str:
    """① Multi-timeframe alignment: each timeframe's trend + structure + ADX, spelled out so the AI can
    weight higher-TF agreement (a 1h long with 4h+1d up is very different from 1h up / 4h down)."""
    parts = []
    for tf in technical.timeframes:
        ind = tf.indicators or {}
        st = ind.get("structure")
        struct = "HH/HL" if (st and st > 0.5) else "LH/LL" if (st and st < -0.5) else "range"
        adx = ind.get("adx")
        parts.append(f"{tf.timeframe}: {tf.trend} ({struct}{f', ADX {adx:.0f}' if adx is not None else ''})")
    return " | ".join(parts) if parts else "n/a"


def _events_line(fundamental: FundamentalRead, now: datetime) -> str:
    """② Upcoming high-impact events (from the calendar's stand-aside windows) with time-to-event, so
    the AI can choose arm/wait instead of open into a data release."""
    ups = []
    for w in fundamental.stand_aside_windows:
        start = w.start if w.start.tzinfo else w.start.replace(tzinfo=timezone.utc)
        if start >= now:
            ups.append(((start - now).total_seconds() / 60.0, w.label, w.importance))
    ups.sort()
    if not ups:
        return "none flagged ahead"

    def _fmt(m: float) -> str:
        return f"{int(m)}m" if m < 60 else f"{int(m // 60)}h{int(m % 60):02d}m"

    return "; ".join(f"{lab} in {_fmt(m)} ({imp})" for m, lab, imp in ups[:3])


def _ladder_str(ladder: list[dict], fmt) -> str:
    """③ Format a level ladder as 'price (Nx), price (Nx), …'."""
    return ", ".join(f"{fmt(l['price'])} ({l['tests']}x)" for l in ladder) if ladder else "n/a"


def _order_type(direction: Direction, trigger: float, price: float) -> ConditionalOrderType:
    """Infer the pending-order type from direction + where the trigger sits vs current price."""
    if direction == Direction.LONG:
        return ConditionalOrderType.BUY_STOP if trigger >= price else ConditionalOrderType.BUY_LIMIT
    return ConditionalOrderType.SELL_STOP if trigger <= price else ConditionalOrderType.SELL_LIMIT


def _norm_action(a: str | None) -> str:
    a = (a or "none").lower().strip()
    return a if a in ("open_long", "open_short", "arm_long", "arm_short") else "none"


def _score_scenario(sc: "_AiScenario", price: float | None, atr: float | None) -> dict:
    """Light tradeability + reward:risk for a scenario's trade plan, used to RANK the scenarios. A
    scenario is 'tradeable' only if its action has valid, correctly-sided levels, a sane ATR stop, and
    R:R >= the floor — so the decider can prefer a lower-probability but genuinely tradeable setup over
    a higher-probability one that is thin. Never raises."""
    action = _norm_action(sc.action)
    ev = {"action": action, "kind": "none", "direction": None, "entry": None,
          "stop": sc.stop_loss, "target": sc.take_profit, "rr": None, "tradeable": False, "reject": ""}
    if action == "none":
        ev["reject"] = "no trade plan"
        return ev
    is_long = action.endswith("long")
    is_arm = action.startswith("arm")
    ev["direction"] = "long" if is_long else "short"
    ev["kind"] = "arm" if is_arm else "open"
    entry = sc.trigger_price if is_arm else price      # arm enters at the trigger; open at the market
    ev["entry"] = entry
    stop, tp = sc.stop_loss, sc.take_profit
    if entry is None or stop is None or tp is None:
        ev["reject"] = "incomplete levels"
        return ev
    ok_sides = (stop < entry < tp) if is_long else (tp < entry < stop)
    risk = abs(entry - stop)
    if risk <= 0 or not ok_sides:
        ev["reject"] = "levels on the wrong side"
        return ev
    if atr and (risk < 0.25 * atr or risk > 6.0 * atr):
        ev["reject"] = "stop too tight/wide vs ATR"
        return ev
    rr = abs(tp - entry) / risk
    ev["rr"] = round(rr, 2)
    if rr < _ARM_MIN_RR:
        ev["reject"] = f"thin R:R (~{rr:.1f}R)"
        return ev
    ev["tradeable"] = True
    return ev


def build_decision_brief(session: Session, symbol: str, asset_class: AssetClass, timeframe: str,
                         proposal: TradeProposal, technical: TechnicalRead,
                         fundamental: FundamentalRead, now: datetime) -> tuple[str, float | None]:
    """Assemble the plain-text decision brief for the AI. Returns (brief, current_price)."""
    from app.agents.context import _fmt, build_context

    tf0 = None
    if technical.timeframes:
        tf0 = next((x for x in technical.timeframes if x.timeframe == timeframe), technical.timeframes[0])
    ind = tf0.indicators if tf0 else {}
    price = ind.get("last_close")

    ctx = build_context(session, symbol, asset_class)  # the deterministic FACTS (levels, strength, structure)
    cal = _calibration_note(session, proposal.confidence or 0.0)
    maturity = _maturity_note(ind, price)
    mtf = _mtf_line(technical)                          # ① multi-timeframe alignment
    events = _events_line(fundamental, now)             # ② upcoming high-impact events
    try:                                                # AI's own graded track record (shadow scorecard)
        from app.agents.shadow import shadow_note
        shadow = shadow_note(session)
    except Exception:  # noqa: BLE001
        shadow = None

    # The engine's mechanical read — a REFERENCE fact, not a scenario. The AI builds its own scenarios.
    eng = (f"direction={proposal.direction.value} entry={proposal.entry} stop={proposal.stop_loss} "
           f"target={proposal.take_profit} confidence={proposal.confidence} regime={proposal.regime}\n"
           f"  engine note: {proposal.rationale}")

    lines = [
        "DETERMINISTIC FACTS BRIEF (the analyst did the homework — these are numbers/facts, NOT scenarios; "
        "YOU create the scenarios from them):",
        f"INSTRUMENT: {symbol}  timeframe: {timeframe}  CURRENT PRICE: {price}  ATR(14): {ind.get('atr14')}",
        f"MULTI-TIMEFRAME ALIGNMENT: {mtf}",
        f"HIGH-IMPACT EVENTS AHEAD: {events}",
        "",
        "ENGINE MECHANICAL READ (reference only — its rule-based direction/levels; you may agree, upgrade, arm, or stand aside):",
        f"  {eng}",
        "",
        f"CALIBRATION (the engine's REAL history) — {cal}",
    ]
    if shadow:
        lines.append(shadow)   # your own graded track record — learn from what actually happened
    lines += [
        f"TREND MATURITY — {maturity}",
    ]
    if ctx:
        nr, ns = ctx.get("nearest_resistance"), ctx.get("nearest_support")
        ls = ctx.get("level_strength", {})
        lines += [
            "",
            f"STRUCTURE: {ctx.get('structure')}  (change-of-character: {ctx.get('choch')})",
            f"NEAREST RESISTANCE: {nr['price'] if nr else 'n/a'} [strength: {ls.get('resistance')}]",
            f"NEAREST SUPPORT: {ns['price'] if ns else 'n/a'} [strength: {ls.get('support')}]",
            f"RESISTANCE LADDER (targets up): {_ladder_str(ctx.get('resistance_ladder', []), _fmt)}",
            f"SUPPORT LADDER (targets down): {_ladder_str(ctx.get('support_ladder', []), _fmt)}",
            f"CHANNEL: {ctx.get('channel') or 'n/a'}",
            f"MOMENTUM/PRICE-ACTION: {ctx.get('price_action')}   VOLUME: {ctx.get('volume_trend')}   RSI: {ctx.get('rsi')}",
            "FACTOR SCORECARD: " + "; ".join(f"{s['factor']} {s['signal']} ({s['note']})" for s in ctx.get("scorecard", [])),
            f"INVALIDATION LEVEL (flips the structural read): {ctx.get('invalidation')}",
        ]
    lines += ["", "Now: CREATE your two scenarios from these facts, choose the most realistic, and decide. "
              "Use only the levels above (the ladders are your targets) — do not invent numbers."]
    return "\n".join(lines), price


def ai_decide_trade(session: Session, symbol: str, asset_class: AssetClass, timeframe: str,
                    proposal: TradeProposal, technical: TechnicalRead, fundamental: FundamentalRead,
                    now: datetime) -> TradeProposal:
    """Let the AI decide from the deterministic brief. Returns a (possibly new) TradeProposal.

    Falls back to the deterministic ``proposal`` unchanged when the LLM is unavailable/failed."""
    from app.agents.orchestrator import _apply_guardrails, _now_in_stand_aside

    # Hard safety: never trade inside a flagged high-impact event window — the AI can't override this.
    if _now_in_stand_aside(fundamental, now):
        proposal.direction = Direction.NO_TRADE
        proposal.entry = proposal.stop_loss = proposal.take_profit = None
        proposal.strategy = "stand_aside"
        proposal.rationale = "Standing aside: inside a high-impact event window (AI decision suppressed)."
        return proposal

    if not llm_available():
        return proposal  # AI down -> deterministic decides

    brief, price = build_decision_brief(session, symbol, asset_class, timeframe, proposal, technical,
                                        fundamental, now)
    decision = analyze(system=_SYSTEM, user=brief, schema=_DecisionLLM, max_tokens=1800)
    if decision is None:
        log.info("ai decider unavailable; keeping deterministic proposal", extra={"symbol": symbol})
        return proposal

    tf0 = (next((x for x in technical.timeframes if x.timeframe == timeframe), technical.timeframes[0])
           if technical.timeframes else None)
    atr = tf0.indicators.get("atr14") if tf0 else None

    # Score each scenario's trade plan, then the DECIDER picks the best TRADEABLE one (adequate R:R),
    # ranked by probability — so a 45% scenario worth 5.6R beats a 55% one worth 0.7R, instead of the
    # AI acting on the highest-probability idea even when it's untradeable.
    scored = [(sc, _score_scenario(sc, price, atr)) for sc in decision.scenarios]
    aid_scen = [{"label": sc.label, "direction": sc.direction, "prob": sc.probability, "path": sc.path,
                 "reasoning": sc.reasoning, "action": ev["action"], "rr": ev["rr"],
                 "tradeable": ev["tradeable"]} for sc, ev in scored]
    risks = list(decision.key_risks)
    risk_tail = (" | risks: " + "; ".join(risks)) if risks else ""
    aid_base = {"summary": decision.rationale, "risks": risks, "scenarios": aid_scen}

    def _mk_base() -> TradeProposal:
        return TradeProposal(symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                             direction=Direction.NO_TRADE, confidence=0.0, technical=technical,
                             fundamental=fundamental, regime=proposal.regime, review_decision="ai",
                             strategy="ai")

    ranked = sorted([(sc, ev) for sc, ev in scored if ev["tradeable"]],
                    key=lambda x: (x[0].probability, x[1]["rr"] or 0), reverse=True)

    if not ranked:
        best = max(decision.scenarios, key=lambda s: s.probability, default=None)
        why = "; ".join(f"{sc.label} ({ev['reject']})" for sc, ev in scored if ev["action"] != "none") \
            or "no tradeable setup"
        base = _mk_base()
        base.strategy = "stand_aside"
        base.rationale = f"AI stood aside — no tradeable scenario ({why}). {decision.rationale}{risk_tail}"
        base.ai_decision = {**aid_base, "kind": "stand_aside", "action": "stand_aside",
                            "chosen": best.label if best else "",
                            "why_chosen": "no scenario cleared the reward:risk floor",
                            "conviction": round((best.probability / 100) if best else 0.0, 2)}
        log.info("ai decision: stand_aside (nothing tradeable)", extra={"symbol": symbol})
        return base

    # Act on the best tradeable scenario; if an OPEN is blocked by a guardrail, fall back to the next.
    for sc, ev in ranked:
        conv = round(min(0.95, max(0.05, sc.probability / 100)), 2)
        direction = Direction.LONG if ev["direction"] == "long" else Direction.SHORT
        chose = f"CHOSE '{sc.label}' ({sc.probability}%, ~{ev['rr']}R)"
        why_chosen = f"best tradeable reward:risk (~{ev['rr']}R at {sc.probability}% probability)"
        tail = f" {decision.rationale}{risk_tail}"
        aid_common = {**aid_base, "chosen": sc.label, "why_chosen": why_chosen, "conviction": conv,
                      "direction": direction.value, "rr": ev["rr"]}

        if ev["kind"] == "arm":
            trig, stop, tp = ev["entry"], ev["stop"], ev["target"]
            base = _mk_base()
            base.direction = Direction.NO_TRADE
            base.watch = True
            base.conditional = ConditionalSuggestion(
                order_type=_order_type(direction, trig, price).value,
                trigger_price=round(trig, 6), stop_loss=round(stop, 6), take_profit=round(tp, 6),
                confidence=conv, rr=ev["rr"], reason=f"AI-armed — {chose}.{tail}",
            )
            base.rationale = (f"AI ARMED a {direction.value.upper()} {base.conditional.order_type} at "
                              f"{base.conditional.trigger_price} (~{ev['rr']}R) — {chose}.{tail}")
            base.ai_decision = {**aid_common, "kind": "arm", "action": ev["action"],
                                "order_type": base.conditional.order_type, "entry": base.conditional.trigger_price,
                                "stop": base.conditional.stop_loss, "target": base.conditional.take_profit}
            log.info("ai decision: arm", extra={"symbol": symbol, "dir": direction.value,
                                                "trigger": base.conditional.trigger_price})
            return base

        # OPEN NOW — build + capital-protective guardrails; if blocked, try the next tradeable scenario.
        cand = _mk_base()
        cand.direction = direction
        cand.entry = ev["entry"]
        cand.stop_loss = ev["stop"]
        cand.take_profit = ev["target"]
        cand.confidence = conv
        cand.rationale = f"AI OPEN {direction.value.upper()} — {chose}.{tail}"
        cand.ai_decision = {**aid_common, "kind": "open", "action": ev["action"],
                            "entry": ev["entry"], "stop": ev["stop"], "target": ev["target"]}
        decided = _apply_guardrails(cand, technical)
        if decided.direction != Direction.NO_TRADE:
            log.info("ai decision: open", extra={"symbol": symbol, "dir": direction.value})
            return decided
        log.info("ai open blocked by guardrail; trying next tradeable scenario", extra={"symbol": symbol})

    # Every tradeable scenario's OPEN was blocked by a guardrail (e.g. against the higher-TF trend).
    best_sc, best_ev = ranked[0]
    base = _mk_base()
    base.strategy = "stand_aside"
    base.rationale = (f"AI stood aside — best scenario '{best_sc.label}' blocked by a capital-protective "
                      f"guardrail. {decision.rationale}{risk_tail}")
    base.ai_decision = {**aid_base, "kind": "blocked", "action": best_ev["action"],
                        "chosen": best_sc.label, "why_chosen": why_chosen,
                        "conviction": round(best_sc.probability / 100, 2),
                        "note": "best scenario failed a capital-protective guardrail"}
    return base
