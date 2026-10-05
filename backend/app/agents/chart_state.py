"""What is price DOING right now - pullback, rejection, break, range or impulse.

The engine already answers "which way is the trend?" and "is this a setup?". It never answered the
question a trader actually asks looking at a chart: *what is this candle doing to the last swing?*
A 1h uptrend that is pulling back to value and a 1h uptrend that has just been rejected at
resistance are the same trend and completely different trades.

DESIGN RULES
------------
1. DESCRIPTIVE, not predictive. Every "expected next move" below is expressed as a TRIGGER and an
   INVALIDATION - two prices - not as a forecast. "Resumes up above 84,900, wrong below 83,500" is
   checkable tomorrow; "likely to go up" is not.
2. Pure composition. Everything is built from app/agents/indicators.py; nothing is recomputed.
3. Read-out first. This module does not gate, size or veto. Six ideas in this codebase have died
   out-of-sample, so it ships visible and unwired until a holdout says otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.indicators import (
    adx,
    atr,
    ema,
    failed_break,
    macd,
    market_structure,
    pivot_levels,
    rejection_candle,
    supertrend,
    volume_ratio,
)
from app.core.logging import get_logger
from app.models.schemas import Candle

log = get_logger("agents.chart_state")

# A close this many ATR beyond a level counts as decisive rather than a probe.
_DECISIVE_ATR = 0.25
# "At" a level means within this many ATR of it.
_AT_LEVEL_ATR = 0.45
# A pullback deeper than this (as a fraction of the prior leg) is no longer a pullback.
_MAX_PULLBACK_FRAC = 0.786
# Volume multiple that makes a break look sponsored rather than drifting.
_BREAK_VOL = 1.3


@dataclass
class ChartState:
    """One reading of the current bar in the context of the last swing."""

    state: str = "unclear"           # impulse|pullback|rejection|breakout|breakdown|range|reversal
    trend: str = "sideways"          # the CONTEXT the state sits in
    headline: str = ""
    evidence: list[str] = field(default_factory=list)
    # Where price is inside the move it is retracing (0 = at the start, 1 = full retrace).
    retrace_frac: float | None = None
    depth_atr: float | None = None
    # The two prices that make this checkable rather than an opinion.
    trigger: float | None = None
    invalidation: float | None = None
    expected: str = ""
    target_level: float | None = None
    level_in_play: float | None = None
    # Which way the PATTERN itself resolved. Mechanical, not a forecast: a bearish pin at
    # resistance resolved down, whatever happens next. The engine may use this ONLY to refuse an
    # entry that contradicts it - never to create one. Same discipline as the news gate.
    pattern_side: str | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state, "trend": self.trend, "headline": self.headline,
            "evidence": self.evidence, "retrace_frac": self.retrace_frac,
            "depth_atr": self.depth_atr, "trigger": self.trigger,
            "invalidation": self.invalidation, "expected": self.expected,
            "target_level": self.target_level, "level_in_play": self.level_in_play,
            "pattern_side": self.pattern_side,
        }


def _nearest(levels: list[float], px: float, atr_v: float, mult: float = _AT_LEVEL_ATR):
    """The level price is currently AT, if any."""
    if not levels or not atr_v:
        return None
    near = [lv for lv in levels if abs(lv - px) <= mult * atr_v]
    return min(near, key=lambda lv: abs(lv - px)) if near else None


def _leg(candles: list[Candle], trend: str, lookback: int = 40):
    """The most recent impulse leg (start, end) in the trend's direction.

    A pullback is only meaningful as a fraction of the leg it is retracing, so this finds that leg
    rather than measuring depth against an arbitrary window.
    """
    win = candles[-lookback:]
    if len(win) < 5:
        return None, None
    highs = [c.high for c in win]
    lows = [c.low for c in win]
    if trend == "up":
        hi_i = highs.index(max(highs))
        lo_i = lows.index(min(lows[: hi_i + 1])) if hi_i > 0 else 0
        return lows[lo_i], highs[hi_i]
    if trend == "down":
        lo_i = lows.index(min(lows))
        hi_i = highs.index(max(highs[: lo_i + 1])) if lo_i > 0 else 0
        return highs[hi_i], lows[lo_i]
    return None, None


def _trend_of(candles: list[Candle], adx_v: float | None = None) -> str:
    """Trend context from SuperTrend and the EMA stack.

    When the two DISAGREE - a fresh SuperTrend flip against a slower stack - the old version
    returned "sideways". That is right in chop and badly wrong in a strong move: a market running
    at ADX 68 was labelled sideways, which then routed it into the range branch. So the tie is
    broken by trend STRENGTH: with real ADX behind it, trust the faster read.
    """
    st = supertrend(candles)
    closes = [c.close for c in candles]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    d_st = ("up" if st["dir"] > 0 else "down") if (st and st.get("dir")) else None
    d_ema = ("up" if e20 > e50 else "down") if (e20 is not None and e50 is not None) else None
    if d_st and d_ema:
        if d_st == d_ema:
            return d_st
        return d_st if (adx_v is not None and adx_v >= 25) else "sideways"
    return d_st or d_ema or "sideways"


def read_chart_state(candles: list[Candle], daily: list[Candle] | None = None) -> ChartState:
    """Classify what the latest bar is doing to the last swing.

    Order matters and is the order a trader's eye actually moves: a decisive break beats a wick, a
    wick at a level beats a drift, and only if none of those is true is it a plain pullback.
    """
    s = ChartState()
    if len(candles) < 30:
        s.headline = "not enough history to read the chart"
        return s

    last = candles[-1]
    px = last.close
    atr_v = atr(candles) or 0.0
    if atr_v <= 0:
        s.headline = "no volatility reading"
        return s

    adx_r = adx(candles) or {}
    adx_v = adx_r.get("adx")
    s.trend = _trend_of(candles, adx_v)
    struct = market_structure(candles) or {}
    mac = macd([c.close for c in candles]) or {}
    vol = volume_ratio(candles) or 1.0
    levels = [d["price"] for d in (pivot_levels(candles, px) or []) if d.get("price")]
    # swing_levels returns (SUPPORT, RESISTANCE) - i.e. (low, high). Unpacking it the other way
    # round compared every close against the range LOW, so every instrument read as a breakout.
    # The window also excludes the CURRENT bar: "did this bar break the prior range" is the
    # question, and a window containing the bar itself can never answer it.
    prior_win = candles[-21:-1]
    hi20 = max(c.high for c in prior_win)
    lo20 = min(c.low for c in prior_win)
    at_level = _nearest(levels, px, atr_v)
    s.level_in_play = at_level

    rej = rejection_candle(candles) or {}
    trap = failed_break(candles) or {}
    rej_bear, rej_bull = rej.get("rej_bear", 0.0) == 1.0, rej.get("rej_bull", 0.0) == 1.0
    trap_bear, trap_bull = trap.get("fbreak_bear", 0.0) == 1.0, trap.get("fbreak_bull", 0.0) == 1.0
    # failed_break reports only a flag, so recover the level it swept with the same windows it uses.
    _prior = candles[-24:-4]
    prior_high = max((c.high for c in _prior), default=None)
    prior_low = min((c.low for c in _prior), default=None)
    swept_high = max(c.high for c in candles[-4:])
    swept_low = min(c.low for c in candles[-4:])
    e20 = ema([c.close for c in candles], 20)

    # ---- 1. a decisive break of the 20-bar extreme ----------------------
    broke_up = hi20 is not None and px > hi20 + _DECISIVE_ATR * atr_v
    broke_dn = lo20 is not None and px < lo20 - _DECISIVE_ATR * atr_v
    if broke_up or broke_dn:
        up = broke_up
        s.state = "breakout" if up else "breakdown"
        s.pattern_side = "up" if up else "down"
        ref = hi20 if up else lo20
        s.evidence.append(
            f"closed {abs(px - ref) / atr_v:.2f} ATR {'above' if up else 'below'} the 20-bar "
            f"{'high' if up else 'low'} {ref:.5g}")
        if vol >= _BREAK_VOL:
            s.evidence.append(f"on {vol:.1f}x average activity")
        else:
            s.evidence.append(f"activity only {vol:.1f}x average - unsponsored so far")
        nxt = [x for x in levels if (x > px) == up]
        s.target_level = (min(nxt) if up else max(nxt)) if nxt else None
        s.trigger = ref                      # the level that must now hold on a retest
        s.invalidation = ref - (_DECISIVE_ATR * atr_v if up else -_DECISIVE_ATR * atr_v)
        s.headline = f"Breaking {'up' if up else 'down'} out of the {abs(hi20 - lo20) / atr_v:.1f} ATR range"
        s.expected = (
            f"Continuation while price holds above {ref:.5g}"
            if up else f"Continuation while price holds below {ref:.5g}")
        s.expected += (f", next level {s.target_level:.5g}." if s.target_level
                       else ", no mapped level ahead.")
        s.expected += " A close back inside is a failed break - the trap, not the trade."
        return s

    # ---- 2. range: below this trend strength, every pattern inside it is noise ---------------------------------------------------------
    # Low trend STRENGTH is what defines a range. Keying this off the trend LABEL as well meant an
    # ADX-68 market could be called sideways by the tie-break above and then printed as "ranging".
    if (adx_v is not None and adx_v < 20) or (adx_v is None and s.trend == "sideways"):
        s.state = "range"
        if hi20 and lo20:
            width = (hi20 - lo20) / atr_v
            pos = (px - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
            s.evidence.append(f"ADX {adx_v:.0f} - no trend" if adx_v is not None else "no trend")
            s.evidence.append(f"{width:.1f} ATR range, price {pos:.0%} of the way up it")
            s.trigger = hi20
            s.invalidation = lo20
            s.target_level = lo20 if pos > 0.5 else hi20
            s.headline = f"Ranging between {lo20:.5g} and {hi20:.5g}"
            s.expected = (
                f"Rotation toward {s.target_level:.5g} while the range holds. A decisive close "
                f"outside {lo20:.5g}-{hi20:.5g} is the change worth acting on - inside it, the "
                "edges are the trade and the middle is noise.")
        else:
            s.headline = "Ranging - no clean edges yet"
        return s

    # ---- 3. a failed break (the trap) -----------------------------------
    if trap_bear or trap_bull:
        up_trap = trap_bear                          # swept highs, closed back inside
        trap_level = prior_high if up_trap else prior_low
        trap_extreme = swept_high if up_trap else swept_low
        s.state = "reversal"
        s.pattern_side = "down" if up_trap else "up"
        s.evidence.append(
            f"swept the {'highs' if up_trap else 'lows'} at {trap_level or px:.5g} and closed "
            "back inside - the breakout buyers are trapped"
            if up_trap else
            f"swept the lows at {trap_level or px:.5g} and closed back inside - the breakdown "
            "sellers are trapped")
        s.trigger = trap_level
        s.invalidation = trap_extreme
        opp = [x for x in levels if (x < px) == up_trap]
        s.target_level = (max(opp) if up_trap else min(opp)) if opp else (lo20 if up_trap else hi20)
        s.headline = f"Failed break {'above' if up_trap else 'below'} {trap_level or px:.5g}"
        s.expected = (
            f"Rotation back {'down' if up_trap else 'up'} toward "
            f"{s.target_level:.5g}" if s.target_level else "Rotation back inside the range")
        s.expected += (f"; wrong if price reclaims {s.invalidation:.5g}."
                       if s.invalidation else ".")
        return s

    # ---- 4. rejection at a level ----------------------------------------
    if (rej_bear or rej_bull) and at_level is not None:
        down_rej = rej_bear                          # upper wick / bearish engulf = rejected above
        s.state = "rejection"
        s.pattern_side = "down" if down_rej else "up"
        s.evidence.append(
            f"{'bearish' if down_rej else 'bullish'} rejection candle (pin or engulfing) at "
            f"{at_level:.5g}")
        if s.trend != "sideways":
            s.evidence.append(f"inside an {s.trend}trend on the entry timeframe" if s.trend == "up" else f"inside a {s.trend}trend on the entry timeframe")
        s.trigger = last.low if down_rej else last.high
        s.invalidation = last.high if down_rej else last.low
        opp = [x for x in levels if (x < px) == down_rej]
        s.target_level = (max(opp) if down_rej else min(opp)) if opp else None
        s.headline = f"Rejected {'from' if down_rej else 'off'} {at_level:.5g}"
        s.expected = (
            f"Move {'down' if down_rej else 'up'} while {at_level:.5g} holds"
            + (f", toward {s.target_level:.5g}" if s.target_level else "")
            + f"; a close beyond {s.invalidation:.5g} says the level is failing.")
        return s

    # ---- 4. pullback inside a trend -------------------------------------
    # "Pullback" and "reversal" only mean something INSIDE a trend. Without the strength guard,
    # chop produced a noise-sized leg, a >78% retrace of it, and the word "reversal" - which is
    # the most misleading thing this module could say about a flat market.
    if s.trend in ("up", "down") and (adx_v is None or adx_v >= 20):
        start, end = _leg(candles, s.trend)
        # A leg has to be an impulse, not two noisy bars: 0.3 ATR admitted pure noise.
        if start is not None and end is not None and abs(end - start) > 1.2 * atr_v:
            leg = abs(end - start)
            retr = (end - px) / leg if s.trend == "up" else (px - end) / leg
            against = retr > 0.05
            if against and retr <= _MAX_PULLBACK_FRAC:
                s.state = "pullback"
                s.pattern_side = s.trend          # a pullback leans toward RESUMPTION
                s.retrace_frac = round(max(0.0, retr), 3)
                s.depth_atr = round(abs(end - px) / atr_v, 2)
                s.evidence.append(
                    f"retraced {s.retrace_frac:.0%} of the {leg / atr_v:.1f} ATR leg "
                    f"({s.depth_atr} ATR deep)")
                if e20 is not None and abs(px - e20) <= 0.6 * atr_v:
                    s.evidence.append("back at the 20-EMA - the value area of the trend")
                if mac.get("hist") is not None:
                    flat = abs(mac["hist"]) < 0.1 * atr_v
                    s.evidence.append(
                        "momentum flat - nothing pushing it either way" if flat
                        else f"MACD histogram {mac['hist']:+.5g}")
                if struct.get("choch"):
                    s.evidence.append("a change-of-character is already printed - treat with care")
                # A pullback resumes on a reclaim of the last minor extreme, and dies at the leg's
                # origin. Both are prices, so tomorrow can settle which happened.
                s.trigger = max(c.high for c in candles[-5:]) if s.trend == "up" \
                    else min(c.low for c in candles[-5:])
                s.invalidation = start
                s.target_level = end
                s.headline = (
                    f"Pullback in an uptrend - {s.retrace_frac:.0%} back"
                    if s.trend == "up" else
                    f"Pullback in a downtrend - {s.retrace_frac:.0%} back")
                deep = "shallow" if retr < 0.382 else "normal" if retr < 0.618 else "deep"
                s.expected = (
                    f"A {deep} pullback. Resumption {'up' if s.trend == 'up' else 'down'} "
                    f"confirms above {s.trigger:.5g}" if s.trend == "up" else
                    f"A {deep} pullback. Resumption down confirms below {s.trigger:.5g}")
                s.expected += (
                    f"; the leg fails if price takes out {s.invalidation:.5g}. "
                    f"Prior swing at {end:.5g} is the first objective.")
                return s
            if against and retr > _MAX_PULLBACK_FRAC:
                s.state = "reversal"
                s.pattern_side = "down" if s.trend == "up" else "up"
                s.retrace_frac = round(retr, 3)
                s.evidence.append(f"retraced {retr:.0%} of the leg - past a pullback")
                s.trigger = start
                s.invalidation = end
                s.headline = f"Retracement is deep enough to be a reversal, not a pullback"
                s.expected = (
                    f"The {s.trend} leg is in question below {start:.5g}. Treat continuation as "
                    f"unproven until price reclaims {end:.5g}.")
                return s

    # ---- 6. impulse / continuation ---------------------------------------
    s.state = "impulse"
    s.pattern_side = s.trend if s.trend != "sideways" else None
    up = s.trend == "up"
    s.evidence.append(f"trending {s.trend}" + (f", ADX {adx_v:.0f}" if adx_v is not None else ""))
    if e20 is not None:
        s.depth_atr = round(abs(px - e20) / atr_v, 2)
        s.evidence.append(f"{s.depth_atr} ATR from the 20-EMA")
        if s.depth_atr > 2.0:
            s.evidence.append("stretched - chasing here pays the worst price of the move")
    nxt = [x for x in levels if (x > px) == up]
    s.target_level = (min(nxt) if up else max(nxt)) if nxt else None
    s.trigger = px
    s.invalidation = e20 if e20 is not None else (lo20 if up else hi20)
    # Reachable with trend == "sideways" when ADX is high but SuperTrend and the EMA stack still
    # disagree - an expansion with no agreed direction. "Trending sideways" is nonsense; say what
    # it actually is.
    s.headline = (f"Trending {s.trend} - extension, not a pullback" if s.trend != "sideways"
                  else "Expanding with no agreed direction - the two trend reads disagree")
    s.expected = (
        (f"Continuation toward {s.target_level:.5g}" if s.target_level else "Continuation")
        + (f" while price holds {'above' if up else 'below'} the 20-EMA at {s.invalidation:.5g}."
           if s.invalidation else ".")
        + " Better entries come from the next pullback than from here.")
    return s
