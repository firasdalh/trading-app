"""Orchestrator / Decision agent.

Acts like a disciplined head trader: requires confluence between the technical and
fundamental reads, sits out unclear or conflicting setups, and refuses to trade into
high-impact news windows the Fundamental Analyst flagged. Output is a ``TradeProposal``
that may be ``NO_TRADE`` — declining is a valid, encouraged result.

LLM-driven when a key is configured; otherwise a deterministic confluence rule runs so the
pipeline works offline. The deterministic path is conservative by design.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.enums import AssetClass, Direction, ReviewDecision, TradingBias
from app.models.schemas import (
    ConditionalSuggestion,
    FundamentalRead,
    TechnicalRead,
    TradeProposal,
    TradeReviewLLM,
)

log = get_logger("agents.orchestrator")

_REVIEW_SYSTEM = """You are a trader with 30 years on institutional desks (FX, indices, commodities,
crypto). You have survived many cycles BECAUSE you are selective: you pass on most setups and only
back the high-quality ones. You are now REVIEWING a setup the deterministic strategy produced.

Your authority is strictly limited (non-negotiable):
- You may ONLY confirm or veto. You CANNOT create a trade, flip its direction, move the
  entry/stop/target, or increase size. Sizing and the hard risk limits are handled by a separate
  deterministic Risk Manager downstream. You may only CONFIRM, VETO, or LOWER the confidence.

Judge the setup against a professional checklist, then grade it A / B / C:
1. TREND & STRUCTURE — Is the trade WITH the dominant higher-timeframe trend and market structure
   (higher highs/lows for longs, lower highs/lows for shorts)? Counter-trend trades start at C.
2. LOCATION — Is entry at VALUE (a pullback to a moving average / prior structure / support-
   resistance) or is it CHASING an extended move far from the mean? Buying highs / selling lows
   into a stretched RSI is a classic amateur error — penalise it hard.
3. MOMENTUM — Does momentum (MACD/ADX/DI) CONFIRM the entry, or is it diverging/rolling over?
   A trade needs the move to already be working, not hoped for.
4. REWARD:RISK vs REAL STRUCTURE — Is there a clean path of at least ~2R to target BEFORE price
   runs into opposing structure (support for shorts, resistance for longs)? If the obvious level
   sits between entry and target, R:R is not real.
5. EVENT & LIQUIDITY RISK — Imminent high-impact event (stand_aside_windows), thin/illiquid
   conditions, or a market with no trend (low ADX / ranging) — stand aside.
6. CONVICTION — Confirm A and B setups. For a MARGINAL setup, CONFIRM it at LOWER confidence
   rather than veto — let the confidence score and the risk limits do the filtering, not a hard
   reject. Reserve VETO for a CLEAR, specific flaw: counter-trend, no real R:R (an obvious level
   sits between entry and target), an imminent high-impact event, or no trend at all. When merely
   unsure, confirm at lower confidence.

