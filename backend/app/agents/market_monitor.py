"""Watchlist MONITOR - every watched pair's direction, momentum and zones on 1h / 4h / 1d at a glance.

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

from app.agents.chart_state import read_chart_state
from app.agents.order_blocks import find_order_blocks
from app.agents.orchestrator import _trend_from_indicators
from app.agents.technical import _deterministic_timeframe
from app.core.logging import get_logger
from app.data.ohlcv_cache import get_ohlcv_cached
from app.models.schemas import OHLCVSeries

log = get_logger("agents.market_monitor")

MONITOR_TFS = ("1h", "4h", "1d")
_FETCH_BARS = 500        # what the chart's order-block overlay reads - same zones as the chart
_ENGINE_BARS = 200       # what the pipeline feeds the engine - same indicator values as the engine
_MIN_BARS = 60
# A bar on the slow timeframes barely moves in a minute; refetching them that often only loads the
# shared MT5 connection. The 1h carries the live price, so it stays fresh.
_TTL = {"1h": 45.0, "4h": 120.0, "1d": 300.0}
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
    structure = {1.0: "up", -1.0: "down"}.get(ind.get("structure"), "range")
    choch = ind.get("choch") == 1.0
    return {
        "trend": _trend_from_indicators(ind, tfr.trend),
        "ema_stacked": stacked,
        "ema20": e20, "ema50": e50, "ema200": e200,
        "price_vs_ema200": (None if e200 is None else "above" if px > e200 else "below"),
        "adx": ind.get("adx"), "strength": strength, "adx_dir": adx_dir,
        "supertrend": ("up" if st_dir and st_dir > 0 else "down" if st_dir and st_dir < 0 else None),
        "rsi": ind.get("rsi14"), "rsi_prev": ind.get("rsi14_prev"), "rsi_zone": _rsi_zone(ind.get("rsi14")),
        "structure": structure,
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
        "zones": _zone_read(candles, candles[-1].close, atr_v),
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

    # Change of character, named by the side it broke - it only WARNS when it breaks against the bias.
    for tf in ("4h", "1h"):
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

    reads = [tfs.get(tf) for tf in MONITOR_TFS if tfs.get(tf)]
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
            series[tf] = get_ohlcv_cached(broker, symbol, tf, _FETCH_BARS, ttl=_TTL[tf])
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
    if px is not None and d1 and len(d1.candles) >= 2:
        # The last daily bar is normally today's, still forming. But the daily series is cached for
        # minutes while the 1h is fresher, so just after midnight it can still end at YESTERDAY - and
        # then [-2] is the day before and "today" would really be a two-day move. The 1h's last bar
        # dates the current session; when the daily lags it, yesterday's bar IS the prior close and
        # today's range comes from the 1h bars of the new day.
        today_bar = d1.candles[-1]
        prior_close = d1.candles[-2].close
        hi, lo = today_bar.high, today_bar.low
        if h1 and h1.candles and today_bar.ts.date() < h1.candles[-1].ts.date():
            prior_close = today_bar.close
            session = h1.candles[-1].ts.date()
            todays = [c for c in h1.candles if c.ts.date() == session]
            hi, lo = max(c.high for c in todays), min(c.low for c in todays)
        if prior_close:
            row["change_pct"] = round((px / prior_close - 1.0) * 100.0, 2)
        d_atr = (row["tfs"].get("1d") or {}).get("atr")
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
