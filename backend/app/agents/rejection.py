"""REJECTION SCORE — is a rejection structurally meaningful, or just a wick?

The engine reads trend well but has no answer to "price is here, will it be REFUSED?". This scores
that, deterministically, from five groups of evidence, ranked the way they actually earn money:

    location  >  rejection quality  >  structure  >  momentum  >  volume

That ordering is not a preference, it is what this book's own measurements showed. ``daily_align``
— a pure location filter — is the only thing that ever moved direction accuracy off a coin flip
(52 -> 55 per 100, 56 excluding forex). ``trend_slope`` and ``adx_rising`` — momentum — failed
out-of-sample and were removed. So location carries the most points and momentum the fewest.

INFORMATION ONLY. Nothing here gates, sizes or vetoes a trade. It is scored, shown, and left alone
until a holdout backtest says the weights are worth something — six ideas in this codebase have
already died out-of-sample, and a fifteen-component weighted score has far more freedom to fit
noise than any single filter did.

Two things deliberately NOT scored:

* Order flow / delta. MT5 exposes ``tick_volume`` (the count of price changes), not traded volume,
  and there is no bid/ask split for these CFDs. "Aggressive buying was absorbed" is not measurable
  on this feed, and a proxy dressed up as order flow would be worse than its absence.
* Volume is capped at 1 point for the same reason: tick volume measures ACTIVITY, not selling.
"""
from __future__ import annotations

from app.agents.indicators import atr, macd, rsi, session_vwap
from app.core.logging import get_logger
from app.models.schemas import Candle

log = get_logger("agents.rejection")

# Wick must be this many times the body to count as a rejection tail.
_WICK_BODY = 1.5
# "At" a level means within this many ATR of it.
_AT_LEVEL_ATR = 0.5
# How far back to look for the sweep / the level being tested.
_LOOKBACK = 6
# Tick volume this many times the recent average counts as a volume-backed rejection.
_VOL_MULT = 1.5

MAX_SCORE = 19


def _band(score: int) -> tuple[str, str]:
    """Score -> verdict. Bands mirror the ratios in the original design, scaled to MAX_SCORE."""
    if score >= 13:
        return "high", "high-quality rejection — location, price action and structure all agree"
    if score >= 9:
        return "valid", "valid rejection — enough structural evidence to act on"
    if score >= 5:
        return "weak", "weak — something rejected here, but it is not confirmed. Wait."
    return "none", "no meaningful rejection — this is a wick, not a refusal"


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def _upper_wick(c: Candle) -> float:
    return c.high - max(c.open, c.close)


def _lower_wick(c: Candle) -> float:
    return min(c.open, c.close) - c.low