Be decisive but not trigger-happy: a vague feeling is not a veto; a concrete flaw is. Vetoing every
borderline setup is its own failure — it means the desk never trades.
In `rationale`, state the GRADE (A/B/C) and the one or two factors that drove the decision, like a
desk head explaining a call. In `concerns`, list the specific risks. Set `confidence` to your honest
conviction (0-1) — modest for B, high for A, and you will be vetoing C.
Return strict JSON: decision ("confirm"|"veto"), confidence (0-1), rationale, concerns[]."""


def _now_in_stand_aside(fundamental: FundamentalRead, now: datetime) -> bool:
    for w in fundamental.stand_aside_windows:
        start = w.start if w.start.tzinfo else w.start.replace(tzinfo=timezone.utc)
        end = w.end if w.end.tzinfo else w.end.replace(tzinfo=timezone.utc)
        if start <= now <= end:
            return True
    return False


def _last_close(technical: TechnicalRead) -> float | None:
    for tf in technical.timeframes:
        if "last_close" in tf.indicators:
            return tf.indicators["last_close"]
    return None


# Indicator gates for the deterministic decision.
_ADX_MIN = 20.0       # below this the market is ranging -> stand aside
_ADX_STRONG = 25.0
_REGIME_VOL_EXPANSION = 1.6  # recent ATR >= 1.6x its baseline = volatility expansion (regime shift)
_REGIME_VOL_EXTREME = 2.2    # a sharp vol blow-off WITHOUT a strong trend -> stand aside (whipsaw)
_ATR_STOP_MULT = 1.5  # protective stop = entry +/- 1.5 * ATR (forex/metal/index/stock/energy)
_ATR_STOP_MULT_CRYPTO = 2.5  # crypto is far more volatile — a tight stop just gets wicked out
_MIN_STOP_ATR_FRAC = 1.0  # never place the stop closer than 1xATR (anti-wick floor): structure-
                          # tightening must not pull the stop inside this, or normal noise stops us
_STRUCT_STOP_BUFFER_ATR = 0.2  # place the structural stop this far BEYOND the swing (wick allowance)
_STRUCT_STOP_MAX_ATR = 3.0     # if the invalidating swing is further than this, it's not a practical stop
_RR = 2.0             # reward:risk target (baseline / fallback)
_RR_MAX = 4.0         # cap the planned target at 4R so a far key level isn't an unrealistic target
_MIN_RR_COND = 1.5    # only suggest a conditional break-entry if its R:R (from the trigger) clears this
_MIN_RR_ENTRY = 1.5   # don't TAKE a direct market entry below this R:R — ~1:1 is negative expectancy
                      # after costs; stand aside and arm the better-priced break/pullback instead
_RSI_OB = 75.0        # overbought / oversold caution thresholds
_RSI_OS = 25.0
# --- ranging-market mean-reversion (fade the range edges instead of trend-trading a flat market) ---
_MR_EDGE_ATR = 0.6    # within this many ATR of a range edge counts as "at the edge"
_MR_STOP_ATR = 0.6    # protective stop sits this many ATR beyond the edge
_MR_MIN_RR = 1.0      # need >=1R from the edge back to the mean to bother fading
_MR_CONF_CAP = 0.68   # cap mean-reversion confidence below strong-trend setups (lower-conviction edge)
# Range fades use a LOOSER RSI band than the trend-overextension thresholds (75/25): in a range RSI
# oscillates ~35-65 and rarely reaches trend extremes (more so with Wilder's smoother RSI), so a
# 75/25 gate would keep the fade dormant. You fade the EDGE of a range before RSI hits trend levels.
_MR_RSI_OB = 66.0
_MR_RSI_OS = 34.0
_STRUCT_IGNORE = 0.5  # ignore overhead structure within 0.5R of entry (breakout zone)
_MOM_ATR_FRAC = 0.10  # counter-momentum only "matters" when |MACD hist| >= 10% of ATR (noise gate)
_PULLBACK_ATR = 2.5   # price > this many ATR beyond EMA20 = stretched entry -> down-weight (a
                      # steady trend rides ~2.4 ATR from the lagging EMA, so only flag real spikes)
_VALUE_ENTRY_ATR = 1.0  # entry within ~1 ATR of the 20-EMA = a pullback to VALUE -> a pro's
                        # preferred trend entry (tight risk to the swing, lots of room to target)
_STRETCHED_ATR = 1.5    # 1.5-2.5 ATR from value = getting stretched (small anti-chase penalty);
                        # beyond _PULLBACK_ATR it's a full chase (bigger penalty) — see grading below

# --- 15m SCALPING (SCMS: Structure-Confirmed Momentum Scalp) — kept deliberately PARSIMONIOUS to
# avoid overfitting: a tiny set of robust, orthogonal conditions (trend+MTF alignment, a pullback to
# value that held, momentum confirmation) behind hard regime+session gates. No indicator soup. ---
_SCALP_VALUE_ATR = 0.30   # "pulled back to value" = the bar tagged within 0.30xATR of the EMA20
_SCALP_MIN_RR = 1.3       # minimum reward:risk for a scalp (a touch below swing trades; banked at 1R)
_SCALP_STOP_MAX_ATR = 2.0 # a scalp's structural stop must be <= 2xATR — wider isn't a scalp, skip it
_SCALP_TP_FIXED_RR = 1.5  # fallback target when there's no clean opposing level in range

_TF_RANK = {"1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "4h": 6, "1d": 7}


def _trend_from_indicators(ind: dict, fallback: str = "sideways") -> str:
    """Derive trend from the COMPUTED EMAs (numbers), independent of any LLM text label.

    This keeps the deterministic strategy the source of truth even when an LLM produced the
    technical interpretation. Falls back to the provided label when EMAs aren't available.
    """
    e20, e50, e200 = ind.get("ema20"), ind.get("ema50"), ind.get("ema200")
    if e20 is not None and e50 is not None:
        if e200 is not None:
            if e20 > e50 > e200:
                return "up"
            if e20 < e50 < e200:
                return "down"
        if e20 > e50:
            return "up"
        if e20 < e50:
            return "down"
    return fallback


def _macro_tf(technical: TechnicalRead):
    """The highest-timeframe read available (the dominant context), or None."""
    best, best_rank = None, -1
    for tf in technical.timeframes:
        r = _TF_RANK.get(tf.timeframe, 0)
        if r > best_rank:
            best, best_rank = tf, r
    return best


def _macro_trend(technical: TechnicalRead) -> str:
    """Trend of the highest-timeframe read (the dominant context), from computed indicators."""
    best = _macro_tf(technical)
    if best is None:
        return "sideways"
    return _trend_from_indicators(best.indicators, best.trend)


def _regime(ind: dict) -> str:
    """The market regime a senior trader reads FIRST: trending / ranging / volatile / moderate.

    ADX measures trend strength; ``vol_atr_ratio`` (recent ATR vs a longer baseline) flags a
    volatility expansion. A strong trend is "trending" even while volatility expands (a breakout);
    volatility expanding WITHOUT a strong trend is "volatile" — the whipsaw zone a pro avoids.
    """
    adx = ind.get("adx")
    vr = ind.get("vol_atr_ratio")
    if adx is not None and adx >= _ADX_STRONG:
        return "trending"
    if vr is not None and vr >= _REGIME_VOL_EXPANSION:
        return "volatile"
    if adx is not None and adx < _ADX_MIN:
        return "ranging"
    return "moderate"


# Explicit, single-source regime -> strategy policy (the "what do we do in this regime?" table the
# rest of the engine and the UI read, instead of scattered ad-hoc checks).
_REGIME_POLICY = {
    "trending": {"strategy": "trend", "note": "Trend continuation — trade with the trend."},
    "moderate": {"strategy": "trend", "note": "Mild trend — trend continuation, lighter conviction."},
    "ranging":  {"strategy": "mean_reversion", "note": "No trend — fade the range edges to the mean."},
    "volatile": {"strategy": "stand_aside", "note": "Volatility expanding without a trend — whipsaw; stand aside."},
}


def regime_policy(regime: str) -> dict:
    """The strategy a given regime permits: 'trend' / 'mean_reversion' / 'stand_aside'."""
    return _REGIME_POLICY.get(regime, {"strategy": "trend", "note": ""})


def _nearest_above(tf0, ind: dict, price: float) -> float | None:
    """Nearest resistance ABOVE price (technical levels, fallback the last swing high)."""
    cands = [l for l in ((tf0.resistance_levels if tf0 else []) or []) if l and l > price]
    sh = ind.get("swing_high")
    if sh and sh > price:
        cands.append(sh)
    return min(cands) if cands else None


def _nearest_below(tf0, ind: dict, price: float) -> float | None:
    """Nearest support BELOW price (technical levels, fallback the last swing low)."""
    cands = [l for l in ((tf0.support_levels if tf0 else []) or []) if l and l < price]
    sl = ind.get("swing_low")
    if sl and sl < price:
        cands.append(sl)
    return max(cands) if cands else None


def _mean_reversion_decision(base: TradeProposal, ind: dict, tf0, macro: str = "sideways") -> TradeProposal:
    """RANGING regime: there's no trend to follow, so FADE the range edges back to the mean (EMA20)
    — the pro's range play. Short a tag of resistance while overbought; long a tag of support while
    oversold; stop just beyond the edge; target the mean. Confidence is capped below trend setups
    (lower-conviction edge). Returns NO_TRADE when price isn't at an edge with RSI confirmation.

    ``macro`` is the higher-timeframe trend: a fade AGAINST a clear higher-TF trend is refused — a
    low-ADX patch inside a daily uptrend/downtrend is a PULLBACK, not a range, and fading it gets run
    over (backtest: USDJPY ranging shorts into a 1d uptrend were 0/11)."""
    base.regime = "ranging"
    base.strategy = "mean_reversion"
    atr = ind.get("atr14")
    rsi = ind.get("rsi14")
    price = ind.get("last_close")
    mean = ind.get("ema20")
    if not (atr and price and mean) or atr <= 0:
        base.rationale = "Ranging, but no usable ATR/price/mean for a fade — sitting out."
        return base

    res = _nearest_above(tf0, ind, price)
    sup = _nearest_below(tf0, ind, price)
    near = _MR_EDGE_ATR * atr
    last_high = ind.get("last_high")
    last_low = ind.get("last_low")
    rsi_prev = ind.get("rsi14_prev")
    bb_up = ind.get("bb_upper")
    bb_lo = ind.get("bb_lower")

    # The range EDGE the bar TAGGED then closed back inside (a rejection) — read TWO ways a pro reads
    # "price reached the edge of the range": a structural level (pivot S/R or swing) OR the Bollinger
    # band. Either one, with RSI in the (looser-than-trend) fade band, qualifies a fade to the mean.
    up_edges: list[float] = []
    if res is not None and (res - price) <= near and last_high is not None and last_high >= res:
        up_edges.append(res)
    if bb_up is not None and last_high is not None and last_high >= bb_up and price < bb_up:
        up_edges.append(bb_up)
    dn_edges: list[float] = []
    if sup is not None and (price - sup) <= near and last_low is not None and last_low <= sup:
        dn_edges.append(sup)
    if bb_lo is not None and last_low is not None and last_low <= bb_lo and price > bb_lo:
        dn_edges.append(bb_lo)

    direction = stop = target = edge = None
    edge_kind = ""
    if up_edges and rsi is not None and rsi >= _MR_RSI_OB and mean < price:
        direction = Direction.SHORT
        edge = max(up_edges)
        edge_kind = "resistance" if edge == res else "upper Bollinger band"
        stop = max(edge, last_high) + _MR_STOP_ATR * atr
        target = mean                       # revert to the mean
        risk, reward = stop - price, price - target
    elif dn_edges and rsi is not None and rsi <= _MR_RSI_OS and mean > price:
        direction = Direction.LONG
        edge = min(dn_edges)
        edge_kind = "support" if edge == sup else "lower Bollinger band"
        stop = min(edge, last_low) - _MR_STOP_ATR * atr
        target = mean
        risk, reward = price - stop, target - price
    else:
        base.rationale = ("Ranging — waiting for a rejection at a range edge (a structural level or "
                          "the Bollinger band) with RSI in the fade band, before fading to the mean. "
                          "No confirmed rejection yet.")
        return base

    # Higher-timeframe trend guard: never fade AGAINST a clear higher-TF trend. A 1h "range" inside a
    # daily uptrend is a pullback (short the resistance edge here and the trend runs you over); the
    # same downtrend makes a long fade a falling-knife. Only fade WITH or neutral to the higher TF.
    if (direction == Direction.SHORT and macro == "up") or (direction == Direction.LONG and macro == "down"):
        base.rationale = (f"Ranging fade skipped: a {direction.value} would fade AGAINST the higher-"
                          f"timeframe {macro}trend — that's a pullback in a trend, not a range. Sitting out.")
        return base

    if risk <= 0 or reward <= 0 or (reward / risk) < _MR_MIN_RR:
        base.rationale = (f"Ranging fade skipped: reward to the mean is too thin "
                          f"(R:R {round(reward / risk, 2) if risk > 0 else 0} < {_MR_MIN_RR}).")
        return base

    rr = reward / risk
    # Confidence: a fade is lower-conviction than a trend trade. Reward a stretched RSI and a clean
    # R:R, but cap it so a range play never outranks a strong-trend setup in the Hybrid selection.
    conf = 0.5
    if (direction == Direction.SHORT and rsi >= _MR_RSI_OB + 8) or (direction == Direction.LONG and rsi <= _MR_RSI_OS - 8):
        conf += 0.1
    # RSI also TURNING back from the extreme (not just at it) = a stronger rejection.
    if rsi_prev is not None and ((direction == Direction.SHORT and rsi < rsi_prev)
                                 or (direction == Direction.LONG and rsi > rsi_prev)):
        conf += 0.05
    if rr >= 1.5:
        conf += 0.05
    confidence = round(min(_MR_CONF_CAP, conf), 2)

    base.direction = direction
    base.entry = round(price, 6)
    base.stop_loss = round(stop, 6)
    base.take_profit = round(target, 6)
    base.confidence = confidence
    base.rationale = (
        f"Mean-reversion {direction.value.upper()} (ranging regime): price tagged the {edge_kind} "
        f"{round(edge, 5)} with RSI {rsi} ({'high' if direction == Direction.SHORT else 'low'}); "
        f"fading back to the mean (EMA20 {round(mean, 5)}). Stop beyond the edge; R:R {round(rr, 2)}."
    )
    return base


def _scalp_decision(base: TradeProposal, ind: dict, tf0, macro: str, regime: str,
                    asset_class: AssetClass, symbol: str, now: datetime) -> TradeProposal:
    """15m SCALP strategy (SCMS) — deliberately simple to resist overfitting.

    The ONLY setup is a trend-pullback continuation: with the 15m trend (and not against the higher
    timeframe), wait for a pullback that TAGGED value (the EMA20) and HELD, with momentum (MACD hist)
    confirming the trend direction. Hard gates first: trade only in a directional regime (trending /
    moderate — 15m's profitable buckets) and a liquid session (skip thin hours). Stop beyond the
    pullback extreme (anti-wick floor, capped tight); target the nearest opposing structure (>= min
    R:R) or a fixed 1.5R. Returns NO_TRADE/watch when any condition fails (which is most of the time —
    that's the point). The live spread filter + scalp risk profile are layered on downstream."""
    base.strategy = "scalp"
    base.regime = regime
    atr = ind.get("atr14")
    price = ind.get("last_close")
    ema20 = ind.get("ema20")
    if not (atr and price and ema20) or atr <= 0:
        base.rationale = "Scalp: no usable ATR/price/EMA20 — sitting out."
        return base

    # --- hard gates ---
    if regime not in ("trending", "moderate"):
        base.watch = True
        base.rationale = f"Scalp: standing aside — {regime} regime (no clean 15m directional energy)."
        return base
    session_q, session_note = _session_quality(asset_class, symbol, now)
    if session_q == "thin":
        base.watch = True
        base.rationale = f"Scalp: standing aside — thin/illiquid session ({session_note})."
        return base

    trend = _trend_from_indicators(ind)
    ema50 = ind.get("ema50")
    rsi, rsi_prev = ind.get("rsi14"), ind.get("rsi14_prev")
    macd = ind.get("macd_hist")
    last_high, last_low = ind.get("last_high"), ind.get("last_low")
    swing_high, swing_low = ind.get("swing_high"), ind.get("swing_low")
    band = _SCALP_VALUE_ATR * atr
    rising = rsi is not None and rsi_prev is not None and rsi > rsi_prev   # momentum turning UP
    falling = rsi is not None and rsi_prev is not None and rsi < rsi_prev  # momentum turning DOWN

    direction = stop = risk = None
    if trend == "up" and macro != "down" and ema50 is not None:
        # Price pulled back into the VALUE ZONE (>= EMA50, <= just above EMA20 — trend intact, not
        # extended) and RSI is turning back UP from the pullback (not yet overbought) — momentum
        # resuming with the trend. A multi-bar pullback read, not a brittle single-bar wick.
        if ema50 <= price <= ema20 + band and rising and rsi < 60:
            direction = Direction.LONG
            ref = swing_low if (swing_low is not None and swing_low < price) else last_low
            stop = (ref if ref is not None else ema50) - _STRUCT_STOP_BUFFER_ATR * atr
            risk = price - stop
    elif trend == "down" and macro != "up" and ema50 is not None:
        if ema20 - band <= price <= ema50 and falling and rsi > 40:
            direction = Direction.SHORT
            ref = swing_high if (swing_high is not None and swing_high > price) else last_high
            stop = (ref if ref is not None else ema50) + _STRUCT_STOP_BUFFER_ATR * atr
            risk = stop - price

    if direction is None or risk is None:
        base.watch = True
        base.rationale = "Scalp: waiting for a pullback-to-value that holds with momentum in the 15m trend."
        return base

    # Stop sanity: never tighter than the anti-wick floor, never wider than a scalp's cap.
    min_stop = _MIN_STOP_ATR_FRAC * atr
    if risk < min_stop:
        stop = price - min_stop if direction == Direction.LONG else price + min_stop
        risk = abs(price - stop)
    if risk <= 0 or risk > _SCALP_STOP_MAX_ATR * atr:
        base.watch = True
        base.rationale = (f"Scalp: invalidation stop too wide ({risk / atr:.1f}xATR > "
                          f"{_SCALP_STOP_MAX_ATR:.0f}) — not a scalp here.")
        return base

    # Target: nearest opposing structure if it clears the min R:R (capped at _RR_MAX), else a fixed
    # 1.5R. If a level sits closer than the min R:R, the path is blocked — skip.
    opp = _nearest_above(tf0, ind, price) if direction == Direction.LONG else _nearest_below(tf0, ind, price)
    if opp is not None:
        rr_opp = (opp - price) / risk if direction == Direction.LONG else (price - opp) / risk
        if rr_opp < _SCALP_MIN_RR:
            base.watch = True
            base.rationale = (f"Scalp: nearest level {round(opp, 5)} is only {rr_opp:.1f}R away "
                              f"(< {_SCALP_MIN_RR}) — no room. Standing aside.")
            return base
        cap = _RR_MAX * risk
        target = min(opp, price + cap) if direction == Direction.LONG else max(opp, price - cap)
    else:
        target = price + _SCALP_TP_FIXED_RR * risk if direction == Direction.LONG else price - _SCALP_TP_FIXED_RR * risk
    reward = abs(target - price)
    rr = reward / risk

    # Confidence: base + a few orthogonal bonuses (kept small to avoid over-tuning).
    conf = 0.5
    if macro == trend:                       # higher timeframe in the same direction
        conf += 0.10
    if session_q == "active":                # prime liquidity window
        conf += 0.10
    if macd is not None and abs(macd) >= _MOM_ATR_FRAC * atr:  # momentum is meaningful, not noise
        conf += 0.05
    vol = ind.get("vol_ratio")
    if vol is not None and vol > 1.1:        # participation behind the move
        conf += 0.05
    if rr >= 2.0:
        conf += 0.05
    confidence = round(min(0.9, conf), 2)

    base.direction = direction
    base.entry = round(price, 6)
    base.stop_loss = round(stop, 6)
    base.take_profit = round(target, 6)
    base.confidence = confidence
    base.rationale = (
        f"Scalp {direction.value.upper()} (15m {regime}, {session_q} session): pullback to value "
        f"(EMA20 {round(ema20, 5)}) with RSI turning ({rsi_prev}->{rsi}); entry {base.entry}, stop "
        f"{base.stop_loss} ({risk / atr:.1f}xATR), target {base.take_profit} (R:R {round(rr, 2)})."
    )
    return base


def _session_quality(asset_class: AssetClass, symbol: str, now: datetime) -> tuple[str, str]:
    """Liquidity quality of the current trading session: 'active' / 'normal' / 'thin'.

    A pro leans into the liquid windows (real participation behind a move) and is wary of thin
    hours where spreads widen and moves are noise. Times are UTC. Crypto is 24/7 (always normal).
    """
    h = now.hour
    s = symbol.upper()
    if asset_class == AssetClass.CRYPTO:
        return "normal", "24/7"
    if asset_class == AssetClass.INDEX:
        if any(k in s for k in ("US500", "US30", "USTEC", "US2000", "NAS", "SPX", "NDX")):
            return ("active", "US cash session") if 13 <= h < 20 else ("thin", "outside US cash")
        if any(k in s for k in ("DE", "UK100", "FR40", "STOXX", "EU")):
            return ("active", "EU session") if 7 <= h < 16 else ("thin", "outside EU session")
        if any(k in s for k in ("JP225", "HK50", "AUS200", "IN50")):
            return ("active", "Asian session") if 0 <= h < 8 else ("thin", "outside Asian session")
        return "normal", ""
    # FX / metals / energy: London 07-16, NY 12-21 UTC; the 12-16 overlap is peak liquidity.
    if 12 <= h < 16:
        return "active", "London-NY overlap (peak liquidity)"
    if 7 <= h < 21:
        return "normal", "London/NY hours"
    if asset_class == AssetClass.FOREX and any(c in s for c in ("JPY", "AUD", "NZD")):
        return "normal", "Asian session (JPY/AUD/NZD)"
    return "thin", "thin hours (post-NY / pre-London)"


def _structure_label(ind: dict) -> str:
    """Market structure (swing highs/lows) encoded in the indicators: 1=up / -1=down / 0=range."""
    s = ind.get("structure")
    if s is None:
        return "range"
    return "up" if s > 0.5 else "down" if s < -0.5 else "range"


def _macro_structure(technical: TechnicalRead) -> str:
    """Market structure of the highest-timeframe read (the dominant chart context)."""
    best = _macro_tf(technical)
    return _structure_label(best.indicators) if best else "range"


def _round_levels(price: float) -> list[float]:
    """Psychological round-number magnets straddling the price (magnitude-scaled to the instrument:
    ~1-10% step, e.g. 580/590 for ~588, 1.10/1.20 for ~1.1, 24000/25000 for ~24k)."""
    if price <= 0:
        return []
    step = 10 ** (math.floor(math.log10(price)) - 1)
    base = math.floor(price / step) * step
    return [round(base, 6), round(base + step, 6)]


def _key_levels(technical: TechnicalRead, tf0, entry: float) -> list[float]:
    """Every confluence level a desk actually watches, deduped + sorted: pivot S/R (entry TF),
    prior day/week high-low (institutional, from the daily TF), and round numbers."""
    raw: list[float] = []
    if tf0:
        raw += list(tf0.support_levels[:1]) + list(tf0.resistance_levels[:1])
    macro = _macro_tf(technical)
    if macro:
        for k in ("prior_day_high", "prior_day_low", "prior_week_high", "prior_week_low"):
            v = macro.indicators.get(k)
            if v:
                raw.append(v)
    raw += _round_levels(entry)
    tol = abs(entry) * 5e-4  # merge levels within ~0.05% of each other
    out: list[float] = []
    for lv in sorted(x for x in raw if x and x > 0):
        if not out or (lv - out[-1]) > tol:
            out.append(round(lv, 6))
    return out


def _conditional_break(
    direction: Direction, entry: float, atr_v: float | None, levels: list[float],
    target: float, confidence: float, ind: dict | None = None,
) -> ConditionalSuggestion | None:
    """If a key level sits BETWEEN entry and target (blocking the path), suggest a break-entry: a
    stop order just beyond that level, stop on the other side of it, target = the original level.
    R:R is recomputed FROM the trigger so it's honest; returns None if no blocker or R:R too thin.

    This is the 'wait for the break' play a pro uses instead of chasing into structure (the UKOILm
    case: short only once the 78.21 support cluster gives way).

    ``ind`` (indicators) enables the FAILED-BREAK / reclaim guard: don't arm a break of a level that
    price has ALREADY pierced and reclaimed in the recent window (a bull/bear trap) — the XAGGBP
    case where a broken-then-reclaimed 47 got re-shorted into repeated stops."""
    if not atr_v or atr_v <= 0 or not levels or target <= 0 or entry <= 0:
        return None
    buf = max(0.1 * atr_v, entry * 5e-4)          # trigger/stop offset beyond the level (wick allowance)
    stop_pad = max(0.5 * atr_v, buf)
    if direction == Direction.LONG:
        blocks = [lv for lv in levels if entry < lv < target]
        if not blocks:
            return None
        block = min(blocks)                        # nearest overhead resistance on the path
        trigger = round(block + buf, 6)
        stop = round(block - stop_pad, 6)          # below the broken resistance (now support)
        tp = round(target, 6)
        risk = trigger - stop
        if risk <= 0 or tp <= trigger:
            return None
        rr = (tp - trigger) / risk
        order_type = "buy_stop"
    elif direction == Direction.SHORT:
        blocks = [lv for lv in levels if target < lv < entry]
        if not blocks:
            return None
        block = max(blocks)                        # nearest support on the path down
        trigger = round(block - buf, 6)
        stop = round(block + stop_pad, 6)          # above the broken support (now resistance)
        tp = round(target, 6)
        risk = stop - trigger
        if risk <= 0 or tp >= trigger:
            return None
        rr = (trigger - tp) / risk
        order_type = "sell_stop"
    else:
        return None
    if rr < _MIN_RR_COND:
        return None
    # Failed-break / reclaim guard: if price has ALREADY pierced this level and traded back to the
    # original side within the recent window, it's a whipsaw/trap, not a clean barrier — don't arm a
    # break of it. (Short break of support: recent low dipped below the level but price is back above
    # it. Long break of resistance: recent high spiked above but price is back below.)
    if ind is not None:
        recent_low, recent_high = ind.get("recent_low"), ind.get("recent_high")
        if direction == Direction.SHORT and recent_low is not None and recent_low < block:
            return None
        if direction == Direction.LONG and recent_high is not None and recent_high > block:
            return None
    return ConditionalSuggestion(
        order_type=order_type, trigger_price=trigger, stop_loss=stop, take_profit=tp,
        confidence=round(confidence, 2), rr=round(rr, 2),
        reason=f"Enter {direction.value} on a confirmed break of {round(block, 5)} "
               f"(blocking level between entry and target).",
    )


def _conditional_pullback(
    direction: Direction, entry: float, ema20: float | None, atr_v: float | None,
    ind: dict, target: float, confidence: float,
) -> ConditionalSuggestion | None:
    """When the entry sits away from value (above EMA20 for a long / below for a short), suggest a
    LIMIT order back at value (~EMA20) for a better entry instead of buying high / selling low. Stop
    sits beyond value/the last swing; target is the structural target (R:R-capped); R:R is measured
    from the value entry (so it beats chasing). Returns None if value isn't a better entry or R:R is
    too thin."""
    if not ema20 or not atr_v or atr_v <= 0 or not target or target <= 0:
        return None
    pad = max(0.5 * atr_v, ema20 * 5e-4)
    if direction == Direction.LONG:
        if entry <= ema20:                         # not above value -> no better pullback entry
            return None
        trigger = round(ema20, 6)
        swing_low = ind.get("swing_low")
        stop_ref = swing_low if (swing_low and swing_low < ema20) else ema20
        stop = round(stop_ref - pad, 6)
        risk = trigger - stop
        if risk <= 0:
            return None
        tp = round(min(target, trigger + _RR_MAX * risk), 6)  # cap R:R so the value entry is honest
        if tp <= trigger:
            return None
        rr = (tp - trigger) / risk
        order_type = "buy_limit"
    elif direction == Direction.SHORT:
        if entry >= ema20:
            return None
        trigger = round(ema20, 6)
        swing_high = ind.get("swing_high")
        stop_ref = swing_high if (swing_high and swing_high > ema20) else ema20
        stop = round(stop_ref + pad, 6)
        risk = stop - trigger
        if risk <= 0:
            return None
        tp = round(max(target, trigger - _RR_MAX * risk), 6)  # cap R:R so the value entry is honest
        if tp >= trigger:
            return None
        rr = (trigger - tp) / risk
        order_type = "sell_limit"
    else:
        return None
    if rr < _MIN_RR_COND:
        return None
    return ConditionalSuggestion(
        order_type=order_type, trigger_price=trigger, stop_loss=stop, take_profit=tp,
        confidence=round(confidence, 2), rr=round(rr, 2),
        reason=f"Enter {direction.value} on a pullback to value (~EMA20 {round(ema20, 5)}) "
               f"instead of chasing an overextended entry.",
    )


def _conditional_resumption(
    direction: Direction, entry: float, ind: dict, atr_v: float | None,
    levels: list[float], confidence: float,
) -> ConditionalSuggestion | None:
    """A momentum pullback isn't a dead end — arm a STOP order at the swing that RESUMES the trend:
    a break above the pullback's swing high (long) or below the swing low (short). It fires only
    when momentum re-aligns with the trend, which is exactly what the 'wait' was for. Stop sits
    beyond the pullback (>= ~1xATR, anti-wick); target is the next key level or a 2R fallback."""
    if not atr_v or atr_v <= 0 or entry <= 0:
        return None
    buf = max(0.1 * atr_v, entry * 5e-4)
    floor = atr_v  # anti-wick: keep the stop at least ~1xATR from the trigger
    sh, sl = ind.get("swing_high"), ind.get("swing_low")
    if direction == Direction.LONG:
        if not sh or sh <= entry:           # the pullback must sit BELOW the last swing high
            return None
        trigger = round(sh + buf, 6)
        base_stop = (sl - buf) if (sl and sl < trigger) else (trigger - 1.5 * atr_v)
        stop = round(min(base_stop, trigger - floor), 6)
        risk = trigger - stop
        ahead = sorted(lv for lv in (levels or []) if lv > trigger + buf)
        tp = round(min(ahead[0] if ahead else trigger + _RR * risk, trigger + _RR_MAX * risk), 6)
        order_type, swing = "buy_stop", sh
    elif direction == Direction.SHORT:
        if not sl or sl >= entry:           # the pullback must sit ABOVE the last swing low
            return None
        trigger = round(sl - buf, 6)
        base_stop = (sh + buf) if (sh and sh > trigger) else (trigger + 1.5 * atr_v)
        stop = round(max(base_stop, trigger + floor), 6)
        risk = stop - trigger
        below = sorted((lv for lv in (levels or []) if lv < trigger - buf), reverse=True)
        tp = round(max(below[0] if below else trigger - _RR * risk, trigger - _RR_MAX * risk), 6)
        order_type, swing = "sell_stop", sl
    else:
        return None
    if risk <= 0:
        return None
    rr = (tp - trigger) / risk if direction == Direction.LONG else (trigger - tp) / risk
    if rr < _MIN_RR_COND:
        return None
    return ConditionalSuggestion(
        order_type=order_type, trigger_price=trigger, stop_loss=stop, take_profit=tp,
        confidence=round(confidence, 2), rr=round(rr, 2),
        reason=f"Enter {direction.value} when momentum resumes the trend "
               f"(break of the pullback swing {round(swing, 5)}).",
    )


def _deterministic_decision(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead, now: datetime,
    trend_only: bool = False, scalp: bool = False,
) -> TradeProposal:
    base = TradeProposal(
        symbol=symbol, asset_class=asset_class, timeframe=timeframe,
        direction=Direction.NO_TRADE, confidence=0.0,
        technical=technical, fundamental=fundamental,
    )

    if _now_in_stand_aside(fundamental, now):
        base.rationale = "Standing aside: inside a high-impact event window."
        return base

    # The ENTRY-timeframe read. Select it by matching the requested timeframe — NOT by position:
    # the LLM technical path can return the timeframes in any order, so timeframes[0] is not
    # guaranteed to be the entry TF (using the wrong TF would size off the wrong ATR/RSI/regime).
    tf0 = None
    if technical.timeframes:
        tf0 = next((x for x in technical.timeframes if x.timeframe == timeframe),
                   technical.timeframes[0])
    ind = tf0.indicators if tf0 else {}
    trend = _trend_from_indicators(ind, tf0.trend if tf0 else "sideways")  # from computed EMAs
    macro = _macro_trend(technical)                                       # higher-timeframe context
    bias = fundamental.bias

    # --- regime FIRST (the senior-trader read): pick the strategy the regime permits ---
    adx_v = ind.get("adx")
    regime = _regime(ind)
    policy = regime_policy(regime)
    base.regime = regime
    base.strategy = policy["strategy"]
    # Scalp mode (15m): a dedicated, parsimonious scalp strategy replaces the swing logic entirely.
    if scalp:
        return _scalp_decision(base, ind, tf0, macro, regime, asset_class, symbol, now)
    # Trend-only mode: only trade a CLEAR trend (ADX >= 25 -> "trending"); stand aside in moderate /
    # ranging / volatile. Backtests show the trend regime is the edge while moderate+ranging are net
    # drags (same return, ~40% more drawdown when included). The live default comes from the setting.
    if trend_only and regime != "trending":
        base.strategy = "stand_aside"
        base.watch = True
        base.rationale = (f"Trend-only mode: standing aside — regime is {regime}, not a clear "
                          f"(ADX≥{_ADX_STRONG:.0f}) trend. {policy['note']}")
        return base
    # Ranging (no trend): fade the range edges back to the mean instead of trend-trading a flat tape.
    # Pass the higher-TF trend so a fade against a daily trend (a pullback, not a range) is refused.
    if regime == "ranging":
        return _mean_reversion_decision(base, ind, tf0, macro)
    # Low ADX but flagged volatile (a vol expansion without a trend) — whipsaw; stand aside.
    if adx_v is not None and adx_v < _ADX_MIN:
        base.rationale = (f"Standing aside: no trend (ADX {adx_v} < {_ADX_MIN:.0f}) and volatility "
                          "expanding — whipsaw zone.")
        return base

    # --- direction from trend + fundamental + MACD momentum + higher-timeframe alignment ---
    macd_hist = ind.get("macd_hist")
    rsi = ind.get("rsi14")
    pdi = ind.get("plus_di")
    mdi = ind.get("minus_di")
    atr_v = ind.get("atr14")
    # Counter-momentum only blocks entry if it's MEANINGFUL (>= 10% of ATR) — trivial noise is
    # ignored so we don't sit out forever on a flat histogram.
    mom_thresh = _MOM_ATR_FRAC * atr_v if atr_v else 0.0
    # The TREND (technical) decides direction. The fundamental bias is a soft macro lean — the
    # fundamental agent self-rates ~0.3 ("not a primary signal"), so it only NUDGES confidence
    # below; it must not veto a clean technical trend. Direction is still gated by the higher-
    # timeframe trend (don't fight the macro) and the momentum-pullback wait.
    if trend == "up":
        if macd_hist is not None and macd_hist < -mom_thresh:
            # Trend up but momentum meaningfully down = pullback. Arm a resumption break instead of
            # just waiting, so it fires when momentum turns back up.
            base.watch = True
            px = ind.get("last_close") or 0.0
            base.conditional = _conditional_resumption(
                Direction.LONG, px, ind, atr_v, _key_levels(technical, tf0, px),
                round(min(0.7, 0.45 + 0.25 * technical.confidence), 2))
            armed_note = ("Arm a long on a break back up to resume the trend."
                          if base.conditional is not None
                          else "Waiting for momentum to turn back up (no clean break level to arm yet).")
            base.rationale = (
                f"Uptrend pullback — momentum still down (MACD hist {macd_hist}, RSI {rsi}, "
                f"−DI {mdi} > +DI {pdi}). {armed_note}"
            )
            return base
        if macro == "down":
            base.rationale = "No confluence: higher-timeframe trend is DOWN — not buying into it."
            return base
        direction = Direction.LONG
    elif trend == "down":
        if macd_hist is not None and macd_hist > mom_thresh:
            base.watch = True
            px = ind.get("last_close") or 0.0
            base.conditional = _conditional_resumption(
                Direction.SHORT, px, ind, atr_v, _key_levels(technical, tf0, px),
                round(min(0.7, 0.45 + 0.25 * technical.confidence), 2))
            armed_note = ("Arm a short on a break back down to resume the trend."
                          if base.conditional is not None
                          else "Waiting for momentum to turn back down (no clean break level to arm yet).")
            base.rationale = (
                f"Downtrend pullback — momentum turning up (MACD hist {macd_hist}, RSI {rsi}, "
                f"+DI {pdi} > −DI {mdi}). {armed_note}"
            )
            return base
        if macro == "up":
            base.rationale = "No confluence: higher-timeframe trend is UP — not selling into it."
            return base
        direction = Direction.SHORT
    else:
        base.rationale = "No clear trend (EMAs sideways) — sitting out."
        return base

    # --- market-structure confluence: a pro won't fight clear opposing swing structure ---
    # If the EMA stack points one way but the SWINGS (entry TF AND higher TF) point the other,
    # that's an early-reversal / chop tell. Stand aside and wait rather than buying into
    # lower-highs/lower-lows (or selling into higher-highs/higher-lows).
    struct = _structure_label(ind)
    macro_struct = _macro_structure(technical)
    against_struct = (
        (direction == Direction.LONG and struct == "down" and macro_struct == "down")
        or (direction == Direction.SHORT and struct == "up" and macro_struct == "up")
    )
    if against_struct:
        base.watch = True
        base.rationale = (
            f"Structure conflict: EMA trend reads {trend}, but market structure is {struct} "
            f"(entry TF) / {macro_struct} (higher TF) — against a {direction.value}. Likely an "
            "early reversal or chop; waiting for swing structure to confirm before entering."
        )
        return base

    # --- regime gate #2: a sharp volatility blow-off WITHOUT a strong trend = whipsaw zone ---
    # A pro doesn't chase a chaotic expansion (often a news spike / capitulation); they wait for
    # it to settle. (A strong-trend breakout is classed "trending", not "volatile", so it passes.)
    # `regime` was already read up top (regime-first); reuse it.
    vol_ratio = ind.get("vol_atr_ratio")
    if regime == "volatile" and vol_ratio is not None and vol_ratio >= _REGIME_VOL_EXTREME:
        base.watch = True
        base.rationale = (
            f"Volatile regime: volatility is expanding sharply (ATR {vol_ratio:.1f}x its baseline) "
            f"without a strong trend (ADX {adx_v}). High whipsaw risk — standing aside until it "
            "settles."
        )
        return base

    entry = ind.get("last_close") or _last_close(technical)
    if entry is None or entry <= 0:
        base.rationale = "No usable entry price from technical read; sitting out."
        return base

    # --- overextension: entering far from the mean (EMA20) invites a mean-reversion bounce into
    # the stop. We DON'T hard-block (a healthy trend always rides above/below the lagging EMA, so
    # blocking would skip every trend) — instead we down-weight a stretched entry below. ---
    ema20 = ind.get("ema20")
    overextended = bool(ema20 and atr_v and (
        (direction == Direction.LONG and entry > ema20 + _PULLBACK_ATR * atr_v) or
        (direction == Direction.SHORT and entry < ema20 - _PULLBACK_ATR * atr_v)
    ))

    # --- divergence exhaustion (#2): regular RSI divergence AGAINST the trade while price is
    # stretched is a classic reversal tell — wait for it to resolve rather than entering into it. ---
    div_against = bool(
        (direction == Direction.LONG and ind.get("div_bear"))
        or (direction == Direction.SHORT and ind.get("div_bull"))
    )
    div_with = bool(
        (direction == Direction.LONG and ind.get("div_bull_hidden"))
        or (direction == Direction.SHORT and ind.get("div_bear_hidden"))
    )
    if div_against and overextended:
        base.watch = True
        base.rationale = (
            f"Momentum divergence against the {direction.value} with price extended (RSI {rsi}) — "
            "exhaustion risk. Waiting for momentum to realign before entering."
        )
        return base

    support = tf0.support_levels[0] if tf0 and tf0.support_levels else None
    resistance = tf0.resistance_levels[0] if tf0 and tf0.resistance_levels else None

    # --- protective stop ---
    # A pro stops where the trade is INVALIDATED, not at an arbitrary distance: just beyond the
    # last swing (structure). We prefer that swing-structure stop, falling back to an ATR stop,
    # with two guards — never tighter than the anti-wick floor (>= 1xATR, so noise can't pick it
    # off), and never wider than _STRUCT_STOP_MAX_ATR (a too-far swing isn't a practical stop).
    # Crypto keeps a wider ATR multiple. (These guards fixed the instant-wick crypto losses.)
    # BEYOND-THE-WICK: when recent wicks have ALREADY pierced the swing, place the stop beyond those
    # wicks (recent_low/recent_high) rather than just past the obvious swing — the swing+0.2ATR zone
    # is exactly where stop-hunts reach. Only widens when there's evidence of wicking, and the
    # _STRUCT_STOP_MAX_ATR cap still bounds it (a wick further than that falls back to the ATR stop).
    atr_mult = _ATR_STOP_MULT_CRYPTO if asset_class == AssetClass.CRYPTO else _ATR_STOP_MULT
    min_stop_dist = _MIN_STOP_ATR_FRAC * atr_v if atr_v else 0.0
    swing_low = ind.get("swing_low")
    swing_high = ind.get("swing_high")
    recent_low = ind.get("recent_low")
    recent_high = ind.get("recent_high")
    stop_basis = "ATR"
    if direction == Direction.LONG:
        atr_stop = entry - atr_mult * atr_v if atr_v else None
        stop = atr_stop if atr_stop is not None else (support if (support and support < entry) else entry * 0.98)
        # Tighten to nearby support only if it stays beyond the anti-wick floor.
        if (support is not None and atr_stop is not None and atr_stop < support < entry
                and (entry - support) >= min_stop_dist):
            stop, stop_basis = support, "support"
        # Structural stop: just below the last swing low (the long's invalidation), if practical —
        # extended below recent wicks that already pierced it (anti stop-hunt).
        if atr_v and swing_low is not None and swing_low < entry:
            struct_ref = swing_low
            if recent_low is not None and recent_low < swing_low:
                struct_ref = recent_low
            struct_dist = (entry - struct_ref) + _STRUCT_STOP_BUFFER_ATR * atr_v
            if struct_dist <= _STRUCT_STOP_MAX_ATR * atr_v:
                stop = entry - max(struct_dist, min_stop_dist)
                stop_basis = "swing-low structure" if struct_ref == swing_low else "swing/wick structure"
        risk = entry - stop
    else:
        atr_stop = entry + atr_mult * atr_v if atr_v else None
        stop = atr_stop if atr_stop is not None else (resistance if (resistance and resistance > entry) else entry * 1.02)
        if (resistance is not None and atr_stop is not None and entry < resistance < atr_stop
                and (resistance - entry) >= min_stop_dist):
            stop, stop_basis = resistance, "resistance"
        # Structural stop: just above the last swing high (the short's invalidation), if practical —
        # extended above recent wicks that already pierced it (anti stop-hunt).
        if atr_v and swing_high is not None and swing_high > entry:
            struct_ref = swing_high
            if recent_high is not None and recent_high > swing_high:
                struct_ref = recent_high
            struct_dist = (struct_ref - entry) + _STRUCT_STOP_BUFFER_ATR * atr_v
            if struct_dist <= _STRUCT_STOP_MAX_ATR * atr_v:
                stop = entry + max(struct_dist, min_stop_dist)
                stop_basis = "swing-high structure" if struct_ref == swing_high else "swing/wick structure"
        risk = stop - entry

    if risk <= 0:
        base.rationale = "Computed risk is non-positive; sitting out."
        return base

    # --- target from REAL key levels (#3/#4): pivot S/R, prior day/week high-low, round numbers.
    # Aim for a clean >=2R level when one exists (snap the 2R target to a real level); a STRONG
    # trend may run to the NEXT level (up to the RR cap); a moderate trend caps at a sub-2R level;
    # sit out if a wall sits within <1R against the trade. Honest R:R reported per setup. ---
    levels = _key_levels(technical, tf0, entry)
    strong = adx_v is not None and adx_v >= _ADX_STRONG
    ignore = _STRUCT_IGNORE * risk  # levels inside the breakout zone aren't barriers
    if direction == Direction.LONG:
        cap, two_r = entry + _RR_MAX * risk, entry + _RR * risk
        ahead = sorted(lv for lv in levels if lv > entry + ignore)          # nearest first
        past_2r = [lv for lv in ahead if lv >= two_r]
    else:
        cap, two_r = entry - _RR_MAX * risk, entry - _RR * risk
        ahead = sorted((lv for lv in levels if lv < entry - ignore), reverse=True)
        past_2r = [lv for lv in ahead if lv <= two_r]

    def _clamp(t: float) -> float:
        return min(t, cap) if direction == Direction.LONG else max(t, cap)

    nearest = ahead[0] if ahead else None
    nearest_rr = (abs(nearest - entry) / risk) if nearest is not None else None

    # Does a market entry NOW clear the minimum take-R:R? If the only level sits below _MIN_RR_ENTRY
    # and the trend isn't strong enough to break through it, we DON'T chase the market entry — the
    # wide market stop makes the direct R:R too thin (~1:1 is negative expectancy after costs). We
    # arm the better-priced break/pullback alternative below and stand aside instead.
    take_market = True
    if nearest is not None and nearest_rr < _RR:
        # A real key level sits BEFORE 2R — the path to a clean 2R isn't free.
        if strong:
            # Strong trend can break the minor level — aim for the next clean >=2R level, else 2R.
            target = _clamp(past_2r[0]) if past_2r else two_r
            struct_note = f"~{abs(target - entry) / risk:.1f}R (strong trend through {round(nearest, 5)})"
        elif nearest_rr >= _MIN_RR_ENTRY:
            target = nearest  # moderate trend respects the level — acceptable R:R, cap there
            struct_note = f"capped at key level {round(nearest, 5)} (~{nearest_rr:.1f}R)"
        else:
            # Too thin to take at market — stand aside; the armed alternative below has real R:R.
            take_market = False
            target = nearest
            struct_note = f"only ~{nearest_rr:.1f}R to {round(nearest, 5)} at market"
    elif past_2r:
        # Nearest meaningful level is already >=2R — clean target; a strong trend may run further.
        target = past_2r[0]
        if strong and len(past_2r) >= 2 and (past_2r[1] <= cap if direction == Direction.LONG else past_2r[1] >= cap):
            target = past_2r[1]
        target = _clamp(target)
        struct_note = f"key level {round(target, 5)} (~{abs(target - entry) / risk:.1f}R)"
    else:
        target = two_r  # no key level in range -> fixed RR
        struct_note = f"~{_RR:.0f}R"

    # --- confidence from multi-factor confluence ---
    conf = 0.3 + 0.2 * technical.confidence + 0.15 * fundamental.confidence
    # Fundamental bias: a soft macro lean (now a confidence nudge, no longer a direction veto).
    # Agrees with the trade -> small bonus; opposes -> small penalty; neutral -> nothing.
    if bias == TradingBias.BULLISH:
        conf += 0.05 if direction == Direction.LONG else -0.05
    elif bias == TradingBias.BEARISH:
        conf += 0.05 if direction == Direction.SHORT else -0.05
    if macro == trend:
        conf += 0.15  # higher-timeframe agrees
    if adx_v is not None and adx_v >= _ADX_STRONG:
        conf += 0.1
    vr = ind.get("vol_ratio")
    if vr is not None and vr > 1.2:
        conf += 0.1
    if macd_hist is not None and ((direction == Direction.LONG) == (macd_hist > 0)):
        conf += 0.05
    # Cross-timeframe momentum conflict: the higher-TF MACD pushing AGAINST the trade is a
    # lower-conviction signal (the XAU short was taken with 1h vs 4h MACD disagreeing).
    macro_tf = _macro_tf(technical)
    macro_macd = macro_tf.indicators.get("macd_hist") if macro_tf else None
    macro_conflict = macro_macd is not None and (
        (direction == Direction.LONG and macro_macd < 0) or
        (direction == Direction.SHORT and macro_macd > 0)
    )
    if macro_conflict:
        conf -= 0.1
    # Entry LOCATION (anti-chase, graded): score the entry by its distance from value (EMA20) in
    # ATRs. A pullback to value is the pro's entry (reward it); the further it's stretched the more
    # we down-weight it — chasing far from value is where losers come from (today's HK50 short was
    # chased to the low and squeezed). The bigger haircut pushes chased setups below the Hybrid
    # threshold and ranks pullbacks above them in the scanner.
    value_dist = abs(entry - ema20) / atr_v if (ema20 and atr_v and atr_v > 0) else None
    at_value = value_dist is not None and value_dist <= _VALUE_ENTRY_ATR
    if value_dist is not None:
        if at_value:
            conf += 0.1                       # pullback to value — preferred entry
        elif value_dist >= _PULLBACK_ATR:
            conf -= 0.18                      # chasing far from value — strong anti-chase
        elif value_dist >= _STRETCHED_ATR:
            conf -= 0.06                      # getting stretched
    rsi = ind.get("rsi14")
    if rsi is not None and ((direction == Direction.LONG and rsi >= _RSI_OB)
                            or (direction == Direction.SHORT and rsi <= _RSI_OS)):
        conf -= 0.1  # entering when already stretched
    e200 = ind.get("ema200")
    if e200:
        regime_ok = (direction == Direction.LONG and entry >= e200) or \
                    (direction == Direction.SHORT and entry <= e200)
        conf += 0.05 if regime_ok else -0.05
    # Market structure: aligned swings (HH/HL for a long, LH/LL for a short) add real conviction;
    # trading against structure or right after a change-of-character (CHoCH) subtracts it. This is
    # the chart-reader's "is price action actually confirming this?" check.
    if struct != "range":
        aligned = (direction == Direction.LONG and struct == "up") or (
            direction == Direction.SHORT and struct == "down"
        )
        conf += 0.1 if aligned else -0.1
    if ind.get("choch"):
        conf -= 0.1
    # RSI divergence: regular divergence AGAINST the trade is exhaustion (down-weight); hidden
    # divergence WITH the trade is continuation confirmation (up-weight).
    if div_against:
        conf -= 0.12
    if div_with:
        conf += 0.07
    # Regime: a clean trend is the engine's edge; a volatile (expanding, trendless) tape is lower
    # conviction even when a setup forms.
    if regime == "volatile":
        conf -= 0.1
    # Session/liquidity: lean into the liquid windows, discount thin hours (noise, wide spreads).
    session_q, _session_note = _session_quality(asset_class, symbol, now)
    if session_q == "active":
        conf += 0.05
    elif session_q == "thin":
        conf -= 0.05
    confidence = round(max(0.05, min(0.95, conf)), 2)

    # Carry a conditional ('wait') entry so the trade can be ARMED rather than chased — computed
    # REGARDLESS of the market decision so it survives both an LLM veto AND a thin-R:R market sit-out.
    # Priority: a break-STOP past a blocking level (valid only after the break); otherwise — whenever
    # the entry sits away from value (not already AT value) — a LIMIT back at value (~EMA20).
    base.conditional = (
        _conditional_break(direction, entry, atr_v, levels, target, confidence, ind)
        or (None if at_value
            else _conditional_pullback(direction, entry, ema20, atr_v, ind, target, confidence))
    )

    # Thin direct R:R (the only level sits < _MIN_RR_ENTRY away, moderate trend): don't take the
    # market entry — keep it a NO_TRADE/watch carrying the armed alternative (real R:R from a tighter
    # stop / better entry). ~1:1 at market is negative expectancy after costs.
    if not take_market:
        side = "above" if direction == Direction.LONG else "below"
        base.watch = True
        if base.conditional is not None:
            base.rationale = (
                f"Standing aside at MARKET (a {direction.value.upper()} is valid): only ~{nearest_rr:.1f}R "
                f"to the nearest level {round(nearest, 5)} {side} entry — below the {_MIN_RR_ENTRY:.1f}R "
                f"minimum, the wide market stop makes the direct R:R too thin. Armed a better-priced "
                f"{base.conditional.order_type} instead (a tighter stop / entry at value lifts the R:R)."
            )
        else:
            base.rationale = (
                f"Standing aside: a {direction.value.upper()} is valid but only ~{nearest_rr:.1f}R to the "
                f"nearest level {round(nearest, 5)} {side} entry (below the {_MIN_RR_ENTRY:.1f}R minimum) "
                f"and no clean armed alternative right now — waiting for a better entry."
            )
        return base

    base.direction = direction
    base.entry = round(entry, 6)
    base.stop_loss = round(stop, 6)
    base.take_profit = round(target, 6)
    base.confidence = confidence
    base.rationale = (
        f"Confluence {direction.value.upper()}: {regime} regime, entry-TF trend={trend}, "
        f"macro={macro}, structure={struct}/{macro_struct}, ADX {adx_v}, MACD hist={macd_hist}, "
        f"RSI {rsi}, bias={bias.value}"
        f"{' (cross-TF momentum conflict)' if macro_conflict else ''}"
        f"{' (pullback entry at value)' if at_value else ''}"
        f"{' (stretched entry)' if overextended else ''}"
        f"{' (divergence against)' if div_against else ''}"
        f"{' (hidden div confirms)' if div_with else ''}"
        f"{' (CHoCH)' if ind.get('choch') else ''}"
        f"{f' ({session_q} session)' if session_q != 'normal' else ''}. "
        f"Entry {base.entry}, stop {base.stop_loss} "
        f"({stop_basis}{f', {risk / atr_v:.1f}xATR' if atr_v else ''}), "
        f"target {base.take_profit} ({struct_note}). Deterministic (no LLM)."
    )
    return base


def run_orchestrator(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead,
    now: datetime | None = None, use_llm: bool = True, trend_only: bool = False,
    scalp: bool = False,
) -> TradeProposal:
    """Deterministic engine decides; the LLM may only CONFIRM or VETO (never widen).

    1. The deterministic strategy (regime/MTF/ADX/momentum/ATR/structure gates) is the source
       of truth — it picks direction, entry, stop, target, and can say NO_TRADE.
    2. If it declined, we return NO_TRADE — the LLM cannot create a trade the rules reject.
    3. If it proposed a trade and the LLM is enabled, the LLM reviews it as a risk-aware
       second opinion: confirm (optionally lowering confidence) or veto with reasons. It can
       never change direction/levels or raise risk. The Risk Manager remains final downstream.
    """
    now = now or datetime.now(timezone.utc)

    proposal = _deterministic_decision(symbol, asset_class, timeframe, technical, fundamental, now,
                                       trend_only=trend_only, scalp=scalp)

    if proposal.direction == Direction.NO_TRADE or not use_llm or not llm_available():
        log.info("orchestrator decision (deterministic)",
                 extra={"symbol": symbol, "direction": proposal.direction.value})
        return proposal

    # --- LLM review of the deterministic setup (confirm / veto only) ---
    # The checklist depends on the STRATEGY: a ranging mean-reversion FADE must NOT be judged like a
    # trend trade (it is deliberately counter to the last leg, in a low-ADX market — those are the
    # premise, not flaws), or the reviewer would veto every fade.
    if proposal.strategy == "scalp":
        checklist = (
            "This is a 15m TREND-PULLBACK SCALP: a WITH-trend entry on a pullback to value (EMA20) "
            "that held, with momentum confirming, in a liquid session. Judge it as a SCALP, not a "
            "swing trade: a ~1.3R+ target is acceptable (half is banked at +1R and the rest trails), "
            "and being a quick 15m trade is the premise — do NOT veto for 'small target' or 'low "
            "timeframe'. CONFIRM if it is with the trend at a value pullback with momentum and room to "
            "the target. VETO only a clear flaw: against the higher-timeframe trend, no momentum, an "
            "imminent high-impact event, or no room to the target. When unsure, confirm at lower "
            "confidence."
        )
    elif proposal.strategy == "mean_reversion":
        checklist = (
            "This is a deliberate RANGING MEAN-REVERSION fade (regime=ranging). Judge it as a FADE, "
            "NOT a trend trade: being counter to the last leg and a low-ADX / no-trend market are the "
            "PREMISE of this strategy, NOT veto reasons. CONFIRM if price is at a real range edge "
            "(resistance for a short / support for a long) showing a rejection, with adequate reward "
            "back to the mean and no imminent high-impact event. VETO only if it is NOT actually at an "
            "edge, there is no rejection, the reward to the mean is thin, or an event is imminent. "
            "When unsure, confirm at lower confidence rather than veto."
        )
    else:
        checklist = (
            "Run your professional checklist (trend & structure, location/value vs chasing, momentum "
            "confirmation, reward:risk vs the nearest opposing level, event & liquidity risk). Grade "
            "it A/B/C. Confirm A/B, and confirm a marginal C at LOWER confidence; VETO only a clear "
            "failure (counter-trend, no real R:R, an imminent high-impact event, or no trend). When "
            "unsure, confirm at lower confidence rather than veto."
        )
    user = (
        f"PROPOSED SETUP (from the deterministic strategy — you may only confirm or veto):\n"
        f"  symbol={symbol} timeframe={timeframe} direction={proposal.direction.value}\n"
        f"  strategy={proposal.strategy} regime={proposal.regime}\n"
        f"  entry={proposal.entry} stop={proposal.stop_loss} target={proposal.take_profit} "
        f"confidence={proposal.confidence}\n  rationale={proposal.rationale}\n\n"
        f"TECHNICAL READ (all timeframes, indicators, support/resistance):\n"
        f"{technical.model_dump_json(indent=2)}\n\n"
        f"FUNDAMENTAL READ:\n{fundamental.model_dump_json(indent=2)}\n\n"
        f"{checklist}"
    )
    review = analyze(system=_REVIEW_SYSTEM, user=user, schema=TradeReviewLLM, max_tokens=2000)
    if review is None:
        return proposal  # LLM unavailable/failed -> trust the deterministic setup

    if review.decision == ReviewDecision.VETO:
        concerns = ("; ".join(review.concerns)) if review.concerns else review.rationale
        log.info("LLM vetoed deterministic setup", extra={"symbol": symbol, "reason": concerns[:120]})
        proposal.rationale = (
            f"{proposal.direction.value.upper()} setup VETOED by AI review: {review.rationale}"
            f"{' | concerns: ' + concerns if review.concerns else ''}"
        )
        proposal.direction = Direction.NO_TRADE
        proposal.entry = proposal.stop_loss = proposal.take_profit = None
        proposal.confidence = 0.0
        proposal.review_decision = "veto"
        return proposal

    # Confirmed: keep deterministic levels; the LLM may only LOWER confidence.
    proposal.confidence = round(min(proposal.confidence, review.confidence or proposal.confidence), 2)
    # Drop the stale "(no LLM)" marker — this proposal WAS just LLM-reviewed.
    base_rationale = proposal.rationale.replace(" Deterministic (no LLM).", "").rstrip()
    proposal.rationale = f"{base_rationale} | AI review CONFIRMED: {review.rationale}"
    proposal.review_decision = "confirm"
    log.info("LLM confirmed deterministic setup",
             extra={"symbol": symbol, "direction": proposal.direction.value, "confidence": proposal.confidence})
    return proposal


def review_armed_setup(
    proposal: TradeProposal, technical: TechnicalRead, *, use_llm: bool = True,
) -> tuple[bool, str]:
    """Re-validate an ALREADY-ARMED conditional setup at the moment its price trigger hits.

    This is the double-check for a pending break/pullback entry — judged on the setup's OWN plan,
    NOT re-derived from the current price. A break entry sits, by definition, right at the level it
    is breaking; re-deriving a fresh trade "from here" would see the next level <1R away and reject
    almost every valid break (the bug this replaces). So the only question here is: has the THESIS
    broken since we armed it? Returns ``(still_valid, reason)``.

    The LLM may only CONFIRM or VETO; it vetoes ONLY a clear invalidation (the higher-timeframe
    trend/structure has flipped against the setup, momentum has decisively reversed, or an imminent
    high-impact event makes entry unsafe). If the LLM is unavailable we trust the armed plan — it was
    already AI-reviewed when it was armed, and the deterministic Risk Manager remains the final gate.
    """
    if not use_llm or not llm_available():
        return True, "LLM unavailable — trusting the armed plan"

    user = (
        f"AN ALREADY-VALIDATED CONDITIONAL SETUP JUST TRIGGERED (its price level was reached). You "
        f"may only confirm or veto whether it is STILL valid to enter — do NOT re-derive a fresh "
        f"trade from the current price.\n"
        f"  symbol={proposal.symbol} timeframe={proposal.timeframe} direction={proposal.direction.value}\n"
        f"  entry/trigger={proposal.entry} stop={proposal.stop_loss} target={proposal.take_profit} "
        f"confidence={proposal.confidence}\n  original thesis: {proposal.rationale}\n\n"
        f"TECHNICAL READ (all timeframes, indicators, support/resistance):\n"
        f"{technical.model_dump_json(indent=2)}\n\n"
        f"IMPORTANT — judge ONLY whether the original thesis is still intact. Do NOT veto because "
        f"price is 'at the level', 'extended from value', or 'has no room to the next level' — a "
        f"break/pullback entry is BY DEFINITION at its level, and its reward is measured from the "
        f"trigger, not from here. CONFIRM unless there is a CLEAR invalidation: the higher-timeframe "
        f"trend/structure has flipped AGAINST this {proposal.direction.value}, momentum has decisively "
        f"reversed against it, or an imminent high-impact event makes entry unsafe. When unsure, CONFIRM."
    )
    review = analyze(system=_REVIEW_SYSTEM, user=user, schema=TradeReviewLLM, max_tokens=1500)
    if review is None:
        return True, "AI review unavailable — trusting the armed plan"
    if review.decision == ReviewDecision.VETO:
        concerns = ("; ".join(review.concerns)) if review.concerns else review.rationale
        log.info("armed setup vetoed at trigger", extra={"symbol": proposal.symbol, "reason": concerns[:120]})
        return False, f"thesis invalidated — {concerns[:180]}"
    return True, f"AI re-confirmed at trigger: {review.rationale[:160]}"
