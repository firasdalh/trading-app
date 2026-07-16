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
_ADX_STRONG = 23.0    # >=23 the trend is strong enough for trend-following (per the entry checklist)
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
_RSI_OB = 75.0        # overbought / oversold caution thresholds (confidence haircut)
_RSI_OS = 25.0
_RSI_TREND_OB = 70.0  # RSI zone at which a trend entry is "stretched" -> arm the pullback instead of
_RSI_TREND_OS = 30.0  # chasing at market (UNLESS a strong trend with momentum still confirming)
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
_MOM_AI_MIN_CONF = 0.55  # below this the AI momentum class isn't trusted -> keep the deterministic fixed arm
_STRETCHED_ATR = 1.5    # 1.5-2.5 ATR from value = getting stretched (small anti-chase penalty);
                        # beyond _PULLBACK_ATR it's a full chase (bigger penalty) — see grading below
# --- map-read WALL-proximity soft factor. Unlike the REMOVED channel factor, the wall penalty applies
# ONLY when the trend is NOT strong: a strong trend legitimately breaks levels, so we don't punish it
# for that; a weak trend running into a nearby barrier with little headroom IS a chase. A key level
# just cleared (behind entry) on rising volume is the opposite — a confirmed break -> bonus. Validated
# on walk-forward (analysis/map_factors.md): kept because it improved BOTH in- and out-of-sample. (The
# volume-TREND factor tested alongside it was worse OOS and was dropped — see the NOTE at its site.)
_WALL_NEAR_ATR = 0.75   # a barrier within this many ATR ahead (weak trend) = limited headroom -> penalty
_WALL_BEHIND_ATR = 0.5  # a key level this recently cleared (behind entry) = a fresh breakout -> bonus
_WALL_PENALTY = 0.06
_WALL_BREAK_BONUS = 0.06

# The deterministic ENTRY-CHECKLIST filters the user can toggle on/off (Settings -> disabled_filters
# -> the `disable` set honoured in _deterministic_decision). All ON by default; each maps to a real
# gate/penalty in the engine. Order = the pro's entry-quality priority (structure first, R:R last).
DET_FILTERS = [
    {"key": "structure", "label": "Market structure",
     "desc": "BOS / CHoCH / higher-high-higher-low confluence — refuse a trade that fights the swing structure."},
    {"key": "mtf", "label": "Higher-timeframe trend",
     "desc": "Trade WITH the immediate higher timeframe (15m→1h, 1h→4h, 4h→1d); don't fight the next TF up (no confluence → stand aside)."},
    {"key": "htf_level", "label": "Higher-TF S/R levels",
     "desc": "Respect major 4h/1d support/resistance — don't enter straight into a big-timeframe level (stand aside / wait for the break or pullback)."},
    {"key": "htf_pullback", "label": "Higher-TF pullback (buy the dip)",
     "desc": "When your timeframe pulls back against a clear higher-TF trend and RSI is exhausted AND confirmed (at a support/resistance or an RSI divergence), ARM a resumption in the higher-TF direction (buy the dip / sell the rally) instead of standing aside."},
    {"key": "range_breakout", "label": "Range breakout",
     "desc": "In a ranging market, when price closes beyond the range top/bottom WITH the higher-TF trend and a volume / decisive-close confirmation, trade the breakout (measured-move target, stop back inside) instead of only fading the edges."},
    {"key": "ema_pullback", "label": "EMA20 pullback",
     "desc": "In a trend, take the continuation when price pulls back to the EMA20 (dynamic support/resistance) and closes back on the trend side — a tight-stopped entry off the moving average."},
    {"key": "failed_break", "label": "Failed-break reversal",
     "desc": "In a range, fade a FALSE breakout (a liquidity sweep beyond the range that closes back inside — a bull/bear trap) back toward the mean, stop beyond the sweep. Not against a clear higher-TF trend."},
    {"key": "alignment", "label": "Trend alignment (A+ boost)",
     "desc": "Grade each trend trade by how clearly the direction stacks up (all timeframes + strength + momentum). A fully-aligned 'A+' trend gets a small confidence bump so the clearest setups rank up and the Hybrid prioritises them."},
    {"key": "chase", "label": "Anti-chase (ATR distance)",
     "desc": "Down-weight entries stretched far from EMA20 in ATRs — the top filter against buying the top / selling the bottom."},
    {"key": "adx", "label": "ADX trend strength",
     "desc": "Only take trend entries when the trend is strong enough (ADX ≥ 23) — stand aside in the forming band (ADX 20–23); ranging (< 20) fades the range instead. This is Trend-only mode."},
    {"key": "ema200", "label": "Long-term trend (EMA200)",
     "desc": "Prefer trades on the right side of the 200-EMA (with the long-term trend); down-weight entries against it."},
    {"key": "momentum", "label": "MACD momentum",
     "desc": "Momentum must align with the trade. When it's rolling over against the trend, the engine "
             "doesn't chase — with AI momentum-read ON it classifies the pullback (healthy pullback / "
             "weak momentum / probable reversal) and decides enter / arm the dip / wait / reject; with "
             "it OFF it just arms the pullback and waits (the fixed rule)."},
    {"key": "macd_rising", "label": "MACD histogram rising",
     "desc": "Prefer entries where the MACD histogram is EXPANDING (growing bars) in the trade direction; down-weight a fading histogram even if still aligned."},
    {"key": "rsi_extreme", "label": "RSI overextension",
     "desc": "Don't chase into an overbought/oversold RSI. Unless a strong trend rides it, the engine "
             "reads the stretch — with AI momentum-read ON it classifies the pullback (healthy / weak / "
             "reversal) and decides enter / arm the dip / wait / reject; OFF it arms the pullback."},
    {"key": "divergence", "label": "RSI divergence",
     "desc": "Skip entries into a momentum-vs-price divergence while stretched (exhaustion)."},
    {"key": "volatility", "label": "Volatility blow-off",
     "desc": "Stand aside in a chaotic volatility expansion with no trend (whipsaw zone)."},
    {"key": "wall", "label": "S/R wall + volume",
     "desc": "Penalise chasing into a nearby S/R wall; reward a volume-backed break behind the entry."},
    {"key": "session", "label": "Session / liquidity",
     "desc": "Lean into the liquid trading sessions; discount thin-hour entries (wider spreads, more noise / lower quality)."},
    {"key": "minrr", "label": "Minimum reward:risk",
     "desc": "Require a minimum R:R at market entry, else arm the better-priced break/pullback."},
]
DET_FILTER_KEYS = {f["key"] for f in DET_FILTERS}

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


# --- Laddered higher-timeframe context ---------------------------------------------------------- #
# The trend engine trades WITH the IMMEDIATE higher timeframe (15m→1h, 1h→4h, 4h→1d) instead of
# demanding alignment with the single highest TF. The big timeframes (4h/1d) are then respected as
# LEVELS (major S/R you don't trade straight into), not as a hard trend veto. This is looser than the
# old "must agree with the daily" rule — the range-fade / RSI-Over paths KEEP the daily filter
# (``_macro_trend``), which is the one the backtest proved plugs the "fading the daily" leak.
_HTF_LEVEL_ATR = 1.0        # a big-TF level within this many ATRs of entry is "in the way"


def _higher_tf(technical: TechnicalRead, entry_tf: str):
    """The smallest loaded timeframe strictly HIGHER than the entry TF (its immediate context)."""
    er = _TF_RANK.get(entry_tf, 0)
    best, best_rank = None, 10**9
    for tf in technical.timeframes:
        r = _TF_RANK.get(tf.timeframe, 0)
        if er < r < best_rank:
            best, best_rank = tf, r
    return best


def _higher_trend(technical: TechnicalRead, entry_tf: str) -> tuple[str, str]:
    """(trend, tf_name) of the immediate higher timeframe; ('sideways', '') if none is loaded."""
    hi = _higher_tf(technical, entry_tf)
    if hi is None:
        return "sideways", ""
    return _trend_from_indicators(hi.indicators, hi.trend), hi.timeframe


