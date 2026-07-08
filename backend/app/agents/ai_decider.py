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
    probability: int = Field(description="0-100, YOUR estimated likelihood; the scenarios should sum to ~100")
    path: str = Field(description="the trigger -> target path in one line, using the REAL levels from the brief")
    reasoning: str = Field(description="why this could happen, citing the deterministic facts (levels, structure, momentum, volume)")


class _DecisionLLM(BaseModel):
    scenarios: list[_AiScenario] = Field(description="the TWO forward scenarios YOU CREATE from the facts (do not copy — build them)")
    chosen: str = Field(description="the label of the scenario you judge the MOST REALISTIC / most likely")
    why_chosen: str = Field(description="why the chosen scenario is the most realistic vs the other — the head-to-head")
    action: str = Field(description='the decision from the chosen scenario: "open_long" | "open_short" | "arm_long" | "arm_short" | "stand_aside"')
    conviction: float = Field(description="0-1, your honest edge in the chosen scenario")
    trigger_price: float | None = Field(default=None, description="ARM only: the price that activates the pending order")
    stop_loss: float | None = Field(default=None, description="protective stop (a real level from the brief)")
    take_profit: float | None = Field(default=None, description="target (a real level from the brief), >=1.5R away")
    rationale: str = Field(description="desk-head reasoning for the action (open now vs arm vs wait)")
    key_risks: list[str] = Field(default_factory=list, description="what would make this wrong")


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
    "usually one continuation and one pullback/rejection), each with a probability that reflects the "
    "evidence, a path using the REAL levels, and reasoning.\n"
    "  2. CHOOSE the single MOST REALISTIC / most likely scenario and explain why it beats the other.\n"
    "  3. DECIDE the action that follows from the chosen scenario:\n"
    "     open_long / open_short = enter now at the market (the chosen scenario is ready to go);\n"
    "     arm_long / arm_short   = don't chase — place a PENDING order at a better price (a breakout "
    "trigger above/below price, or a pullback trigger back at a level) and give trigger_price;\n"
    "     stand_aside            = the most realistic scenario is 'no edge / wait'.\n"
    "Rules: use ONLY the price levels in the brief — NEVER invent numbers. Stop and target on the correct "
    "side, anchored to real structure, >=1.5R (prefer 2R) to target before opposing structure. Keep the "
    "stop a SANE distance (roughly 0.5-3x ATR from entry) — not a hair-trigger. Prefer ARM over chasing "
    "into a nearby wall. Do NOT fight a strong higher-timeframe trend. Standing aside is a professional "
    "answer. You NEVER size the trade — a separate deterministic Risk Manager does that.\n"
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

    base = TradeProposal(
        symbol=symbol, asset_class=asset_class, timeframe=timeframe, direction=Direction.NO_TRADE,
        confidence=0.0, technical=technical, fundamental=fundamental,
        regime=proposal.regime, review_decision="ai", strategy="ai",
    )
    risks = (" | risks: " + "; ".join(decision.key_risks)) if decision.key_risks else ""
    action = (decision.action or "stand_aside").lower()
    # The AI CREATED these scenarios from the deterministic facts, then chose the most realistic — carry
    # both the scenarios and the head-to-head choice in the rationale so the user sees the reasoning.
    scen = " | ".join(f"{s.label} {s.probability}% [{s.direction}] — {s.path}" for s in decision.scenarios)
    chose = f"CHOSE '{decision.chosen}' ({decision.why_chosen})"
    tail = f" {decision.rationale}{risks} || Scenarios the AI built: {scen}"

    # Structured decision for the UI (rendered cleanly instead of parsing the rationale text).
    aid: dict = {
        "action": action, "chosen": decision.chosen, "why_chosen": decision.why_chosen,
        "summary": decision.rationale, "risks": list(decision.key_risks),
        "conviction": round(min(0.95, max(0.05, decision.conviction)), 2),
        "scenarios": [{"label": s.label, "direction": s.direction, "prob": s.probability,
                       "path": s.path, "reasoning": s.reasoning} for s in decision.scenarios],
    }

    if action == "stand_aside":
        base.strategy = "stand_aside"
        base.rationale = f"AI stood aside — {chose}.{tail}"
        base.ai_decision = {**aid, "kind": "stand_aside"}
        log.info("ai decision: stand_aside", extra={"symbol": symbol, "chosen": decision.chosen})
        return base

    is_long = action.endswith("long")
    direction = Direction.LONG if is_long else Direction.SHORT

    # --- ARM: a pending order at a better price (breakout or pullback) ---
    if action.startswith("arm"):
        trig, stop, tp = decision.trigger_price, decision.stop_loss, decision.take_profit
        if not (trig and stop and tp) or price is None:
            base.strategy = "stand_aside"
            base.rationale = f"AI wanted to ARM {direction.value} but gave incomplete levels; standing aside. {decision.rationale}"
            base.ai_decision = {**aid, "kind": "blocked", "note": "incomplete arm levels"}
            return base
        risk = abs(trig - stop)
        # side sanity: stop/target on the correct side of the TRIGGER.
        ok_sides = (stop < trig < tp) if is_long else (tp < trig < stop)
        if risk <= 0 or not ok_sides:
            base.strategy = "stand_aside"
            base.rationale = f"AI ARM {direction.value} rejected (stop/target on the wrong side of the trigger). {decision.rationale}"
            base.ai_decision = {**aid, "kind": "blocked", "note": "stop/target on the wrong side of the trigger"}
            return base
        # ATR sanity (arms don't go through _apply_guardrails): reject a hair-trigger / absurdly-wide stop,
        # which would otherwise size huge and get stopped on noise, or report a fake giant R:R.
        tf0 = next((x for x in technical.timeframes if x.timeframe == timeframe),
                   technical.timeframes[0]) if technical.timeframes else None
        atr = tf0.indicators.get("atr14") if tf0 else None
        if atr and (risk < 0.25 * atr or risk > 6.0 * atr):
            base.strategy = "stand_aside"
            reason = f"stop {'too tight' if risk < 0.25 * atr else 'too wide'} vs ATR"
            base.rationale = (f"AI ARM {direction.value} rejected: {reason} ({risk:.4f} vs ATR {atr:.4f}). "
                              f"{decision.rationale}")
            base.ai_decision = {**aid, "kind": "blocked", "note": reason}
            return base
        rr = abs(tp - trig) / risk
        # R:R floor (arms skip _apply_guardrails' min-R:R): a thin arm like ~0.1R (huge stop, tiny
        # target) is negative expectancy after costs — reject it rather than arm a bad trade.
        if rr < _ARM_MIN_RR:
            base.strategy = "stand_aside"
            base.rationale = (f"AI ARM {direction.value} rejected: only ~{rr:.1f}R "
                              f"(below the {_ARM_MIN_RR:.1f}R floor). {decision.rationale}")
            base.ai_decision = {**aid, "kind": "blocked", "note": f"thin reward:risk (~{rr:.1f}R)"}
            return base
        base.direction = Direction.NO_TRADE
        base.watch = True
        base.conditional = ConditionalSuggestion(
            order_type=_order_type(direction, trig, price).value,
            trigger_price=round(trig, 6), stop_loss=round(stop, 6), take_profit=round(tp, 6),
            confidence=round(min(0.95, max(0.05, decision.conviction)), 2), rr=round(rr, 2),
            reason=f"AI-armed — {chose}.{tail}",
        )
        base.rationale = (f"AI ARMED a {direction.value.upper()} {base.conditional.order_type} at "
                          f"{base.conditional.trigger_price} (~{rr:.1f}R) — {chose}.{tail}")
        base.ai_decision = {**aid, "kind": "arm", "direction": direction.value,
                            "order_type": base.conditional.order_type, "entry": base.conditional.trigger_price,
                            "stop": base.conditional.stop_loss, "target": base.conditional.take_profit,
                            "rr": round(rr, 2)}
        log.info("ai decision: arm", extra={"symbol": symbol, "dir": direction.value,
                                            "trigger": base.conditional.trigger_price})
        return base

    # --- OPEN NOW: a market entry at the current price ---
    base.direction = direction
    base.entry = price if price else proposal.entry
    base.stop_loss = decision.stop_loss
    base.take_profit = decision.take_profit
    base.confidence = round(min(0.95, max(0.05, decision.conviction)), 2)
    base.rationale = f"AI OPEN {direction.value.upper()} — {chose}.{tail}"
    orr = (abs(base.take_profit - base.entry) / abs(base.entry - base.stop_loss)
           if base.entry and base.stop_loss and base.take_profit and base.entry != base.stop_loss else None)
    base.ai_decision = {**aid, "kind": "open", "direction": direction.value, "entry": base.entry,
                        "stop": base.stop_loss, "target": base.take_profit,
                        "rr": round(orr, 2) if orr else None}
    decided = _apply_guardrails(base, technical)   # thin capital-protective checks; may -> NO_TRADE
    if decided.direction == Direction.NO_TRADE and decided.ai_decision:
        decided.ai_decision = {**decided.ai_decision, "kind": "blocked", "note": "failed a capital-protective guardrail"}
    log.info("ai decision: open", extra={"symbol": symbol, "dir": direction.value,
                                         "blocked": decided.direction == Direction.NO_TRADE})
    return decided
