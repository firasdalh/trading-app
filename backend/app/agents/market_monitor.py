"""Watchlist MONITOR - every watched pair's direction, momentum and zones on 15m / 1h / 4h / 1d at a glance.

The 15m is the ENTRY-timing column: it is shown and described, but the bias (aligned / dip / counter)
is still read from 1h / 4h / 1d only, so a 15-minute wiggle never relabels a pair's trend.

A manual trader scanning twenty charts asks the same four questions of each one: which way is it
going on the timeframes that matter, how hard, is momentum stretched, and is price sitting on a zone
someone defends? This answers them for the whole watchlist in one table, so the twenty charts become
one look.

DESIGN RULES
------------
1. The engine's own numbers. Direction is `orchestrator._trend_from_indicators` over
   `technical._deterministic_timeframe` - the exact read the laddered higher-timeframe filter uses - so
   an arrow here can never disagree with what the engine sees. Zones are `find_order_blocks` on the
   same 500 bars the chart's order-block overlay draws, so "inside 4H supply" matches the chart.
2. Read-out only. Nothing here gates, sizes, opens or vetoes a trade (same discipline as
   chart_state and order blocks). The notes describe what is on the chart; they never say buy/sell.
3. One broker call per pair per timeframe, cached. The MT5 terminal is a single connection shared
   with the scanner, the advisor and the Hybrid, so the slow timeframes are refreshed less often.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from app.agents.chart_state import read_chart_state
from app.agents.indicators import atr
from app.agents.order_blocks import find_order_blocks
from app.agents.orchestrator import _trend_from_indicators
from app.agents.technical import _deterministic_timeframe
from app.core.logging import get_logger
from app.data.ohlcv_cache import get_ohlcv_cached
from app.models.schemas import OHLCVSeries

log = get_logger("agents.market_monitor")

MONITOR_TFS = ("15m", "1h", "4h", "1d")
BIAS_TFS = ("1h", "4h", "1d")        # the timeframes the bias label is read from
_FETCH_BARS = 500        # what the chart's order-block overlay reads - same zones as the chart
# The 15m volume baseline compares a bar with the SAME 15-minute slot in ~20 earlier sessions, which
# needs ~20 days of 15m bars. Zones still use only the last _FETCH_BARS (what the chart draws).
_FETCH_BARS_TF = {"15m": 2000}
_ENGINE_BARS = 200       # what the pipeline feeds the engine - same indicator values as the engine
_MIN_BARS = 60
# A bar on the slow timeframes barely moves in a minute; refetching them that often only loads the
# shared MT5 connection. The 1h carries the live price, so it stays fresh.
_TTL = {"15m": 30.0, "1h": 45.0, "4h": 120.0, "1d": 300.0}
# Price this close to a zone edge (in ATRs of that timeframe) counts as "at" the zone.
NEAR_ZONE_ATR = 0.5
RSI_HIGH, RSI_LOW = 70.0, 30.0
ADX_STRONG, ADX_WEAK = 25.0, 20.0


def _rsi_zone(r: float | None) -> str | None:
    if r is None:
        return None
    if r >= RSI_HIGH:
        return "overbought"
    if r <= RSI_LOW:
        return "oversold"
    return "bullish" if r >= 50 else "bearish"


def _strength(adx_v: float | None, adx_prev: float | None) -> tuple[str | None, str | None]:
    """(strong|moderate|weak, rising|falling|flat) - the level alone can't tell a trend starting from
    one running out of steam, so the slope travels with it."""
    if adx_v is None:
        return None, None
    level = "strong" if adx_v >= ADX_STRONG else "weak" if adx_v < ADX_WEAK else "moderate"
    if adx_prev is None:
        return level, None
    d = adx_v - adx_prev
    return level, "rising" if d > 0.3 else "falling" if d < -0.3 else "flat"


def _zone_read(candles, px: float, atr_v: float | None) -> dict:
    """Is price inside a live demand/supply zone, and how far are the nearest ones either side?"""
    blocks = find_order_blocks(candles)          # the same (max 8, live only) set the chart draws
    out: dict = {"inside": None, "demand": None, "supply": None}

    def box(b) -> dict:
        d = {"top": round(b.top, 8), "bottom": round(b.bottom, 8), "strength": b.strength,
             "mitigated": b.mitigated, "dist_atr": None}
        if atr_v:
            edge = b.top if b.kind == "bullish" else b.bottom
            d["dist_atr"] = round(abs(px - edge) / atr_v, 2) if not b.contains(px) else 0.0
        return d

    inside = [b for b in blocks if b.contains(px)]
    # Every KIND price is inside, not just the strongest block's: when a demand and a supply block
    # overlap price, reporting only the stronger one hid the other completely (it is neither "below"
    # nor "above"), and an opposing zone is the one a trader most needs to see.
    out["inside_kinds"] = sorted({"demand" if b.kind == "bullish" else "supply" for b in inside})
    if inside:
        b = max(inside, key=lambda x: x.strength)
        out["inside"] = {"kind": "demand" if b.kind == "bullish" else "supply", **box(b)}
    below = [b for b in blocks if b.kind == "bullish" and b.top < px]
    above = [b for b in blocks if b.kind == "bearish" and b.bottom > px]
    if below:
        out["demand"] = box(min(below, key=lambda b: px - b.top))
    if above:
        out["supply"] = box(min(above, key=lambda b: b.bottom - px))
    return out


VOL_HIGH, VOL_LOW = 1.5, 0.7          # last completed bar vs its 20-bar average
VOL_SPIKE = 2.0
VOL_SIDE_LO, VOL_SIDE_HI = 0.40, 0.60  # share of recent volume on up-close bars


_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 4 * 3600, "1d": 86400}
_SAME_HOUR_MIN = 8          # same-hour samples needed before an intraday ratio means anything


def completed(candles: list, timeframe: str, now: datetime | None = None) -> list:
    """The bars whose window has CLOSED.

    MT5 hands back the still-forming bar last, and its volume is partial: ten minutes into an hour it
    always looks "low". But the last bar is not always forming - on a closed market (weekends, HK50 and
    IN50 every night) it is complete, and dropping it unconditionally made "the last completed bar"
    silently mean the one before. So it is dropped only while its window is still open. Exness server
    time is UTC (daily bars stamp 00:00 UTC), which is what makes this comparison valid.
    """
    if not candles:
        return candles
    span = _TF_SECONDS.get(timeframe)
    if span is None:
        return candles[:-1]
    now = now or datetime.now(timezone.utc)
    last = candles[-1].ts if candles[-1].ts.tzinfo else candles[-1].ts.replace(tzinfo=timezone.utc)
    return candles[:-1] if last + timedelta(seconds=span) > now else candles


def _volume_read(candles, timeframe: str = "1h", now: datetime | None = None) -> dict | None:
    """Volume status from COMPLETED bars (see `completed`). MT5 TICK volume (price changes per bar),
    a proxy for traded volume that moves with it most of the time - not exchange volume.

    Intraday bars are compared with the SAME UTC HOUR in earlier sessions, not with the bars just
    before them. Against a trailing window the reading mostly reported the clock: replayed over 14
    months of Exness 1H bars, the 14:00 UTC bar (US cash open) read as a 2x "spike" on 96% of days for
    US500/USTEC and the 21-23 UTC bars read "low" on 89-100% of days; per-hour baselines bring the spike
    rate down to 1-15% in every hour. Daily bars use the previous 20 sessions.

      ratio      the last completed bar's volume / its baseline
      level      high (>= 1.5x) | normal | low (<= 0.7x)
      trend      expanding | fading | flat - the last 3 completed bars vs the 5 before, each first
                 divided by its own baseline so the session clock cancels out
      up_share   share of the last 20 completed bars' volume that traded on UP-close bars
    """
    done = completed(candles, timeframe, now)
    if len(done) < 25:
        return None
    vols = [float(c.volume or 0.0) for c in done]
    intraday = timeframe in ("15m", "1h", "4h")

    def baseline(i: int) -> float | None:
        if intraday:
            # Same clock SLOT (hour and minute): on 1h/4h the minute is always :00, so this is the
            # same-hour baseline; on 15m it keeps 14:30 from being judged against 14:00 (the US open).
            slot = (done[i].ts.hour, done[i].ts.minute)
            same = [vols[j] for j in range(i - 1, -1, -1)
                    if (done[j].ts.hour, done[j].ts.minute) == slot][:20]
            if len(same) < _SAME_HOUR_MIN:
                return None
        else:
            same = vols[max(0, i - 20):i]
            if len(same) < 10:
                return None
        avg = sum(same) / len(same)
        return avg if avg > 0 else None

    ratios: list[float | None] = []
    for i in range(len(done) - 8, len(done)):
        b = baseline(i)
        ratios.append(vols[i] / b if b else None)
    ratio = ratios[-1]
    if ratio is None:
        return None
    trend = None
    rec, pri = [r for r in ratios[-3:] if r is not None], [r for r in ratios[:-3] if r is not None]
    if len(rec) == 3 and len(pri) >= 3:
        rm, pm = sum(rec) / len(rec), sum(pri) / len(pri)
        if pm > 0:
            r = rm / pm
            trend = "expanding" if r >= 1.15 else "fading" if r <= 0.85 else "flat"
    win = done[-20:]
    upv = sum(float(c.volume or 0.0) for c in win if c.close > c.open)
    dnv = sum(float(c.volume or 0.0) for c in win if c.close < c.open)
    return {
        "ratio": round(ratio, 2),
        "level": "high" if ratio >= VOL_HIGH else "low" if ratio <= VOL_LOW else "normal",
        "trend": trend,
        "up_share": round(upv / (upv + dnv), 2) if (upv + dnv) > 0 else None,
        "baseline": ("same 15-min slot" if timeframe == "15m" else "same hour") if intraday else "20 sessions",
    }


_HOLIDAY_STUB = 0.15        # a weekday trading under 15% of a normal session is a holiday stub


def _fold(into, stub):
    return into.model_copy(update={"open": stub.open, "high": max(stub.high, into.high),
                                   "low": min(stub.low, into.low),
                                   "volume": (stub.volume or 0) + (into.volume or 0)})


def session_days(daily: list) -> list:
    """Daily bars with weekend and holiday STUBS folded into the next trading day.

    Exness prints a small Sunday bar for FX, indices and metals - the hour or two the week is open on
    Sunday evening (measured: DE30 2,680 ticks vs ~110,000 on a weekday). Read as "a day" it made the
    last completed day look 50x quieter than normal, dragged the daily ATR down (so "% of the day used"
    read high) and moved Monday's "change today" off Friday's close. Holiday half-sessions (Good Friday,
    Memorial Day, Juneteenth... measured under 25% of a normal day) do the same on weekdays. Folding a
    stub into the next session is how the FX week is conventionally drawn. Instruments that really
    trade weekends (crypto: weekend bars are ~2/7 of all bars) keep their weekend days.
    """
    if not daily:
        return daily
    weekend = sum(1 for c in daily if c.ts.weekday() >= 5)
    fold_weekends = weekend <= 0.2 * len(daily)
    out: list = []
    pending = None
    last = len(daily) - 1
    for i, c in enumerate(daily):
        is_stub = fold_weekends and c.ts.weekday() >= 5
        if not is_stub and i < last and len(out) >= 10:
            # A weekday holiday stub. Never the last bar: today's forming bar is small early in the day.
            vols = sorted(float(x.volume or 0) for x in out[-20:])
            med = vols[len(vols) // 2]
            is_stub = med > 0 and float(c.volume or 0) < _HOLIDAY_STUB * med
        if is_stub:
            pending = c if pending is None else pending.model_copy(update={
                "high": max(pending.high, c.high), "low": min(pending.low, c.low),
                "close": c.close, "volume": (pending.volume or 0) + (c.volume or 0)})
            continue
        if pending is not None:
            c = _fold(c, pending)
            pending = None
        out.append(c)
    if pending is not None:          # Sunday evening, Monday's bar not printed yet: the stub IS the
        out.append(pending)          # forming session
    return out if (fold_weekends or len(out) != len(daily)) else daily


def read_timeframe(series: OHLCVSeries) -> dict | None:
    """One timeframe's read-out, or None when there isn't enough history to say anything honest."""
    candles = list(series.candles)
    if len(candles) < _MIN_BARS:
        return None
    eng = candles[-_ENGINE_BARS:]
    tfr = _deterministic_timeframe(OHLCVSeries(symbol=series.symbol, timeframe=series.timeframe,
                                               candles=eng))
    ind = tfr.indicators
    px = eng[-1].close
    atr_v = ind.get("atr14")
    e20, e50, e200 = ind.get("ema20"), ind.get("ema50"), ind.get("ema200")
    stacked = (e20 is not None and e50 is not None and e200 is not None
               and (e20 > e50 > e200 or e20 < e50 < e200))
    strength, adx_dir = _strength(ind.get("adx"), ind.get("adx_prev"))
    st_dir = ind.get("supertrend_dir")
    cs = read_chart_state(eng)
    div = ("bear" if ind.get("div_bear") else "bull" if ind.get("div_bull")
           else "bear_hidden" if ind.get("div_bear_hidden") else "bull_hidden" if ind.get("div_bull_hidden")
           else None)
    daily = series.timeframe == "1d"
    structure = {1.0: "up", -1.0: "down"}.get(ind.get("structure"), "range")
    choch = ind.get("choch") == 1.0
    return {
        "trend": _trend_from_indicators(ind, tfr.trend),
        "ema_stacked": stacked,
        "ema20": e20, "ema50": e50, "ema200": e200,
        "price_vs_ema200": (None if e200 is None else "above" if px > e200 else "below"),
        "adx": ind.get("adx"), "strength": strength, "adx_dir": adx_dir,
        # ADX measures strength only; +DI vs -DI says which side that strength belongs to.
        "plus_di": ind.get("plus_di"), "minus_di": ind.get("minus_di"),
        "supertrend": ("up" if st_dir and st_dir > 0 else "down" if st_dir and st_dir < 0 else None),
        "rsi": ind.get("rsi14"), "rsi_prev": ind.get("rsi14_prev"), "rsi_zone": _rsi_zone(ind.get("rsi14")),
        "structure": structure,
        # The two swings that define the structure: a close beyond the opposite one breaks it.
        "swing_high": ind.get("swing_high"), "swing_low": ind.get("swing_low"),
        "choch": choch,
        # Which way the change of character broke. It is measured against the SWING structure, which
        # can disagree with the EMA arrow shown beside it: swing structure "up" plus a close under the
        # last swing low is a BEARISH break - it confirms a down arrow rather than warning against it.
        "choch_side": ("down" if structure == "up" else "up") if choch else None,
        "divergence": div,
        "state": cs.state, "state_headline": cs.headline,
        # Which way the latest bar's pattern leans (chart_state's own read), so "pullback" is never
        # shown without saying whether it is resuming the move or fading it.
        "pattern_side": cs.pattern_side,
        "atr": atr_v,
        # Daily volume is read on SESSION days (stubs folded in); intraday on all fetched bars, so each
        # hour has ~20 earlier sessions of the same hour to be compared with.
        "volume": _volume_read(session_days(candles) if daily else candles, series.timeframe),
        # Zone distances are display-only, so the daily ones use the session-day ATR - the same unit
        # as "% of day used" on the same row (the raw daily ATR is 14-23% smaller, deflated by stubs).
        "zones": _zone_read(candles[-_FETCH_BARS:], candles[-1].close,
                            (atr(session_days(eng)) or atr_v) if daily else atr_v),
    }


def bias_of(trends: dict[str, str | None]) -> str:
    """How the three directions line up. Not a signal - a sorting key and a label.

    aligned_*  all three agree
    pullback_* the 4h and 1d agree and the 1h points the OTHER way (the classic dip/bounce)
    counter_*  the 1h and 4h agree and the 1d points the other way
    mixed      anything else - INCLUDING a timeframe that could not be read. A missing read is not
               "against": labelling it so printed "Up vs the 1D" for a daily nobody had measured.
    """
    h1, h4, d1 = trends.get("1h"), trends.get("4h"), trends.get("1d")
    for side in ("up", "down"):
        if h1 == h4 == d1 == side:
            return f"aligned_{side}"
    for side, opp in (("up", "down"), ("down", "up")):
        if h4 == d1 == side and h1 == opp:
            return f"pullback_{side}"
        if h1 == h4 == side and d1 == opp:
            return f"counter_{side}"
    return "mixed"


def _fmt_dist(d: float | None) -> str:
    return "" if d is None else f" ({d:.1f} ATR away)"


def notes_for(row: dict) -> list[dict]:
    """Plain-language 'what to watch' for one pair, most important first. Each note is
    {tone: info|warn|caution, text}. Descriptive only - never 'buy' or 'sell'."""
    tfs = row["tfs"]
    bias = row["bias"]
    up = bias.endswith("_up")
    down = bias.endswith("_down")
    side = "up" if up else "down" if down else None
    opp = "down" if up else "up" if down else None
    out: list[dict] = []

    if row.get("market_open") is False:
        # Crypto trades around the clock: no ticks there means a quiet feed, not a closed session.
        out.append({"tone": "info", "text": (
            "No fresh prices for 10+ minutes - the readings may be stale"
            if row.get("asset_class") == "crypto"
            else "Market closed - these readings are from the last session")})
    if row.get("events_soon"):
        out.append({"tone": "warn", "text": f"News: {row['events_soon']}"})

    if bias.startswith("pullback_"):
        h1 = tfs.get("1h") or {}
        word = "dipping" if up else "bouncing"
        text = f"1H is {word} against a 4H + 1D {side}trend"
        # chart_state's pattern side says which way the latest 1H bar leans. Without it a bare
        # "(pullback)" read like a confirmation even when the 1H was still pushing against the bias.
        ps, state = h1.get("pattern_side"), h1.get("state")
        if ps == side:
            text += f"; the latest 1H bar leans back {side} ({state})"
        elif ps == opp:
            text += f"; the latest 1H bar still leans {opp} ({state})"
        out.append({"tone": "info", "text": text})
    elif bias.startswith("counter_"):
        out.append({"tone": "caution", "text": f"1H + 4H point {side} but the 1D points the other way"})

    # The 15m, for timing only: is the short-term move with the higher timeframes right now?
    m15 = (tfs.get("15m") or {}).get("trend")
    if side and m15 in ("up", "down"):
        if bias.startswith("aligned_") and m15 == opp:
            out.append({"tone": "info", "text": f"15M is moving {opp} against the aligned {side} trend - "
                        "a short-term pullback for now"})
        elif bias.startswith("pullback_") and m15 == side:
            out.append({"tone": "info", "text": f"15M has turned back {side}, with the 4H + 1D"})

    # Zones. An OPPOSING zone (price inside one, or one just ahead) is the warning that matters, so it
    # is searched on every timeframe before an in-line zone is allowed to be the only zone note.
    if side:
        against_kind, with_kind = ("supply", "demand") if up else ("demand", "supply")
        caution = info = None
        for tf in ("1d", "4h", "1h"):
            z = ((tfs.get(tf) or {}).get("zones") or {})
            kinds = z.get("inside_kinds") or ([z["inside"]["kind"]] if z.get("inside") else [])
            ahead = z.get(against_kind)
            if caution is None and against_kind in kinds:
                caution = f"Price is inside a {tf.upper()} {against_kind} zone, against the {side} bias"
            elif (caution is None and ahead and ahead.get("dist_atr") is not None
                  and ahead["dist_atr"] <= NEAR_ZONE_ATR):
                caution = f"A {tf.upper()} {against_kind} zone is just ahead{_fmt_dist(ahead['dist_atr'])}"
            if info is None and with_kind in kinds and against_kind not in kinds:
                info = f"Price is inside a {tf.upper()} {with_kind} zone, in line with the {side} bias"
        if caution:
            out.append({"tone": "caution", "text": caution})
        if info:
            out.append({"tone": "info", "text": info})

    # Momentum stretched in the direction of travel.
    for tf in ("4h", "1h"):
        t = tfs.get(tf) or {}
        r = t.get("rsi")
        if r is None:
            continue
        if up and r >= RSI_HIGH:
            out.append({"tone": "caution", "text": f"{tf.upper()} RSI {r:.0f} - overbought; a new long here "
                        "would be buying after the move"})
            break
        if down and r <= RSI_LOW:
            out.append({"tone": "caution", "text": f"{tf.upper()} RSI {r:.0f} - oversold; a new short here "
                        "would be selling after the move"})
            break

    # Swing structure against the bias on a higher timeframe. The arrows are EMA-based and lag; the
    # swings are what price actually printed. A 4H/1D still making lower highs and lower lows under an
    # "up" bias means the EMAs have turned before the structure has - a trend in transition.
    # Only where that timeframe's OWN arrow is with the bias (under a counter_* bias the 1D arrow already
    # disagrees, and this would restate the counter note as a second caution), and never once price has
    # broken that structure in the bias direction: market_structure keeps the old label after a CHoCH,
    # so "still making lower lows" was printed for USOIL 1D with price already above the last lower high.
    if side:
        for tf in ("1d", "4h"):
            t = tfs.get(tf) or {}
            st = t.get("structure")
            if st == opp and t.get("trend") == side and t.get("choch_side") != side:
                swings = "lower highs and lower lows" if st == "down" else "higher highs and higher lows"
                out.append({"tone": "caution", "text": f"{tf.upper()} swing structure is still making {swings}, "
                            f"against the {side} bias"})
                break

    # Volume. Where the volume traded says more than how much: most of the 4H's recent volume on bars
    # AGAINST the bias is pressure the arrows do not show yet. A spike on the last completed 1H bar is
    # reported as a fact, not read as a direction - spikes mark breakouts and climaxes alike.
    if side:
        v4 = ((tfs.get("4h") or {}).get("volume") or {})
        share = v4.get("up_share")
        if share is not None and ((up and share <= VOL_SIDE_LO) or (down and share >= VOL_SIDE_HI)):
            against = 1 - share if up else share
            word = "down" if up else "up"
            pressure = "selling" if up else "buying"
            out.append({"tone": "caution", "text": f"4H: {against * 100:.0f}% of recent volume traded on {word} bars - "
                        f"{pressure} pressure under the {side} bias"})
    v1 = ((tfs.get("1h") or {}).get("volume") or {})
    if (v1.get("ratio") is not None and v1["ratio"] >= VOL_SPIKE and row.get("market_open") is not False):
        out.append({"tone": "info", "text": f"1H volume spike: the last completed bar traded {v1['ratio']:.1f}x "
                    "the usual volume for that hour"})

    # Change of character, named by the side it broke - it only WARNS when it breaks against the bias.
    # The 1D is included: a daily break is the most important one, and it was never reported.
    for tf in ("1d", "4h", "1h"):
        t = tfs.get(tf) or {}
        ch = t.get("choch_side")
        if not ch:
            continue
        what = ("bullish change of character (closed above the last swing high)" if ch == "up"
                else "bearish change of character (closed below the last swing low)")
        if side and ch != side:
            out.append({"tone": "caution", "text": f"{tf.upper()} {what} - against the {side} bias, "
                        "an early sign of a turn"})
        else:
            out.append({"tone": "info", "text": f"{tf.upper()} {what}"
                        + (f" - in line with the {side} bias" if side else "")})
        break
    for tf in ("4h", "1d"):
        t = tfs.get(tf) or {}
        dv = t.get("divergence")
        if (dv == "bear" and up) or (dv == "bull" and down):
            kind = "Bearish" if dv == "bear" else "Bullish"
            out.append({"tone": "caution", "text": f"{kind} RSI divergence on {tf.upper()} - momentum is not "
                        "confirming the latest swing"})
            break

    ru = row.get("range_used")
    if ru is not None and ru >= 1.0 and row.get("market_open") is not False:
        out.append({"tone": "info", "text": f"Today has already covered {ru * 100:.0f}% of a normal day's range"})

    reads = [tfs.get(tf) for tf in BIAS_TFS if tfs.get(tf)]
    if reads and all(t.get("strength") == "weak" for t in reads):
        out.append({"tone": "info", "text": "No trend strength on any timeframe (ADX under 20) - choppy"})
    return out


# The 4h / 1d candles are the SAME cached objects for 2-5 minutes while the table is polled every
# minute, so their reads are reused until the series object changes (the full 60-read rebuild is
# seconds of pure-Python CPU in a process the scanner and the stop monitor share).
_memo_lock = threading.Lock()
_memo: dict[tuple[str, str], tuple[OHLCVSeries, dict | None]] = {}


def _read_cached(symbol: str, tf: str, series: OHLCVSeries) -> dict | None:
    key = (symbol.upper(), tf)
    with _memo_lock:
        hit = _memo.get(key)
    if hit is not None and hit[0] is series:
        return hit[1]
    result = read_timeframe(series)
    with _memo_lock:
        _memo[key] = (series, result)
    return result


def read_pair(broker, symbol: str, asset_class: str) -> dict:
    """The full monitor row for one pair. Never raises: a timeframe that fails reads as missing."""
    row: dict = {"symbol": symbol, "asset_class": asset_class, "tfs": {}, "errors": []}
    series: dict[str, OHLCVSeries] = {}
    for tf in MONITOR_TFS:
        try:
            series[tf] = get_ohlcv_cached(broker, symbol, tf, _FETCH_BARS_TF.get(tf, _FETCH_BARS), ttl=_TTL[tf])
            row["tfs"][tf] = _read_cached(symbol, tf, series[tf])
            if row["tfs"][tf] is None:
                row["errors"].append(f"{tf}: not enough history ({len(series[tf].candles)} bars)")
        except Exception as exc:  # noqa: BLE001 - one bad timeframe must not blank the whole row
            log.warning("monitor read failed", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
            row["tfs"][tf] = None
            row["errors"].append(f"{tf}: {exc}")

    h1 = series.get("1h")
    d1 = series.get("1d")
    px = h1.candles[-1].close if h1 and h1.candles else (d1.candles[-1].close if d1 and d1.candles else None)
    row["price"] = px
    row["change_pct"] = row["range_used"] = None
    raw_days = list(d1.candles) if d1 else []
    days = session_days(raw_days)               # weekend stubs folded into Monday (see session_days)
    folded = days is not raw_days
    if px is not None and len(days) >= 2:
        # The last daily bar is normally today's, still forming. But the daily series is cached for
        # minutes while the 1h is fresher, so just after midnight it can still end at YESTERDAY - and
        # then [-2] is the day before and "today" would really be a two-day move. The 1h's last bar
        # dates the current session; when the daily lags it, yesterday's bar IS the prior close and
        # today's range comes from the 1h bars of the new day - unless the lagging bar is a Sunday
        # stub, which is the START of today's (Monday's) session rather than a day of its own.
        today_bar = days[-1]
        prior_close = days[-2].close
        hi, lo = today_bar.high, today_bar.low
        if h1 and h1.candles and today_bar.ts.date() < h1.candles[-1].ts.date():
            session = h1.candles[-1].ts.date()
            todays = [c for c in h1.candles if c.ts.date() == session]
            if folded and today_bar.ts.weekday() >= 5:
                hi = max([today_bar.high, *(c.high for c in todays)])
                lo = min([today_bar.low, *(c.low for c in todays)])
            else:
                prior_close = today_bar.close
                hi, lo = max(c.high for c in todays), min(c.low for c in todays)
        if prior_close:
            row["change_pct"] = round((px / prior_close - 1.0) * 100.0, 2)
        # A normal day's range from SESSION days: the engine's daily ATR counts each Sunday stub as a
        # day, which pulls it down and made "% of the day used" read high.
        d_atr = atr(days[-_ENGINE_BARS:])
        if d_atr:
            row["range_used"] = round((max(hi, px) - min(lo, px)) / d_atr, 2)
    try:
        row["market_open"] = bool(broker.market_open(symbol))
    except Exception:  # noqa: BLE001 - advisory; unknown reads as open (the chart badge does the same)
        row["market_open"] = True
    row["bias"] = bias_of({tf: (t or {}).get("trend") for tf, t in row["tfs"].items()})
    return row


# Whole-table cache: several open windows (or a fast double-click on Refresh) must not each re-read
# twenty pairs. The lock makes a second caller WAIT for the build in flight instead of starting its own.
_TABLE_TTL = 20.0
_lock = threading.Lock()
_table: dict = {"key": None, "at": 0.0, "rows": None}


def cached_table(key: tuple, build, fresh_after: float | None = None) -> tuple[list[dict], float]:
    """(rows, built_at_epoch) for this watchlist ``key``, rebuilt at most every _TABLE_TTL seconds.

    ``fresh_after`` (Refresh): serve a table only if it was finished after that moment. A Refresh
    that arrives while a build is in flight therefore WAITS for that build and gets it, instead of
    throwing it away and rebuilding - two windows refreshing at once cost one build, not two.
    """
    with _lock:
        now = time.time()
        hit = _table["key"] == key and _table["rows"] is not None
        if hit and (_table["at"] >= fresh_after if fresh_after is not None
                    else now - _table["at"] < _TABLE_TTL):
            return _table["rows"], _table["at"]
        rows = build()
        _table.update(key=key, at=time.time(), rows=rows)
        return rows, _table["at"]


def clear_cache() -> None:
    with _lock:
        _table.update(key=None, at=0.0, rows=None)
    with _memo_lock:
        _memo.clear()