def _big_tf_levels(technical: TechnicalRead, entry_tf: str) -> list[float]:
    """Major S/R from the BIG timeframes above the entry (4h/1d): their nearest pivot support/
    resistance plus prior day/week highs/lows — the levels a desk won't trade straight into."""
    floor_rank = max(_TF_RANK.get(entry_tf, 0) + 1, _TF_RANK["4h"])
    out: list[float] = []
    for tf in technical.timeframes:
        if _TF_RANK.get(tf.timeframe, 0) < floor_rank:
            continue
        out += list(tf.support_levels[:2]) + list(tf.resistance_levels[:2])
        for k in ("prior_day_high", "prior_day_low", "prior_week_high", "prior_week_low"):
            v = tf.indicators.get(k)
            if v:
                out.append(v)
    return [x for x in out if x and x > 0]


def _opposing_big_tf_level(direction: Direction, entry: float, atr: float | None,
                           levels: list[float]) -> float | None:
    """A big-TF level sitting in the trade's path within ~1 ATR — overhead resistance for a LONG,
    support just below for a SHORT. Returns the nearest such level (to explain), else None."""
    if not entry or not atr or atr <= 0 or not levels:
        return None
    reach = _HTF_LEVEL_ATR * atr
    if direction == Direction.LONG:
        ahead = [lv for lv in levels if entry < lv <= entry + reach]
        return min(ahead) if ahead else None
    behind = [lv for lv in levels if entry - reach <= lv < entry]
    return max(behind) if behind else None


# --- 'Buy the dip / sell the rally' — pullback entry in the HIGHER-TF trend direction ------------ #
# When the entry TF pulls back AGAINST a clear higher-TF trend and the pullback is exhausted, arm a
# resumption in the higher-TF direction. SAFE: it ARMS a break (waits for the turn), never market-
# enters the falling/rising move. Requires RSI in the pullback zone AND a confirmation (the dip is at
# a support / rally at a resistance, or an RSI divergence).
_RSI_PULLBACK_LONG = 40.0    # a dip in an uptrend must reach ≤ this (oversold-ish) to arm a buy
_RSI_PULLBACK_SHORT = 60.0   # a rally in a downtrend must reach ≥ this (overbought-ish) to arm a sell


def _nearest_level_within(price: float, levels: list[float], atr: float | None, mult: float = 1.0):
    """Nearest level within ``mult`` ATRs of price (a support a dip can bounce off / a resistance a
    rally can reject at), else None."""
    if not price or not atr or atr <= 0 or not levels:
        return None
    reach = mult * atr
    near = [lv for lv in levels if lv and abs(lv - price) <= reach]
    return min(near, key=lambda lv: abs(lv - price)) if near else None


def _htf_pullback_arm(base: "TradeProposal", resume_dir: Direction, ind: dict, tf0,
                      technical: "TechnicalRead", atr_v, rsi, px: float, htf_name: str, disable):
    """Arm a buy-the-dip / sell-the-rally resumption in the higher-TF direction, or None if the
    pullback doesn't qualify (caller then stands aside). Gated by RSI exhaustion + a confirmation
    (S/R bounce level OR RSI divergence). It arms a break in the trend direction — never a market
    entry into the countertrend move."""
    if "htf_pullback" in disable or rsi is None:
        return None
    is_long = resume_dir == Direction.LONG
    if not (rsi <= _RSI_PULLBACK_LONG if is_long else rsi >= _RSI_PULLBACK_SHORT):
        return None  # pullback not exhausted enough

    # Confirmation — at least one of: the dip is at a support / rally at a resistance, or a divergence.
    confirms: list[str] = []
    levels = list((tf0.support_levels if is_long else tf0.resistance_levels) if tf0 else [])
    near = _nearest_level_within(px, levels, atr_v)
    if near is not None:
        confirms.append(f"at {'support' if is_long else 'resistance'} ~{round(near, 6)}")
    div = (ind.get("div_bull") if is_long else ind.get("div_bear")) or 0.0
    if div == 1.0 and "divergence" not in disable:
        confirms.append("RSI divergence")
    if not confirms:
        return None  # a dip with no bounce level and no divergence — don't arm

    base.watch = True
    base.strategy = "htf_pullback"
    conf = round(min(0.7, 0.45 + 0.25 * technical.confidence), 2)
    base.conditional = _conditional_resumption(resume_dir, px, ind, atr_v,
                                               _key_levels(technical, tf0, px), conf)
    trend_word = "uptrend" if is_long else "downtrend"
    zone = "oversold" if is_long else "overbought"
    brk = "up" if is_long else "down"
    tail = (f"arming a {resume_dir.value} on a break back {brk} to join the trend."
            if base.conditional is not None
            else f"waiting for a clean break back {brk} to arm a {resume_dir.value} (no arm level yet).")
    base.rationale = (f"Higher-TF pullback: price pulled back into the {htf_name} {trend_word}, RSI "
                      f"{rsi:.0f} ({zone}), confirmed {' + '.join(confirms)} — {tail}")
    return base


# --- Range breakout — the counterpart to the range fade (regime == ranging) --------------------- #
_BREAKOUT_VOL = 1.2          # a break with volume >= 1.2x average is real, not a drift
_BREAKOUT_DECISIVE_ATR = 0.3  # ...or a decisive close >= 0.3 ATR beyond the range (if volume is absent)
_EMA_PULLBACK_ATR = 0.4      # the pullback low is "at" the EMA when within this many ATRs of it


def _range_breakout_decision(base: TradeProposal, ind: dict, tf0, htf_trend: str, disable) -> TradeProposal | None:
    """When a range RESOLVES — price closes beyond the prior range top/bottom, WITH the higher-TF
    trend (not against it) and a volume or decisive-close confirmation — trade the breakout with a
    measured-move target and a stop back inside the range. The counterpart to the range fade; returns
    None if there's no clean break (caller then falls through to mean-reversion)."""
    if "range_breakout" in disable:
        return None
    hi, lo = ind.get("prior_high"), ind.get("prior_low")
    last, atr = ind.get("last_close"), ind.get("atr14")
    if not (hi and lo and last and atr and atr > 0) or hi <= lo:
        return None
    if (hi - lo) < atr:          # too tight to be a tradeable range (noise)
        return None
    buf = 0.1 * atr
    height = hi - lo
    vol = ind.get("vol_ratio") or 0.0
    up_ok = last > hi + buf and htf_trend != "down" and (vol >= _BREAKOUT_VOL or last > hi + _BREAKOUT_DECISIVE_ATR * atr)
    dn_ok = last < lo - buf and htf_trend != "up" and (vol >= _BREAKOUT_VOL or last < lo - _BREAKOUT_DECISIVE_ATR * atr)
    if up_ok:
        direction, entry, stop = Direction.LONG, last, round(hi - buf, 6)
        risk = entry - stop
        tp = round(entry + min(height, _RR_MAX * risk), 6)
        edge, lvl = "top", hi
    elif dn_ok:
        direction, entry, stop = Direction.SHORT, last, round(lo + buf, 6)
        risk = stop - entry
        tp = round(entry - min(height, _RR_MAX * risk), 6)
        edge, lvl = "bottom", lo
    else:
        return None
    if risk <= 0 or abs(tp - entry) / risk < _MIN_RR_ENTRY:
        return None
    base.direction = direction
    base.entry, base.stop_loss, base.take_profit = round(entry, 6), stop, tp
    base.strategy = "range_breakout"
    base.confidence = 0.6
    conf_txt = f"volume {vol:.1f}x" if vol >= _BREAKOUT_VOL else "a decisive close"
    base.rationale = (f"Range breakout {direction.value.upper()}: closed beyond the range {edge} "
                      f"(~{round(lvl, 6)}) with {conf_txt} — measured-move target {base.take_profit}, "
                      f"stop back inside the range at {stop}.")
    return base