def score_rejection(
    candles: list[Candle],
    *,
    levels: list[dict] | None = None,
    key_levels: dict | None = None,
    swings: dict | None = None,
) -> dict | None:
    """Score the most recent rejection, in whichever direction one is present.

    ``levels``     — multi-TF S/R dicts ({price, tf, tests}) for the LOCATION score.
    ``key_levels`` — prior day/week high-low + today's open (the liquidity pools desks watch).
    ``swings``     — market_structure() output, for the STRUCTURE score.

    Returns None when there is nothing to score (too little data, or no rejection candle at all).
    """
    if len(candles) < 60:
        return None
    a = atr(candles, 14) or 0.0
    if a <= 0:
        return None

    last = candles[-1]
    recent = candles[-_LOOKBACK:]
    body = _body(last) or (a * 0.05)     # a doji has ~no body; floor it so ratios stay finite
    up_w, lo_w = _upper_wick(last), _lower_wick(last)

    # Which side is being refused? The bigger tail wins; if neither tail is meaningful there is no
    # rejection to score, and saying so is more useful than scoring noise out of politeness.
    bearish = up_w >= lo_w
    wick = up_w if bearish else lo_w
    if wick < _WICK_BODY * body and wick < 0.25 * a:
        return None

    direction = "short" if bearish else "long"
    parts: list[dict] = []
    score = 0

    def add(group: str, pts: int, label: str, hit: bool) -> None:
        nonlocal score
        if hit:
            score += pts
        parts.append({"group": group, "points": pts if hit else 0, "max": pts, "label": label, "hit": hit})

    # --- LOCATION (0-5) ------------------------------------------------------------------------
    # The single most important group. A rejection in the middle of nowhere is a wick; the same
    # candle at a level other traders are watching is a refusal.
    px = last.high if bearish else last.low
    near = lambda p: abs(px - p) <= _AT_LEVEL_ATR * a   # noqa: E731

    htf = [lv for lv in (levels or []) if lv.get("tf") in ("4h", "1d") and near(float(lv["price"]))]
    add("location", 2, f"at a {'/'.join(sorted({str(l['tf']).upper() for l in htf})) or '4H/1D'} level", bool(htf))

    pools: list[str] = []
    for name, key in (("prior day high", "prior_day_high"), ("prior day low", "prior_day_low"),
                      ("prior week high", "prior_week_high"), ("prior week low", "prior_week_low")):
        v = (key_levels or {}).get(key)
        if v is not None and near(float(v)):
            pools.append(name)
    # Equal highs/lows in the recent window are a liquidity pool too — resting stops sit just past
    # them, which is exactly what a sweep is reaching for.
    prior = candles[-40:-1]
    if prior:
        ref = max(c.high for c in prior) if bearish else min(c.low for c in prior)
        touches = sum(1 for c in prior if abs((c.high if bearish else c.low) - ref) <= 0.15 * a)
        if touches >= 2 and near(ref):
            pools.append("equal highs" if bearish else "equal lows")
    add("location", 2, f"at {', '.join(pools)}" if pools else "at a prior high/low or liquidity pool", bool(pools))

    vw = session_vwap(candles)
    v_last = vw[-1] if vw else None
    vwap_hit = v_last is not None and (
        (bearish and last.high >= v_last >= last.close) or (not bearish and last.low <= v_last <= last.close)
    )
    add("location", 1, "rejected at VWAP", bool(vwap_hit))

    # --- REJECTION QUALITY (0-5) ---------------------------------------------------------------
    add("rejection", 1, f"long {'upper' if bearish else 'lower'} wick vs body ({wick / body:.1f}x)",
        wick >= _WICK_BODY * body)

    # Where the candle CLOSES matters far more than how long the wick is.
    rng = max(last.high - last.low, 1e-9)
    closed_back = ((last.high - last.close) / rng >= 0.6) if bearish else ((last.close - last.low) / rng >= 0.6)
    add("rejection", 2, "closed back in the lower third" if bearish else "closed back in the upper third", closed_back)

    # Liquidity sweep: traded BEYOND the prior extreme, then closed back inside it.
    swept = False
    if prior:
        ref = max(c.high for c in prior) if bearish else min(c.low for c in prior)
        swept = (last.high > ref and last.close < ref) if bearish else (last.low < ref and last.close > ref)
    add("rejection", 2, "swept the prior high then closed back below" if bearish
        else "swept the prior low then closed back above", swept)

    # --- STRUCTURE (0-5) -----------------------------------------------------------------------
    st = swings or {}
    sh, sl = st.get("swing_high"), st.get("swing_low")
    lower_high = bool(sh and last.high < float(sh))
    higher_low = bool(sl and last.low > float(sl))
    add("structure", 1, "lower high formed" if bearish else "higher low formed",
        lower_high if bearish else higher_low)

    # Break of structure: the swing the rejection should invalidate has actually given way.
    closes = [c.close for c in recent]
    bos = False
    if bearish and sl:
        bos = any(c < float(sl) for c in closes)
    elif not bearish and sh:
        bos = any(c > float(sh) for c in closes)
    add("structure", 2, "broke the short-term swing low" if bearish else "broke the short-term swing high", bos)

    # Retest failure: after the break, price came back to the level and could not reclaim it.
    retest_failed = False
    if bos:
        lvl = float(sl) if bearish else float(sh)
        back = [c for c in recent if (c.high >= lvl if bearish else c.low <= lvl)]
        retest_failed = bool(back) and ((last.close < lvl) if bearish else (last.close > lvl))
    add("structure", 2, "retest of the broken level failed", retest_failed)

    # --- MOMENTUM (0-3) ------------------------------------------------------------------------
    # Fewest points on purpose: momentum indicators were the tier that failed out-of-sample here.
    closes_all = [c.close for c in candles]
    r_now = rsi(closes_all, 14)
    r_prev = rsi(closes_all[:-1], 14)
    m = macd(closes_all) or {}
    hist = m.get("hist")

    div = False
    if prior and r_now is not None:
        ref = max(c.high for c in prior) if bearish else min(c.low for c in prior)
        made_extreme = (last.high > ref) if bearish else (last.low < ref)
        r_at_ref = rsi(closes_all[: len(closes_all) - 1], 14)
        if made_extreme and r_at_ref is not None:
            div = (r_now < r_at_ref) if bearish else (r_now > r_at_ref)
    add("momentum", 1, "RSI divergence at the extreme", div)

    add("momentum", 1, "RSI back below 50" if bearish else "RSI back above 50",
        r_now is not None and ((r_now < 50) if bearish else (r_now > 50)))
    add("momentum", 1, "MACD on the rejection side",
        hist is not None and ((hist < 0) if bearish else (hist > 0)))

    # --- VOLUME (0-1) --------------------------------------------------------------------------
    # Capped at one point: this is TICK volume, so it measures activity, not selling pressure.
    base = [c.volume for c in candles[-21:-1] if c.volume]
    avg = sum(base) / len(base) if base else 0.0
    add("volume", 1, "rejection bar on above-average activity",
        bool(avg) and last.volume >= _VOL_MULT * avg)

    band, verdict = _band(score)
    _ = r_prev  # kept for readability of the RSI block; direction is carried by `div`
    return {
        "direction": direction,
        "score": score,
        "max": MAX_SCORE,
        "band": band,
        "verdict": verdict,
        "parts": parts,
        "groups": {
            g: {"got": sum(p["points"] for p in parts if p["group"] == g),
                "max": sum(p["max"] for p in parts if p["group"] == g)}
            for g in ("location", "rejection", "structure", "momentum", "volume")
        },
        "vwap": round(v_last, 6) if v_last is not None else None,
    }