def _ema_pullback_decision(base: TradeProposal, ind: dict, direction: Direction, disable) -> TradeProposal | None:
    """A textbook trend-continuation entry: in a trend, price pulls back to the rising/falling EMA20
    (dynamic support/resistance) and closes back on the trend side. Enters with a TIGHT stop beyond
    the pullback extreme (better R:R than a mid-move entry). Returns None if it isn't at the EMA."""
    if "ema_pullback" in disable:
        return None
    ema = ind.get("ema20")
    last, lo, hi, atr = ind.get("last_close"), ind.get("last_low"), ind.get("last_high"), ind.get("atr14")
    if not (ema and last and atr and atr > 0):
        return None
    near = _EMA_PULLBACK_ATR * atr
    if direction == Direction.LONG:
        # dipped to/through the rising EMA and closed back above it (a bounce off dynamic support).
        if lo is None or not (lo <= ema + near and last > ema):
            return None
        entry = last
        stop = round(min(lo, ema) - 0.1 * atr, 6)
        risk = entry - stop
        tp = round(entry + _RR * risk, 6)
    else:
        if hi is None or not (hi >= ema - near and last < ema):
            return None
        entry = last
        stop = round(max(hi, ema) + 0.1 * atr, 6)
        risk = stop - entry
        tp = round(entry - _RR * risk, 6)
    if risk <= 0 or abs(tp - entry) / risk < _MIN_RR_ENTRY:
        return None
    base.direction = direction
    base.entry, base.stop_loss, base.take_profit = round(entry, 6), stop, tp
    base.strategy = "ema_pullback"
    base.confidence = 0.62
    side = "support" if direction == Direction.LONG else "resistance"
    base.rationale = (f"EMA20 pullback {direction.value.upper()}: price pulled back to the EMA20 "
                      f"(~{round(ema, 6)}) as dynamic {side} and closed back on the trend side — "
                      f"entering the continuation, stop {stop} beyond the pullback ({_RR:.0f}R target).")
    return base


def _failed_break_decision(base: TradeProposal, ind: dict, htf_trend: str, disable) -> TradeProposal | None:
    """Fade a FALSE breakout (liquidity sweep / stop-run): price pokes BEYOND the prior range then
    closes back inside — a bull/bear trap. Fade it back toward the range mean, stop beyond the sweep
    extreme. This is the FADE side that the backtest shows carries the edge (most range breaks fail).
    Refused against a clear immediate-higher-TF trend (don't fade a real trend break). None if no trap."""
    if "failed_break" in disable:
        return None
    hi, lo = ind.get("prior_high"), ind.get("prior_low")
    last, last_hi, last_lo, atr = ind.get("last_close"), ind.get("last_high"), ind.get("last_low"), ind.get("atr14")
    rsi = ind.get("rsi14")
    if not (hi and lo and last and last_hi and last_lo and atr and atr > 0) or hi <= lo:
        return None
    buf = 0.1 * atr
    mid = (hi + lo) / 2.0
    if last_hi > hi and last < hi - buf and htf_trend != "up":
        # swept above the range top then closed back inside = bull trap -> fade SHORT toward the mean
        direction, entry, stop, tp, swept, edge, trap = (
            Direction.SHORT, last, round(last_hi + buf, 6), round(mid, 6), hi, "top", "bull trap")
        risk = stop - entry
    elif last_lo < lo and last > lo + buf and htf_trend != "down":
        # swept below the range bottom then closed back inside = bear trap -> fade LONG toward the mean
        direction, entry, stop, tp, swept, edge, trap = (
            Direction.LONG, last, round(last_lo - buf, 6), round(mid, 6), lo, "bottom", "bear trap")
        risk = entry - stop
    else:
        return None
    if risk <= 0 or abs(tp - entry) / risk < _MIN_RR_ENTRY:
        return None
    base.direction = direction
    base.entry, base.stop_loss, base.take_profit = round(entry, 6), stop, tp
    base.strategy = "failed_break"
    base.confidence = 0.6
    rsi_txt = f", RSI {rsi:.0f}" if rsi is not None else ""
    base.rationale = (f"Failed break {direction.value.upper()} ({trap}): price swept the range {edge} "
                      f"(~{round(swept, 6)}) then closed back inside{rsi_txt} — fading the trap back toward "
                      f"the range mean {tp}, stop beyond the sweep at {stop}.")
    return base


# --- Trend alignment: how CLEARLY the direction stacks up (the "A+ / high-conviction" grade) ----- #
_ALIGN_AP = 0.85       # score at/above this = "A+" fully-aligned trend (everything points one way)
_ALIGN_A = 0.70        # ...A-grade
_ALIGN_BONUS = 0.08    # modest confidence bump when a trend is fully aligned (leans the desk in)


def _trend_alignment(technical: TechnicalRead, ind: dict, direction: Direction) -> float:
    """How clearly the direction stacks up, 0..1: timeframe agreement (dominant) + trend strength
    (ADX) + long-term-trend side (EMA200) + momentum expanding. 1.0 = every timeframe and signal
    points the same way — the clearest 'price is going up (or down)'. Exposed for the A+ badge +
    a small confidence bonus + backtest segmentation. NOT a new trade — just grades the trend trades."""
    want = "up" if direction == Direction.LONG else "down"
    tfs = technical.timeframes or []
    if not tfs:
        return 0.0
    agree = sum(1 for tf in tfs if _trend_from_indicators(tf.indicators, tf.trend) == want)
    tf_frac = agree / len(tfs)                                    # all TFs aligned = the core signal
    adx = ind.get("adx") or 0.0
    strong = 1.0 if adx >= _ADX_STRONG else max(0.0, adx / _ADX_STRONG)
    e200, last = ind.get("ema200"), ind.get("last_close")
    on_side = 1.0 if (e200 and last and ((direction == Direction.LONG) == (last > e200))) else 0.0
    h, hp = ind.get("macd_hist"), ind.get("macd_hist_prev")
    mom = 0.0
    if h is not None and ((h > 0) == (direction == Direction.LONG)):  # momentum with the trade
        mom = 1.0 if (hp is not None and abs(h) >= abs(hp)) else 0.6  # 1.0 if expanding, else aligned
    return round(0.45 * tf_frac + 0.25 * strong + 0.15 * on_side + 0.15 * mom, 2)


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
    chan_up = ind.get("chan_upper")
    chan_lo = ind.get("chan_lower")

    # The range EDGE the bar TAGGED then closed back inside (a rejection) — read TWO ways a pro reads
    # "price reached the edge of the range": a structural level (pivot S/R or swing) OR the PRICE
    # CHANNEL band (the regression channel's dynamic support/resistance). Either one, with RSI in the
    # (looser-than-trend) fade band, qualifies a fade to the mean.
    up_edges: list[float] = []
    if res is not None and (res - price) <= near and last_high is not None and last_high >= res:
        up_edges.append(res)
    if chan_up is not None and last_high is not None and last_high >= chan_up and price < chan_up:
        up_edges.append(chan_up)
    dn_edges: list[float] = []
    if sup is not None and (price - sup) <= near and last_low is not None and last_low <= sup:
        dn_edges.append(sup)
    if chan_lo is not None and last_low is not None and last_low <= chan_lo and price > chan_lo:
        dn_edges.append(chan_lo)

    direction = stop = target = edge = None
    edge_kind = ""
    if up_edges and rsi is not None and rsi >= _MR_RSI_OB and mean < price:
        direction = Direction.SHORT
        edge = max(up_edges)
        edge_kind = "resistance" if edge == res else "upper channel band"
        stop = max(edge, last_high) + _MR_STOP_ATR * atr
        target = mean                       # revert to the mean
        risk, reward = stop - price, price - target
    elif dn_edges and rsi is not None and rsi <= _MR_RSI_OS and mean > price:
        direction = Direction.LONG
        edge = min(dn_edges)
        edge_kind = "support" if edge == sup else "lower channel band"
        stop = min(edge, last_low) - _MR_STOP_ATR * atr
        target = mean
        risk, reward = price - stop, target - price
    else:
        base.rationale = ("Ranging — waiting for a rejection at a range edge (a structural level or "
                          "the price channel) with RSI in the fade band, before fading to the mean. "
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


def _first_level_beyond(direction: Direction, px: float, levels: list[float]) -> float | None:
    """Nearest key level in the trade direction (resistance above for a long / support below a short)."""
    if direction == Direction.LONG:
        ahead = [lv for lv in levels if lv > px]
        return min(ahead) if ahead else None
    behind = [lv for lv in levels if 0 < lv < px]
    return max(behind) if behind else None


def _momentum_action(base: TradeProposal, direction: Direction, ind: dict, tf0,
                     technical: TechnicalRead, atr_v: float | None, px: float,
                     momentum_ai: bool, symbol: str) -> tuple[str, TradeProposal | None]:
    """AI CLASSIFIES the momentum disagreement (healthy_pullback / weak_momentum / probable_reversal
    + evidence + confidence); the DETERMINISTIC engine here maps the class to an action. Returns
    ``(action, proposal)``:
      - ``('decided', base)`` — the engine armed a dip-limit / rejected from the classification: return it.
      - ``('enter', None)``   — healthy pullback AT value: take the normal market entry (fall through).
      - ``('fallback', None)``— no AI / low-confidence: keep the caller's original fixed arm-and-wait rule.
    The AI only labels; it never chooses direction/levels and never bypasses the downstream gates."""
    if not momentum_ai:
        return "fallback", None
    from app.agents.momentum_read import interpret_momentum

    read = interpret_momentum(symbol, direction, ind, technical, tf0.timeframe if tf0 else "")
    if read is None or read.confidence < _MOM_AI_MIN_CONF:
        return "fallback", None
    # Surface the classification to the UI ("What the analysis saw") regardless of the action chosen.
    base.momentum_read = {"category": read.category, "evidence": read.evidence,
                          "confidence": round(read.confidence, 2)}
    tag = f"[momentum AI: {read.category} · {round(read.confidence * 100)}% — {read.evidence}]"
    conf = round(min(0.7, 0.45 + 0.25 * technical.confidence + 0.1 * read.confidence), 2)
    ema20 = ind.get("ema20")

    if read.category == "probable_reversal":
        base.watch = True
        base.conditional = None
        base.rationale = (f"Probable reversal — standing aside, not arming into a turning "
                          f"{direction.value}. {tag}")
        return "decided", base

    if read.category == "weak_momentum":
        base.watch = True
        base.conditional = _conditional_resumption(
            direction, px, ind, atr_v, _key_levels(technical, tf0, px), conf)
        note = ("Armed a resumption break to wait for momentum to confirm."
                if base.conditional is not None else "Waiting for confirmation (no clean break to arm).")
        base.rationale = f"Weak momentum — waiting rather than entering. {note} {tag}"
        return "decided", base

    # healthy_pullback: buy the dip. AT value -> enter at market; still stretched from value -> arm a
    # LIMIT back at value for a better fill.
    stretched = bool(ema20 and atr_v and (
        (direction == Direction.LONG and px > ema20 + _VALUE_ENTRY_ATR * atr_v)
        or (direction == Direction.SHORT and px < ema20 - _VALUE_ENTRY_ATR * atr_v)))
    if not stretched:
        return "enter", None
    target = _first_level_beyond(direction, px, _key_levels(technical, tf0, px))
    cond = _conditional_pullback(direction, px, ema20, atr_v, ind, target, conf) if target else None
    if cond is None:
        return "fallback", None            # couldn't build a clean value entry -> keep the safe fixed arm
    base.watch = True
    base.conditional = cond
    base.rationale = (f"Healthy pullback (higher-TF momentum aligned) but price is stretched from value — "
                      f"armed a {cond.order_type} back at value (~EMA20) to buy the dip. {tag}")
    return "decided", base


_ST_BAND_MIN_RR = 1.5      # skip a signal whose structure target is closer than this
_ST_BAND_TP_R = 2.0        # fallback target (R) when there's no clean opposing S/R level
_ST_BAND_FRESH_FLIP = 3    # EARLY entry: only take the break within this many bars of a SuperTrend flip

# --- RSI-Over: a simple mean-reversion strategy. RSI at an extreme => price is stretched and likely
# to snap back; we take the reversal, but ONLY once the EMA10 confirms the turn (a close back through
# it). Overbought => SHORT, oversold => LONG. Scanned across the watchlist by app/agents/rsi_over.py.
_RSI_OVER_OB = 72.0        # RSI at/above this = overbought -> look for a SHORT
_RSI_OVER_OS = 28.0        # RSI at/below this = oversold  -> look for a LONG
_RSI_OVER_TP_R = 1.5       # target = this many R (risk = entry-to-stop, stop just beyond the recent swing)
_RSI_OVER_BUF_ATR = 0.2    # stop buffer beyond the recent swing high/low, as a fraction of ATR


def _supertrend_band_decision(base: TradeProposal, ind: dict, tf0, symbol: str) -> TradeProposal:
    """Mechanical SuperTrend + EMA20-band breakout with STRUCTURE-BASED exits. Enter WITH the
    SuperTrend when a candle closes beyond the EMA20 high/low band. The STOP sits just beyond the
    nearest support (long) / resistance (short); the TARGET is the next opposing S/R level — an ATR /
    2R fallback is used when structure is absent. Requires >= 1.5R; NO_TRADE inside the band or when
    the SuperTrend disagrees with the breakout side."""
    base.strategy = "supertrend_band"
    st_dir = ind.get("supertrend_dir")
    ema_h = ind.get("ema20_high")
    ema_l = ind.get("ema20_low")
    last = ind.get("last_close")
    atr_v = ind.get("atr14") or 0.0
    if None in (st_dir, ema_h, ema_l, last):
        base.rationale = "SuperTrend-band: not enough data to compute the SuperTrend / EMA20 band."
        return base

    if st_dir > 0 and last > ema_h:
        direction, is_long = Direction.LONG, True
    elif st_dir < 0 and last < ema_l:
        direction, is_long = Direction.SHORT, False
    else:
        if ema_l <= last <= ema_h:
            base.rationale = (f"SuperTrend-band: no trade — price {round(last, 6)} inside the EMA20 band "
                              f"[{round(ema_l, 6)}, {round(ema_h, 6)}].")
        else:
            base.rationale = ("SuperTrend-band: no trade — the breakout side doesn't match the "
                              f"SuperTrend ({'up' if st_dir > 0 else 'down'}).")
        return base

    # EARLY entry only: require a FRESH SuperTrend flip (skip late mid-trend band-breaks, which
    # backtested worst). bars_since_flip is None when the read couldn't compute it.
    bsf = ind.get("supertrend_bars_since_flip")
    if bsf is None or bsf > _ST_BAND_FRESH_FLIP:
        base.rationale = (f"SuperTrend-band: {direction.value} signal but not a fresh flip "
                          f"(bars since flip: {bsf}) — waiting for an early entry.")
        return base

    # Structure-based stop & target (support/resistance), with a beyond-the-wick buffer and ATR/2R
    # fallbacks when there's no clean level.
    buf = 0.2 * atr_v
    entry = last
    if is_long:
        sup, res = _nearest_below(tf0, ind, entry), _nearest_above(tf0, ind, entry)
        stop = (sup - buf) if sup is not None else (entry - 1.5 * atr_v)
        tp = res if res is not None else (entry + _ST_BAND_TP_R * (entry - stop))
        stop_src, tp_src = ("support" if sup is not None else "1.5xATR"), ("resistance" if res is not None else "2R")
    else:
        res, sup = _nearest_above(tf0, ind, entry), _nearest_below(tf0, ind, entry)
        stop = (res + buf) if res is not None else (entry + 1.5 * atr_v)
        tp = sup if sup is not None else (entry - _ST_BAND_TP_R * (stop - entry))
        stop_src, tp_src = ("resistance" if res is not None else "1.5xATR"), ("support" if sup is not None else "2R")

    risk = abs(entry - stop)
    if risk <= 0:
        base.rationale = "SuperTrend-band: no valid stop distance from structure."
        return base
    rr = abs(tp - entry) / risk
    if rr < _ST_BAND_MIN_RR:
        base.rationale = (f"SuperTrend-band: {direction.value} signal but the structure target is only "
                          f"{rr:.2f}R (< {_ST_BAND_MIN_RR}R) — skipping.")
        return base
    base.direction = direction
    base.entry, base.stop_loss, base.take_profit = round(entry, 6), round(stop, 6), round(tp, 6)
    base.confidence = 0.7
    base.rationale = (
        f"SuperTrend-band {direction.value.upper()}: SuperTrend {'up' if is_long else 'down'} + close "
        f"{'above EMA20-high' if is_long else 'below EMA20-low'}. Stop beyond {stop_src} "
        f"{base.stop_loss}, target at {tp_src} {base.take_profit} ({rr:.1f}R)."
    )
    return base


def _rsi_over_trigger(confirm: bool, macd: bool, rsi_div: bool,
                      ema_ok: bool, macd_ok: bool, div_ok: bool, macd_kind: str) -> str | None:
    """Which confirmation(s) fired for an RSI-Over entry, as a label — or None if not confirmed yet.
    EMA10 is the STRONG confirm; MACD (cross/divergence) is the EARLY one; RSI divergence is the
    exhaustion tell. With several enabled, the entry fires on EITHER (whichever confirms first)."""
    if not confirm and not macd and not rsi_div:
        return "RSI extreme only"
    parts = []
    if confirm and ema_ok:
        parts.append("EMA10 close")
    if macd and macd_ok:
        parts.append(f"MACD {macd_kind}")
    if rsi_div and div_ok:
        parts.append("RSI divergence")
    return " + ".join(parts) if parts else None


def _rsi_over_decision(base: TradeProposal, ind: dict, tf0, symbol: str,
                       confirm: bool = True, macd: bool = False, rsi_div: bool = False,
                       trend_filter: bool = True, macro: str = "sideways") -> TradeProposal:
    """Mechanical RSI-extreme mean-reversion. RSI >= _RSI_OVER_OB (overbought) -> SHORT; RSI <=
    _RSI_OVER_OS (oversold) -> LONG. The entry needs a confirmation of the turn (OR of the enabled):
      confirm (EMA10, default) = a close back through EMA10 — the STRONG, later confirmation;
      macd                     = a MACD signal-line cross OR MACD divergence — the EARLY entry;
      rsi_div                  = an RSI divergence (price extreme, momentum not = the exhaustion tell).
    ``trend_filter`` (default) REFUSES a fade AGAINST a clear higher-timeframe trend (``macro``) — the
    #1 protection against fading a runaway. Stop sits just beyond the recent swing; target _RSI_OVER_TP_R
    x the risk. The deterministic Risk Manager still sizes + gates every signal."""
    base.strategy = "rsi_over"
    r = ind.get("rsi14")
    ema10 = ind.get("ema10")
    last = ind.get("last_close")
    atr_v = ind.get("atr14") or 0.0
    rec_hi, rec_lo = ind.get("recent_high"), ind.get("recent_low")
    m_cross = ind.get("macd_cross") or 0.0
    m_div_bull, m_div_bear = ind.get("macd_div_bull") or 0.0, ind.get("macd_div_bear") or 0.0
    r_div_bull, r_div_bear = ind.get("div_bull") or 0.0, ind.get("div_bear") or 0.0  # RSI divergence
    if r is None or last is None or (confirm and ema10 is None):
        base.rationale = ("RSI-Over: not enough data (need RSI14"
                          + (", EMA10" if confirm else "") + ", last close).")
        return base

    # --- direction from the RSI extreme ---
    if r >= _RSI_OVER_OB:
        direction, is_long, zone, opp = Direction.SHORT, False, "overbought", "up"
        ema_ok = ema10 is not None and last < ema10
        macd_ok = m_cross == -1.0 or m_div_bear == 1.0
        macd_kind = "cross" if m_cross == -1.0 else ("divergence" if m_div_bear == 1.0 else "")
        div_ok = r_div_bear == 1.0
    elif r <= _RSI_OVER_OS:
        direction, is_long, zone, opp = Direction.LONG, True, "oversold", "down"
        ema_ok = ema10 is not None and last > ema10
        macd_ok = m_cross == 1.0 or m_div_bull == 1.0
        macd_kind = "cross" if m_cross == 1.0 else ("divergence" if m_div_bull == 1.0 else "")
        div_ok = r_div_bull == 1.0
    else:
        base.rationale = (f"RSI-Over: RSI {r:.0f} is not in an extreme zone "
                          f"(need >= {_RSI_OVER_OB:.0f} or <= {_RSI_OVER_OS:.0f}).")
        return base

    # --- #1 TREND FILTER: don't fade AGAINST a clear higher-timeframe trend (a fade into a runaway
    # is how RSI mean-reversion blows up). Only fade WITH or neutral to the higher TF. ---
    if trend_filter and macro == opp:
        base.rationale = (f"RSI-Over: RSI {r:.0f} {zone}, but NOT fading against the higher-timeframe "
                          f"{macro}trend — a {direction.value} here fights the trend (a pullback in a "
                          f"trend, not a top/bottom). Standing aside.")
        return base

    # --- confirmation (OR of the enabled sources) ---
    trig = _rsi_over_trigger(confirm, macd, rsi_div, ema_ok, macd_ok, div_ok, macd_kind)
    if trig is None:
        waits = [w for w, on in (("EMA10 close", confirm), ("a MACD cross/divergence", macd),
                                 ("an RSI divergence", rsi_div)) if on]
        base.rationale = (f"RSI-Over: RSI {r:.0f} {zone} but the turn has not confirmed yet "
                          f"(waiting for {' or '.join(waits)}).")
        return base

    # --- stop just beyond the recent swing; target _RSI_OVER_TP_R x the risk ---
    buf = _RSI_OVER_BUF_ATR * atr_v
    if is_long:
        stop = (rec_lo - buf) if rec_lo is not None else (last - 1.5 * atr_v)
        if stop >= last:
            stop = last - 1.5 * atr_v
        risk, tp = last - stop, None
        tp = last + _RSI_OVER_TP_R * risk
    else:
        stop = (rec_hi + buf) if rec_hi is not None else (last + 1.5 * atr_v)
        if stop <= last:
            stop = last + 1.5 * atr_v
        risk = stop - last
        tp = last - _RSI_OVER_TP_R * risk

    if risk <= 0:
        base.rationale = "RSI-Over: invalid stop distance (no room between entry and the recent swing)."
        return base
    base.direction = direction
    base.entry, base.stop_loss, base.take_profit = round(last, 6), round(stop, 6), round(tp, 6)
    base.confidence = 0.7
    base.rationale = (
        f"RSI-Over {direction.value.upper()}: RSI {r:.0f} {zone} — confirmed by {trig}. "
        f"Stop beyond the recent {'low' if is_long else 'high'} {base.stop_loss}, "
        f"target {base.take_profit} ({_RSI_OVER_TP_R:.1f}R)."
    )
    return base


def _deterministic_decision(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead, now: datetime,
    trend_only: bool = False, st_band: bool = False, rsi_over: bool = False,
    rsi_confirm: bool = True, rsi_macd: bool = False, rsi_div: bool = False, rsi_trend_filter: bool = True,
    disable: frozenset[str] = frozenset(), momentum_ai: bool = False,
) -> TradeProposal:
    # ``disable`` is a BACKTEST-ONLY filter-ablation switch (the live path never passes it): naming a
    # gate ("mtf", "momentum", "structure", "volatility", "divergence", "minrr", "rsi_extreme") skips it, so the
    # backtest can measure each filter's contribution (keep it if removing it hurts).
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
    macro = _macro_trend(technical)                                       # highest-TF context (fades)
    htf_trend, htf_name = _higher_trend(technical, timeframe)             # IMMEDIATE higher TF (laddered)
    big_levels = _big_tf_levels(technical, timeframe)
    bias = fundamental.bias

    # --- regime FIRST (the senior-trader read): pick the strategy the regime permits ---
    adx_v = ind.get("adx")
    regime = _regime(ind)
    policy = regime_policy(regime)
    base.regime = regime
    base.strategy = policy["strategy"]
    # SuperTrend-band breakout mode: a dedicated mechanical strategy replaces the swing logic.
    if st_band:
        return _supertrend_band_decision(base, ind, tf0, symbol)
    # RSI-Over mode: a dedicated mechanical RSI-extreme mean-reversion, confirmed by EMA10/MACD/RSI-div
    # and (default) filtered against fading a strong higher-timeframe trend.
    if rsi_over:
        return _rsi_over_decision(base, ind, tf0, symbol, confirm=rsi_confirm, macd=rsi_macd,
                                  rsi_div=rsi_div, trend_filter=rsi_trend_filter, macro=macro)
    # Trend-only mode: only trade a CLEAR trend (ADX >= 25 -> "trending"); stand aside in moderate /
    # ranging / volatile. Backtests show the trend regime is the edge while moderate+ranging are net
    # drags (same return, ~40% more drawdown when included). The live default comes from the setting.
    if trend_only and regime != "trending":
        base.strategy = "stand_aside"
        base.watch = True
        base.rationale = (f"Trend-only mode: standing aside — regime is {regime}, not a clear "
                          f"(ADX≥{_ADX_STRONG:.0f}) trend. {policy['note']}")
        return base
    # Ranging: first check for a BREAKOUT (the range resolving out with the higher-TF trend + volume);
    # else fade the range edges back to the mean. Together they cover both range outcomes.
    if regime == "ranging":
        brk = _range_breakout_decision(base, ind, tf0, htf_trend, disable)
        if brk is not None:
            return brk
        fail = _failed_break_decision(base, ind, htf_trend, disable)
        if fail is not None:
            return fail
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
    # LADDERED higher-TF context (htf_trend / big_levels computed above): the gate uses the IMMEDIATE
    # higher timeframe (15m→1h, 1h→4h, 4h→1d); the big TFs (4h/1d) are respected as LEVELS below.
    px = ind.get("last_close") or 0.0
    # The TREND (technical) decides direction. The fundamental bias is a soft macro lean — the
    # fundamental agent self-rates ~0.3 ("not a primary signal"), so it only NUDGES confidence
    # below; it must not veto a clean technical trend. Direction is still gated by the immediate
    # higher-timeframe trend (don't fight the next TF up) + the big-TF levels + the momentum wait.
    if trend == "up":
        # Higher-TF DOWN + entry-TF rallied up = a rally in the bigger downtrend. Try to SELL the
        # rally (arm a short in the higher-TF direction, confirmed by S/R or RSI divergence); if it
        # doesn't qualify, stand aside — don't buy into / against the higher TF.
        if htf_trend == "down" and "mtf" not in disable:
            pull = _htf_pullback_arm(base, Direction.SHORT, ind, tf0, technical, atr_v, rsi, px, htf_name, disable)
            if pull is not None:
                return pull
            base.rationale = (f"No confluence: the next-higher timeframe ({htf_name}) is DOWN — "
                              "not buying into it.")
            return base
        if macd_hist is not None and macd_hist < -mom_thresh and "momentum" not in disable:
            px = ind.get("last_close") or 0.0
            # AI classifies WHY momentum disagrees; the engine decides enter/arm/reject. When the AI is
            # off / unavailable / low-confidence it returns 'fallback' and we keep the fixed arm below.
            action, decided = _momentum_action(base, Direction.LONG, ind, tf0, technical, atr_v, px,
                                                momentum_ai, symbol)
            if action == "decided":
                return decided
            if action != "enter":
                # (fixed rule) Trend up but momentum meaningfully down = pullback. Arm a resumption
                # break instead of just waiting, so it fires when momentum turns back up.
                base.watch = True
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
            # action == 'enter' (healthy pullback AT value) -> fall through to the normal market entry
        lvl = _opposing_big_tf_level(Direction.LONG, px, atr_v, big_levels)
        if lvl is not None and "htf_level" not in disable:
            base.watch = True
            base.rationale = (f"Respecting a higher-timeframe level: major resistance ~{round(lvl, 6)} "
                              f"just overhead (within {_HTF_LEVEL_ATR:.0f} ATR). Waiting for a break or a "
                              "cleaner pullback before buying into it.")
            return base
        direction = Direction.LONG
    elif trend == "down":
        # Higher-TF UP + entry-TF dipped down = a dip in the bigger uptrend. Try to BUY the dip (arm
        # a long in the higher-TF direction, confirmed by S/R or RSI divergence); if it doesn't
        # qualify, stand aside — don't sell into / against the higher TF.
        if htf_trend == "up" and "mtf" not in disable:
            pull = _htf_pullback_arm(base, Direction.LONG, ind, tf0, technical, atr_v, rsi, px, htf_name, disable)
            if pull is not None:
                return pull
            base.rationale = (f"No confluence: the next-higher timeframe ({htf_name}) is UP — "
                              "not selling into it.")
            return base
        if macd_hist is not None and macd_hist > mom_thresh and "momentum" not in disable:
            px = ind.get("last_close") or 0.0
            action, decided = _momentum_action(base, Direction.SHORT, ind, tf0, technical, atr_v, px,
                                                momentum_ai, symbol)
            if action == "decided":
                return decided
            if action != "enter":
                base.watch = True
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
            # action == 'enter' (healthy pullback AT value) -> fall through to the normal market entry
        lvl = _opposing_big_tf_level(Direction.SHORT, px, atr_v, big_levels)
        if lvl is not None and "htf_level" not in disable:
            base.watch = True
            base.rationale = (f"Respecting a higher-timeframe level: major support ~{round(lvl, 6)} "
                              f"just below (within {_HTF_LEVEL_ATR:.0f} ATR). Waiting for a break or a "
                              "cleaner pullback before selling into it.")
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
    if against_struct and "structure" not in disable:
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
    if regime == "volatile" and vol_ratio is not None and vol_ratio >= _REGIME_VOL_EXTREME and "volatility" not in disable:
        base.watch = True
        base.rationale = (
            f"Volatile regime: volatility is expanding sharply (ATR {vol_ratio:.1f}x its baseline) "
            f"without a strong trend (ADX {adx_v}). High whipsaw risk — standing aside until it "
            "settles."
        )
        return base

    # Trend ALIGNMENT grade (the "A+ / clear direction" score) — how cleanly every TF + signal stacks.
    align = _trend_alignment(technical, ind, direction)
    base.alignment = align

    # EMA20 pullback: if price is bouncing off the EMA20 (a textbook continuation entry), take it with
    # a tight stop beyond the pullback rather than the generic mid-move entry. Falls through otherwise.
    ema_pb = _ema_pullback_decision(base, ind, direction, disable)
    if ema_pb is not None:
        ema_pb.alignment = align
        if align >= _ALIGN_AP and "alignment" not in disable:
            ema_pb.confidence = round(min(0.95, ema_pb.confidence + _ALIGN_BONUS), 2)  # fully aligned — lean in
        return ema_pb

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
    if div_against and overextended and "divergence" not in disable:
        base.watch = True
        base.rationale = (
            f"Momentum divergence against the {direction.value} with price extended (RSI {rsi}) — "
            "exhaustion risk. Waiting for momentum to realign before entering."
        )
        return base

    # --- RSI overextension (respect the pullback): entering a trend LONG at an overbought RSI (or a
    # SHORT at oversold) is chasing into a likely pullback. Unless it's a STRONG trend (ADX>=strong)
    # with momentum STILL confirming — a real trend can ride an extreme — don't take the market entry;
    # ARM a pullback-resumption so we join on the dip/bounce when the trend resumes, instead of
    # chasing. (Ablation switch: "rsi_extreme" in `disable` skips this.) ---
    # (Momentum meaningfully ROLLING OVER at the trend was already caught + armed above, so here it's
    # with-or-flat; a STRONG trend riding the extreme is the only case we still let enter at market.)
    rsi_extreme = rsi is not None and (
        (direction == Direction.LONG and rsi >= _RSI_TREND_OB)
        or (direction == Direction.SHORT and rsi <= _RSI_TREND_OS)
    )
    strong_trend = adx_v is not None and adx_v >= _ADX_STRONG
    if rsi_extreme and not strong_trend and "rsi_extreme" not in disable:
        px = ind.get("last_close") or 0.0
        action, decided = _momentum_action(base, direction, ind, tf0, technical, atr_v, px,
                                            momentum_ai, symbol)
        if action == "decided":
            return decided
        if action != "enter":
            base.watch = True
            base.conditional = _conditional_resumption(
                direction, px, ind, atr_v, _key_levels(technical, tf0, px),
                round(min(0.7, 0.45 + 0.25 * technical.confidence), 2))
            zone = "overbought" if direction == Direction.LONG else "oversold"
            dip = "dip" if direction == Direction.LONG else "bounce"
            armed_note = (f"Armed a {direction.value} pullback-resumption to join on the {dip}."
                          if base.conditional is not None
                          else f"Waiting for the {dip} (no clean break level to arm yet).")
            base.rationale = (
                f"{direction.value.upper()} trend but RSI {round(rsi)} is {zone} — a pullback is likely and "
                f"the trend isn't strong-enough-with-momentum to ride it, so not chasing at market. {armed_note}"
            )
            return base
        # action == 'enter' (healthy pullback AT value) -> fall through to the normal market entry

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
            # (Ablation: with "minrr" disabled, take the thin trade anyway to measure the floor.)
            take_market = "minrr" in disable
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
    if macro == trend and "mtf" not in disable:
        conf += 0.15  # higher-timeframe agrees
    if adx_v is not None and adx_v >= _ADX_STRONG:
        conf += 0.1
    vr = ind.get("vol_ratio")
    if vr is not None and vr > 1.2:
        conf += 0.1
    # NOTE: the entry-TF "MACD agrees with direction" +0.05 bonus was REMOVED as redundant — in
    # trend-only mode we enter WITH momentum by construction, so it was handed to nearly every setup
    # (non-discriminating) and duplicated the trend signal. Ablation: removing it tightened the 70%
    # gate to the genuinely-confident setups and improved expectancy (small sample; a robustness cut,
    # not a returns play). The CROSS-timeframe MACD conflict below is kept — it carries distinct info.
    # Cross-timeframe momentum conflict: the higher-TF MACD pushing AGAINST the trade is a
    # lower-conviction signal (the XAU short was taken with 1h vs 4h MACD disagreeing).
    macro_tf = _macro_tf(technical)
    macro_macd = macro_tf.indicators.get("macd_hist") if macro_tf else None
    macro_conflict = macro_macd is not None and (
        (direction == Direction.LONG and macro_macd < 0) or
        (direction == Direction.SHORT and macro_macd > 0)
    )
    if macro_conflict and "mtf" not in disable:
        conf -= 0.1
    # Entry LOCATION (anti-chase, graded): score the entry by its distance from value (EMA20) in
    # ATRs. A pullback to value is the pro's entry (reward it); the further it's stretched the more
    # we down-weight it — chasing far from value is where losers come from (today's HK50 short was
    # chased to the low and squeezed). The bigger haircut pushes chased setups below the Hybrid
    # threshold and ranks pullbacks above them in the scanner.
    value_dist = abs(entry - ema20) / atr_v if (ema20 and atr_v and atr_v > 0) else None
    at_value = value_dist is not None and value_dist <= _VALUE_ENTRY_ATR
    if value_dist is not None and "chase" not in disable:
        if at_value:
            conf += 0.1                       # pullback to value — preferred entry
        elif value_dist >= _PULLBACK_ATR:
            conf -= 0.18                      # chasing far from value — strong anti-chase
        elif value_dist >= _STRETCHED_ATR:
            conf -= 0.06                      # getting stretched
    # MACD histogram EXPANDING vs FADING (the checklist's "growing bars"): a histogram rising in the
    # trade direction = momentum still building (reward); shrinking = momentum fading even if still
    # aligned (down-weight). Soft factor, toggle "macd_rising".
    macd_hist_prev = ind.get("macd_hist_prev")
    if macd_hist is not None and macd_hist_prev is not None and "macd_rising" not in disable:
        rising = (macd_hist > macd_hist_prev) if direction == Direction.LONG else (macd_hist < macd_hist_prev)
        conf += 0.05 if rising else -0.06
    # NOTE: a regression-channel "don't buy into the upper (resistance) band" confidence factor was
    # tested (analysis/channel_test.md) and REMOVED — it was slightly worse overall and clearly worse
    # OUT-OF-SAMPLE. Reason: this is a TREND-following engine, and in a real trend price legitimately
    # RIDES and BREAKS the upper band (continuation), so penalising "near resistance" cut good trend
    # trades. The channel is still COMPUTED (chan_pos/chan_r2) and drawn on the chart for the user's
    # own read, but it does not gate the decision. Level-rejection logic suits range/reversal trading,
    # not trend continuation.
    #
    # Map-read WALL PROXIMITY (the scorecard's 🔴 Resistance/Support item), done the RIGHT way: only a
    # WEAK trend gets penalised for running into a nearby barrier (a strong trend breaks through — the
    # lesson from the failed channel factor). And a key level just CLEARED behind the entry on rising
    # volume is a confirmed break -> reward. `nearest` is the nearest level AHEAD; `levels` are all S/R.
    if not strong and nearest is not None and atr_v and atr_v > 0 and "wall" not in disable:
        head_atr = abs(nearest - entry) / atr_v            # headroom before the barrier
        if head_atr < _WALL_NEAR_ATR:
            conf -= _WALL_PENALTY                           # chasing into a wall with no break-power
    if atr_v and atr_v > 0 and "wall" not in disable:
        vt_break = ind.get("vol_trend")
        behind = [lv for lv in levels if (lv < entry if direction == Direction.LONG else lv > entry)]
        just_cleared = behind and min(abs(entry - lv) for lv in behind) <= _WALL_BEHIND_ATR * atr_v
        if just_cleared and vt_break is not None and vt_break > 0:
            conf += _WALL_BREAK_BONUS                       # broke a level on rising volume — continuation
    # NOTE: a VOLUME-TREND confidence factor (expanding volume into the move = +, fading = -) was
    # tested on walk-forward (analysis/map_factors.md) and REMOVED as a confidence input — it was
    # WORSE both in- and out-of-sample (OOS +0.121R vs +0.144R base). Raison: this is a trend engine,
    # and real trends routinely GRIND higher on FADING volume while volume SPIKES often mark climaxes/
    # reversals — so volume slope is a poor conviction signal here. The `vol_trend` indicator is still
    # computed (it powers the wall breakout bonus above and the 🗺️ Read scorecard), just not scored.
    # The WALL-proximity factor (tested alongside) DID help both IS and OOS and is kept, above.
    rsi = ind.get("rsi14")
    if rsi is not None and "rsi_extreme" not in disable and (
            (direction == Direction.LONG and rsi >= _RSI_OB)
            or (direction == Direction.SHORT and rsi <= _RSI_OS)):
        conf -= 0.1  # entering when already stretched
    # Long-term trend (EMA200): reward being on the right side of the 200-EMA, penalise against it.
    e200 = ind.get("ema200")
    if e200 and "ema200" not in disable:
        regime_ok = (direction == Direction.LONG and entry >= e200) or \
                    (direction == Direction.SHORT and entry <= e200)
        conf += 0.05 if regime_ok else -0.05
    # Market structure: aligned swings (HH/HL for a long, LH/LL for a short) add real conviction;
    # trading against structure or right after a change-of-character (CHoCH) subtracts it. This is
    # the chart-reader's "is price action actually confirming this?" check.
    if "structure" not in disable:
        if struct != "range":
            aligned = (direction == Direction.LONG and struct == "up") or (
                direction == Direction.SHORT and struct == "down"
            )
            conf += 0.1 if aligned else -0.1
        if ind.get("choch"):
            conf -= 0.1
    # RSI divergence: regular divergence AGAINST the trade is exhaustion (down-weight); hidden
    # divergence WITH the trade is continuation confirmation (up-weight).
    if "divergence" not in disable:
        if div_against:
            conf -= 0.12
        if div_with:
            conf += 0.07
    # Regime: a clean trend is the engine's edge; a volatile (expanding, trendless) tape is lower
    # conviction even when a setup forms.
    if regime == "volatile" and "volatility" not in disable:
        conf -= 0.1
    # Session/liquidity: lean into the liquid windows, discount thin hours (noise, wide spreads).
    # Thin-hour entries validated as materially lower-quality (backtest: ~+0.10R vs ~+0.24R in
    # active/normal, and it holds in- AND out-of-sample), and their real spread/slippage cost is
    # under-modelled — so the thin discount is DOUBLED to -0.10. This is a soft filter: a strong thin
    # setup still clears the confidence bar; a marginal one now falls below it (esp. the 70% Hybrid gate).
    session_q, _session_note = _session_quality(asset_class, symbol, now)
    if "session" not in disable:
        if session_q == "active":
            conf += 0.05
        elif session_q == "thin":
            conf -= 0.10
    # Trend ALIGNMENT: a fully-stacked trend (every TF + strength + momentum agree) is the clearest,
    # highest-conviction direction — lean in. (The base factors only check the single highest TF, so
    # full multi-TF agreement is extra edge.) Modest bonus so it ranks A+ setups up without inflating.
    if align >= _ALIGN_AP and "alignment" not in disable:
        conf += _ALIGN_BONUS
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


def _apply_guardrails(proposal: TradeProposal, technical: TechnicalRead) -> TradeProposal:
    """Thin, capital-protective checks on an AI-led proposal — the only deterministic gates left in
    AI-led mode (the mechanical entry filters are folded into the AI's judgment). Any failure converts
    the proposal to NO_TRADE. The Risk Manager remains the final authority downstream."""
    if proposal.direction == Direction.NO_TRADE:
        return proposal
    d = proposal.direction
    entry, stop, tp = proposal.entry, proposal.stop_loss, proposal.take_profit

    def _block(why: str) -> TradeProposal:
        log.info("AI proposal blocked by guardrail", extra={"symbol": proposal.symbol, "why": why})
        proposal.rationale = f"AI {d.value.upper()} blocked ({why}). {proposal.rationale}"
        proposal.direction = Direction.NO_TRADE
        proposal.entry = proposal.stop_loss = proposal.take_profit = None
        proposal.confidence = 0.0
        proposal.review_decision = "veto"
        return proposal

    if entry is None or stop is None or tp is None:
        return _block("incomplete entry/stop/target")
    if d == Direction.LONG and not (stop < entry < tp):
        return _block("stop/target on the wrong side of entry for a long")
    if d == Direction.SHORT and not (tp < entry < stop):
        return _block("stop/target on the wrong side of entry for a short")
    risk = abs(entry - stop)
    if risk <= 0:
        return _block("zero risk distance")
    rr = abs(tp - entry) / risk
    if rr < _MIN_RR_ENTRY:
        return _block(f"reward:risk {rr:.2f} below the {_MIN_RR_ENTRY:.1f}R floor")
    # Don't fight a strong higher-timeframe trend (the one load-bearing deterministic check we keep).
    macro = _macro_trend(technical)
    if (d == Direction.LONG and macro == "down") or (d == Direction.SHORT and macro == "up"):
        return _block(f"against the higher-timeframe trend (macro {macro})")
    # ATR sanity: the stop must be a sane distance — not a hair-trigger, not absurdly wide.
    tf0 = next((x for x in technical.timeframes if x.timeframe == proposal.timeframe),
               technical.timeframes[0]) if technical.timeframes else None
    atr = tf0.indicators.get("atr14") if tf0 else None
    if atr:
        if risk < 0.25 * atr:
            return _block("stop too tight vs ATR (hair-trigger)")
        if risk > 6.0 * atr:
            return _block("stop too wide vs ATR")
    return proposal


def run_orchestrator(
    symbol: str, asset_class: AssetClass, timeframe: str,
    technical: TechnicalRead, fundamental: FundamentalRead,
    now: datetime | None = None, use_llm: bool = True, trend_only: bool = False,
    st_band: bool = False, rsi_over: bool = False, rsi_confirm: bool = True, rsi_macd: bool = False,
    rsi_div: bool = False, rsi_trend_filter: bool = True,
    disable: frozenset[str] = frozenset(),
    ai_review: bool = True, momentum_ai: bool = False,
) -> TradeProposal:
    """Deterministic engine decides; the LLM may only CONFIRM or VETO (never widen).

    1. The deterministic strategy (regime/MTF/ADX/momentum/ATR/structure gates) is the source
       of truth — it picks direction, entry, stop, target, and can say NO_TRADE.
    2. If it declined, we return NO_TRADE — the LLM cannot create a trade the rules reject.
    3. If it proposed a trade and the LLM is enabled, the LLM reviews it as a risk-aware
       second opinion: confirm (optionally lowering confidence) or veto with reasons. It can
       never change direction/levels or raise risk. The Risk Manager remains final downstream.

    (The AI as the DECIDER is a separate, richer layer — ``ai_decider.ai_decide_trade`` at the pipeline
    level, gated by the 'AI decides' toggle. That replaced the old AI-led branch that used to live here.)
    """
    now = now or datetime.now(timezone.utc)

    proposal = _deterministic_decision(symbol, asset_class, timeframe, technical, fundamental, now,
                                       trend_only=trend_only, st_band=st_band, rsi_over=rsi_over,
                                       rsi_confirm=rsi_confirm, rsi_macd=rsi_macd, rsi_div=rsi_div,
                                       rsi_trend_filter=rsi_trend_filter, disable=disable,
                                       momentum_ai=momentum_ai)

    # SuperTrend-band and RSI-Over are purely mechanical strategies — no LLM confirm/veto over them.
    # ai_review=False takes the AI out of the trade decision entirely (the deterministic engine +
    # confidence gate decide; the AI is kept only for the fundamental read upstream). Default per the
    # repeatability finding that the reviewer isn't a stable filter.
    if (st_band or rsi_over or proposal.direction == Direction.NO_TRADE or not use_llm
            or not llm_available() or not ai_review):
        log.info("orchestrator decision (deterministic)",
                 extra={"symbol": symbol, "direction": proposal.direction.value,
                        "strategy": proposal.strategy})
        return proposal

    # --- LLM review of the deterministic setup (confirm / veto only) ---
    # The checklist depends on the STRATEGY: a ranging mean-reversion FADE must NOT be judged like a
    # trend trade (it is deliberately counter to the last leg, in a low-ADX market — those are the
    # premise, not flaws), or the reviewer would veto every fade.
    if proposal.strategy == "mean_reversion":
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
