"""Market-CONTEXT read (Task: 'analyse like a chart reader').

For a symbol, produce the plain-language "where is price on the map, and do a few indicators confirm?"
analysis a discretionary trader writes — WITHOUT changing the engine's validated decision. It's an
INFORMATION layer for the user's Mode-A approve/reject call: it reads price vs multi-timeframe
support/resistance, the regression channel, and market structure (HH/HL/LH/LL), then confirms with a
short set of indicators (RSI, volume, ATR) and gives a short-term + medium-term lean and a key level
to watch.

Deterministic + read-only. Does NOT gate trades (we backtested level-based gating — it hurt the trend
edge; see analysis/channel_test.md). This just tells the user what the chart is saying.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.indicators import pivot_levels
from app.core.logging import get_logger
from app.models.enums import AssetClass

log = get_logger("agents.context")

_NEAR_ATR = 0.6   # within this many ATR of a level = "at" it

# Timeframe ordering, so the read can tell "higher than the chart" from "lower".
_TF_RANK = {"1m": 1, "5m": 2, "15m": 3, "30m": 4, "1h": 5, "2h": 6, "4h": 7, "6h": 8, "12h": 9,
            "1d": 10, "1w": 11}


def _level_timeframes(entry_tf: str) -> list[str]:
    """Timeframes to harvest support/resistance from: the chart's own, plus the higher context ones.

    Never lower. A 5m chart should still respect the 4h wall above it — that's what actually stops
    price — but a 4h chart has no business reacting to 5m noise, and mixing those in would bury the
    levels that matter under a dozen that don't.
    """
    rank = _TF_RANK.get(entry_tf)
    if rank is None:                 # unknown label — read it as 1h rather than invent a ladder
        entry_tf, rank = "1h", _TF_RANK["1h"]
    return list(dict.fromkeys([entry_tf, *[t for t in ("1h", "4h", "1d") if _TF_RANK[t] > rank]]))


# The timeframes the app actually charts, in order — used to find "the two above this one".
_COMPARE_LADDER = ("5m", "15m", "1h", "4h", "1d")


def _higher_timeframes(entry_tf: str, n: int = 2) -> list[str]:
    """The next ``n`` charted timeframes above ``entry_tf`` (fewer near the top of the ladder)."""
    if entry_tf not in _COMPARE_LADDER:
        entry_tf = "1h"
    i = _COMPARE_LADDER.index(entry_tf)
    return list(_COMPARE_LADDER[i + 1: i + 1 + n])


def _fmt(p: float | None) -> str:
    if p is None:
        return "?"
    a = abs(p)
    if a >= 1000:
        return f"{p:.2f}"
    if a >= 100:
        return f"{p:.3f}"
    if a >= 1:
        return f"{p:.5f}"
    return f"{p:.6f}"


def build_context(session: Session, symbol: str, asset_class: AssetClass,
                  timeframe: str = "1h") -> dict | None:
    """Return the structured + plain-language context read, or None if data is unavailable.

    ``timeframe`` is the chart you're reading. EVERYTHING derived from candles — indicators, market
    structure, price action, level tests — is computed on it, so switching the chart to 5m gives a
    5m read rather than a 1h read wearing a 5m label. Support/resistance additionally pulls in the
    higher timeframes (see ``_level_timeframes``), because those walls hold regardless of the chart
    you happen to be looking at.
    """
    from app.agents.pipeline import _timeframes_for
    from app.agents.technical import run_technical
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.data.ohlcv_cache import get_ohlcv_cached

    settings = get_or_create_settings(session)
    broker = get_broker_for(asset_class, settings.broker_map)
    entry_tf = timeframe if timeframe in _TF_RANK else "1h"
    higher_tfs = _higher_timeframes(entry_tf)   # the two charts above this one, for the comparison
    # run_technical needs the entry TF + the HTF context; the comparison adds any rung in between
    # (a 5m chart compares against 15m, which the standard context ladder skips).
    wanted = list(dict.fromkeys([*_timeframes_for(entry_tf), *higher_tfs]))
    level_tfs = _level_timeframes(entry_tf)     # where support/resistance may come from
    series, candle_by_tf = [], {}
    for tf in dict.fromkeys([*wanted, *level_tfs]):
        try:
            s = get_ohlcv_cached(broker, symbol, tf, 200)
        except Exception as exc:  # noqa: BLE001
            log.warning("context ohlcv failed", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
            continue
        if s and s.candles:
            candle_by_tf[tf] = list(s.candles)
            if tf in wanted:
                series.append(s)
    if entry_tf not in candle_by_tf or not series:
        return None
    tech = run_technical(symbol, series, use_llm=False)
    prim = next((x for x in tech.timeframes if x.timeframe == entry_tf), None) if tech.timeframes else None
    if prim is None:
        return None
    ind = prim.indicators
    price = ind.get("last_close") or candle_by_tf[entry_tf][-1].close
    atr = ind.get("atr14") or 0.0

    # --- nearest multi-TF support/resistance ---
    all_levels: dict[str, list[dict]] = {}
    for tf in level_tfs:
        cs = candle_by_tf.get(tf)
        if cs:
            all_levels[tf] = [{**lv, "tf": tf} for lv in pivot_levels(cs, price)]
    res = [lv for lvs in all_levels.values() for lv in lvs if lv["kind"] == "resistance"]
    sup = [lv for lvs in all_levels.values() for lv in lvs if lv["kind"] == "support"]
    near_res = min(res, key=lambda x: x["price"], default=None)         # nearest above
    near_sup = max(sup, key=lambda x: x["price"], default=None)         # nearest below
    res_d = abs(near_res["price"] - price) / atr if (near_res and atr) else None
    sup_d = abs(price - near_sup["price"]) / atr if (near_sup and atr) else None

    # --- level STRENGTH: how many recent bars TESTED the nearest level (came within 0.25 ATR of it).
    # A level tested many times is a real wall (favours a reject/fade); a fresh level is weak (breaks
    # more easily). This is the single biggest tell for breakout-vs-rejection, so we count and attach it.
    def _tests(level: float | None) -> int:
        if level is None or not atr:
            return 0
        tol = 0.25 * atr
        bars = candle_by_tf[entry_tf][-60:]
        return sum(1 for c in bars if (c.low - tol) <= level <= (c.high + tol))

    res_tests = _tests(near_res["price"] if near_res else None)
    sup_tests = _tests(near_sup["price"] if near_sup else None)
    if near_res:
        near_res = {**near_res, "tests": res_tests}
    if near_sup:
        near_sup = {**near_sup, "tests": sup_tests}

    # --- level LADDER: the next 2-3 levels each way (deduped ~0.3 ATR apart), each with a test count,
    # so the AI has realistic TARGETS to build scenario paths ("break X -> run to Y"), not just the wall.
    def _ladder(levels: list[dict], reverse: bool) -> list[dict]:
        out: list[dict] = []
        for lv in sorted(levels, key=lambda x: x["price"], reverse=reverse):
            if atr and out and abs(lv["price"] - out[-1]["price"]) < 0.3 * atr:
                continue  # too close to one we already kept — same wall
            out.append({"price": lv["price"], "tf": lv["tf"], "tests": _tests(lv["price"])})
            if len(out) >= 3:
                break
        return out

    res_ladder = _ladder(res, reverse=False)   # ascending: nearest resistance first
    sup_ladder = _ladder(sup, reverse=True)     # descending: nearest support first

    def _strength(n: int) -> str:
        return "strong" if n >= 4 else "moderate" if n >= 2 else "fresh/weak"

    # --- ① BREAKOUT CANDIDATES: the strongest nearby level to break + the next level as target +
    # a projected reward:risk, so the AI arms at a REAL level with real numbers instead of guessing.
    # Stop sits just the other side of the broken level (it flips to support/resistance): ~0.6 ATR.
    def _candidate(ladder: list[dict], up: bool) -> dict | None:
        if len(ladder) < 2 or not atr or atr <= 0:
            return None
        trig, tgt = ladder[0]["price"], ladder[1]["price"]
        stop = trig - 0.6 * atr if up else trig + 0.6 * atr
        risk, reward = abs(trig - stop), abs(tgt - trig)
        if risk <= 0 or reward <= 0:
            return None
        return {"trigger": round(trig, 6), "tests": ladder[0]["tests"], "target": round(tgt, 6),
                "stop": round(stop, 6), "rr": round(reward / risk, 2), "strength": _strength(ladder[0]["tests"])}

    breakout_up = _candidate(res_ladder, up=True)     # break UP through nearest resistance
    breakdown = _candidate(sup_ladder, up=False)      # break DOWN through nearest support

    # --- ② BREAKOUT READINESS: is the range coiled (a clean break tends to run) or loose/expanded
    # (higher fakeout & whipsaw risk)? vol_atr_ratio = recent ATR / its 50-bar baseline. ---
    var = ind.get("vol_atr_ratio")
    bbw = ind.get("bb_width")
    if var is None:
        readiness_state = "unknown"
    elif var <= 0.85:
        readiness_state = "coiled — low volatility / compression (a clean break tends to follow through)"
    elif var >= 1.4:
        readiness_state = "loose/expanded — volatility already high (higher fakeout & whipsaw risk)"
    else:
        readiness_state = "normal volatility"
    compression = {"vol_atr_ratio": round(var, 2) if var is not None else None,
                   "bb_width": round(bbw, 6) if bbw is not None else None, "state": readiness_state}

    # --- structure / channel / confirmation ---
    struct = ind.get("structure")
    struct_label = ("bullish — higher highs & higher lows" if struct and struct > 0.5 else
                    "bearish — lower highs & lower lows" if struct and struct < -0.5 else
                    "ranging — no clear swing trend")
    choch = bool(ind.get("choch"))
    chan_pos, chan_r2 = ind.get("chan_pos"), ind.get("chan_r2") or 0.0
    chan_slope = ind.get("chan_slope") or 0.0
    chan_note = None
    if chan_pos is not None and chan_r2 >= 0.30:
        where = ("top (near resistance)" if chan_pos >= 0.80 else
                 "bottom (near support)" if chan_pos <= 0.20 else "middle")
        direction = "rising" if chan_slope > 0 else "falling" if chan_slope < 0 else "flat"
        broke = " — price has broken OUT of it" if (chan_pos > 1.05 or chan_pos < -0.05) else ""
        chan_note = f"in the {where} of a {direction} channel{broke}"

    rsi, rsi_prev = ind.get("rsi14"), ind.get("rsi14_prev")
    rsi_dir = ("rising" if (rsi is not None and rsi_prev is not None and rsi > rsi_prev) else
               "falling" if (rsi is not None and rsi_prev is not None and rsi < rsi_prev) else "flat")
    rsi_note = None
    if rsi is not None:
        tag = ("overbought" if rsi >= 70 else "oversold" if rsi <= 30 else
               "cooling from high" if (rsi_dir == "falling" and rsi >= 60) else
               "turning up from low" if (rsi_dir == "rising" and rsi <= 40) else "neutral")
        rsi_note = f"RSI {rsi:.0f} ({rsi_dir}, {tag})"
    vr = ind.get("vol_ratio")
    vol_note = ("volume above average" if (vr and vr > 1.1) else
                "volume fading" if (vr and vr < 0.9) else "volume normal") if vr is not None else None
    atr_pct = (atr / price * 100) if (atr and price) else None
    atr_note = f"volatility ~{atr_pct:.2f}% of price" if atr_pct else None

    e20, e50 = ind.get("ema20"), ind.get("ema50")
    up_trend = bool(e20 and e50 and e20 > e50)

    # --- leans ---
    short = "neutral — no immediate level pressure."
    if near_res and res_d is not None and res_d <= _NEAR_ATR and rsi_dir == "falling":
        short = (f"pullback risk — price is right under {near_res['tf'].upper()} resistance "
                 f"{_fmt(near_res['price'])} and RSI is cooling; a dip toward "
                 f"{_fmt(near_sup['price']) if near_sup else 'support'} is likely.")
    elif near_sup and sup_d is not None and sup_d <= _NEAR_ATR and rsi_dir == "rising":
        short = (f"bounce likely — price is on {near_sup['tf'].upper()} support "
                 f"{_fmt(near_sup['price'])} with RSI turning up.")
    elif near_res and res_d is not None and res_d <= _NEAR_ATR:
        short = (f"testing {near_res['tf'].upper()} resistance {_fmt(near_res['price'])} — watch for a "
                 "rejection (down) or a clean break (continuation up).")
    elif near_sup and sup_d is not None and sup_d <= _NEAR_ATR:
        short = (f"testing {near_sup['tf'].upper()} support {_fmt(near_sup['price'])} — watch for a "
                 "bounce (up) or a breakdown (down).")

    if struct and struct > 0.5 and up_trend:
        medium = ("up — bullish structure with the trend; dips into support are opportunities while "
                  "structure holds.")
    elif struct and struct < -0.5 and not up_trend:
        medium = ("down — bearish structure with the trend; rallies into resistance are opportunities "
                  "while structure holds.")
    else:
        medium = "unclear — mixed structure/trend; wait for a clean break of the nearest level."
    if choch:
        medium += " NOTE: a recent change-of-character (structure break) warns the trend may be turning."

    watch = None
    if near_sup and near_res:
        watch = (f"Watch {near_sup['tf'].upper()} support {_fmt(near_sup['price'])} vs "
                 f"{near_res['tf'].upper()} resistance {_fmt(near_res['price'])}: holding support keeps "
                 "the structure intact; a clean close beyond a level sets the next leg.")

    at_res = res_d is not None and res_d <= _NEAR_ATR
    at_sup = sup_d is not None and sup_d <= _NEAR_ATR
    macd = ind.get("macd_hist")

    # --- price action: candle bodies + wicks (is momentum in the candles fading?) ---
    cs = candle_by_tf[entry_tf]

    def _body(c):
        return abs(c.close - c.open)

    last5, prev5 = cs[-5:], (cs[-10:-5] if len(cs) >= 10 else cs[:-5])
    ab_now = sum(_body(c) for c in last5) / len(last5) if last5 else 0.0
    ab_prev = sum(_body(c) for c in prev5) / len(prev5) if prev5 else ab_now
    shrinking = ab_prev > 0 and ab_now < 0.7 * ab_prev
    up_wicks = sum(1 for c in last5 if (c.high - max(c.open, c.close)) > _body(c)) >= 2
    lo_wicks = sum(1 for c in last5 if (min(c.open, c.close) - c.low) > _body(c)) >= 2
    if shrinking and up_wicks:
        price_action = "small candle bodies with upper wicks — buyers losing steam / rejection"
    elif shrinking and lo_wicks:
        price_action = "small bodies with lower wicks — sellers losing steam"
    elif shrinking:
        price_action = "candle bodies shrinking — momentum fading"
    else:
        price_action = "candle bodies healthy — momentum intact"

    # --- volume trend (expanding vs fading INTO the current move) ---
    vnow = sum(c.volume for c in cs[-3:]) / 3 if len(cs) >= 3 else 0.0
    vprev = sum(c.volume for c in cs[-8:-3]) / 5 if len(cs) >= 8 else vnow
    volume_trend = ("expanding" if vnow > 1.15 * vprev else
                    "fading" if (vprev > 0 and vnow < 0.85 * vprev) else "steady")

    # --- factor scorecard -------------------------------------------------------------------
    # Each row carries THREE things, because a reading on its own doesn't tell you what to do:
    #   note    — what the indicator actually says, with its numbers
    #   implies — which SIDE that reading argues for (long / short / neither), in plain words
    #   signal  — 🟢 supports long · 🔴 supports short · 🟡 no directional edge / mixed
    # The colour deliberately means "which side", not "good/bad": a red RSI is not a warning, it's
    # evidence for the short case, and reading it as a warning on a short is exactly backwards.
    SIG = {"bull": "🟢", "warn": "🟡", "bear": "🔴"}
    LONG, SHORT, NEITHER = "supports LONG", "supports SHORT", "no directional edge"

    macd_prev = ind.get("macd_hist_prev")
    macd_cross = ind.get("macd_cross") or 0.0
    div_bull = bool(ind.get("macd_div_bull"))
    div_bear = bool(ind.get("macd_div_bear"))
    adx_v, adx_prev = ind.get("adx"), ind.get("adx_prev")

    rows: list[dict] = []

    # 1) Structure — the swing skeleton. Highest-weight read on this panel.
    st_sig = "bull" if (struct and struct > 0.5) else "bear" if (struct and struct < -0.5) else "warn"
    st_note = struct_label
    if choch:
        st_note += " · change-of-character just printed (the trend may be turning)"
    rows.append({
        "factor": "Structure", "signal": SIG[st_sig], "note": st_note,
        "implies": (LONG if st_sig == "bull" else SHORT if st_sig == "bear" else
                    "no directional edge — the swings are not making a trend"),
    })

    # 2) Trend — EMA20 vs EMA50, with the actual gap so "barely crossed" reads differently from
    #    "firmly stacked". The gap is in ATR, so it means the same thing on gold as on an index.
    tr_sig = "bull" if up_trend else "bear" if (e20 and e50 and e20 < e50) else "warn"
    if e20 and e50:
        gap_atr = abs(e20 - e50) / atr if atr else None
        firmness = ("tightly stacked — a weak, easily-flipped trend" if (gap_atr is not None and gap_atr < 0.25)
                    else "clearly separated" if (gap_atr is not None and gap_atr >= 0.6) else "moderately separated")
        tr_note = (f"EMA20 {_fmt(e20)} {'above' if up_trend else 'below'} EMA50 {_fmt(e50)}"
                   + (f" by {gap_atr:.2f} ATR — {firmness}" if gap_atr is not None else ""))
    else:
        tr_note = "EMA20 vs EMA50 unavailable"
    if adx_v is not None:
        strength = "strong trend" if adx_v >= 25 else "no real trend — ranging" if adx_v < 20 else "moderate/undecided"
        building = ("building" if (adx_prev is not None and adx_v > adx_prev)
                    else "fading" if (adx_prev is not None and adx_v < adx_prev) else "flat")
        tr_note += f" · ADX {adx_v:.0f} ({strength}, {building})"
    rows.append({
        "factor": "Trend", "signal": SIG[tr_sig], "note": tr_note,
        "implies": (LONG if tr_sig == "bull" else SHORT if tr_sig == "bear" else NEITHER)
                   + ("; but ADX says there is no trend to follow — treat breakouts as suspect"
                      if (adx_v is not None and adx_v < 20) else ""),
    })

    # 3) Momentum from the candles themselves — bodies and wicks, which turn before any indicator.
    if shrinking and up_wicks:
        mom_implies = "supports SHORT — sellers are capping every push up"
    elif shrinking and lo_wicks:
        mom_implies = "supports LONG — sellers can't press it lower"
    elif shrinking:
        mom_implies = "no directional edge — the move is running out of fuel, wait"
    else:
        mom_implies = (LONG if up_trend else SHORT) + " — the candles are still committing to the move"
    # Derive the colour FROM the verdict. Computing it separately let the two disagree — the row
    # showed a red dot next to "supports LONG", which is worse than showing no dot at all.
    mom_sig = ("bull" if mom_implies.startswith(LONG) else
               "bear" if mom_implies.startswith(SHORT) else "warn")
    rows.append({"factor": "Momentum", "signal": SIG[mom_sig], "note": price_action, "implies": mom_implies})

    # 4) MACD — direction, whether the histogram is EXPANDING or SHRINKING (momentum accelerating
    #    or bleeding out), fresh crosses, and divergence, which is the earliest reversal warning.
    if macd is None:
        rows.append({"factor": "MACD", "signal": SIG["warn"], "note": "unavailable", "implies": NEITHER})
    else:
        above = macd > 0
        expanding = macd_prev is not None and abs(macd) > abs(macd_prev)
        bits = [f"histogram {macd:+.5f} — {'above' if above else 'below'} zero "
                f"({'bullish' if above else 'bearish'} side)"]
        bits.append("expanding — momentum accelerating" if expanding else
                    "shrinking — momentum bleeding out of the move")
        if macd_cross > 0:
            bits.append("fresh BULLISH cross on the last bar")
        elif macd_cross < 0:
            bits.append("fresh BEARISH cross on the last bar")
        if div_bull:
            bits.append("BULLISH divergence — price made a lower low, MACD did not")
        if div_bear:
            bits.append("BEARISH divergence — price made a higher high, MACD did not")
        macd_sig = "bull" if above else "bear"
        if div_bear and above:
            macd_sig, macd_impl = "warn", ("supports SHORT soon — price is still rising but the engine "
                                           "behind it is not; this is a classic top warning")
        elif div_bull and not above:
            macd_sig, macd_impl = "warn", ("supports LONG soon — price is still falling but selling "
                                           "pressure is already easing; a classic bottom warning")
        elif above and expanding:
            macd_impl = "supports LONG — and strengthening, which favours holding a long, not fading it"
        elif above and not expanding:
            macd_impl = "supports LONG but weakening — fine for holding, poor for a fresh entry"
        elif not above and expanding:
            macd_impl = "supports SHORT — and strengthening; rallies are more likely to get sold"
        else:
            macd_impl = "supports SHORT but weakening — the down-move is tiring, don't chase it lower"
        rows.append({"factor": "MACD", "signal": SIG[macd_sig], "note": " · ".join(bits), "implies": macd_impl})

    # 5) RSI — the level AND the direction. 72-and-still-rising means a strong trend you don't
    #    fight; 72-and-falling means the buyers already quit. Same number, opposite trades.
    if rsi is None:
        rows.append({"factor": "RSI", "signal": SIG["warn"], "note": "unavailable", "implies": NEITHER})
    else:
        if rsi >= 70:
            rsi_impl = ("supports SHORT — overbought AND rolling over: buyers ran out and the "
                        "unwind has started" if rsi_dir == "falling" else
                        "no directional edge — overbought but still rising; strong trends stay "
                        "overbought, so shorting this is fighting the move. Wait for it to turn down")
            rsi_sig = "bear" if rsi_dir == "falling" else "warn"
        elif rsi <= 30:
            rsi_impl = ("supports LONG — oversold AND turning up: sellers are exhausted"
                        if rsi_dir == "rising" else
                        "no directional edge — oversold but still falling; don't catch it mid-drop. "
                        "Wait for it to turn up")
            rsi_sig = "bull" if rsi_dir == "rising" else "warn"
        elif rsi >= 55:
            rsi_impl = ("supports SHORT (mild) — momentum cooling off the highs"
                        if rsi_dir == "falling" else "supports LONG — momentum on the buyers' side")
            rsi_sig = "warn" if rsi_dir == "falling" else "bull"
        elif rsi <= 45:
            rsi_impl = ("supports LONG (mild) — pressure lifting off the lows"
                        if rsi_dir == "rising" else "supports SHORT — momentum on the sellers' side")
            rsi_sig = "warn" if rsi_dir == "rising" else "bear"
        else:
            rsi_impl = "no directional edge — RSI in the middle says nothing either way"
            rsi_sig = "warn"
        rows.append({"factor": "RSI", "signal": SIG[rsi_sig], "note": rsi_note or "—", "implies": rsi_impl})

    # 6) Location — where price sits BETWEEN the walls. The single most common way to lose money is
    #    a correct direction entered in the wrong place, so this gets its own row.
    if near_res and near_sup and res_d is not None and sup_d is not None:
        loc_note = (f"{res_d:.1f} ATR below {near_res['tf'].upper()} resistance {_fmt(near_res['price'])} "
                    f"({_strength(res_tests)}, {res_tests}x tested) · "
                    f"{sup_d:.1f} ATR above {near_sup['tf'].upper()} support {_fmt(near_sup['price'])} "
                    f"({_strength(sup_tests)}, {sup_tests}x tested)")
        if at_res:
            loc_sig, loc_impl = "bear", ("supports SHORT / do NOT buy here — you would be buying "
                                         "straight into the wall, with your stop the wrong side of it")
        elif at_sup:
            loc_sig, loc_impl = "bull", ("supports LONG / do NOT sell here — you would be shorting "
                                         "into the floor")
        elif res_d > sup_d:
            loc_sig, loc_impl = "bull", ("supports LONG — more room to the upside than the "
                                         "downside, so a long risks less to reach its target")
        else:
            loc_sig, loc_impl = "bear", ("supports SHORT — more room below than above, so a short "
                                         "risks less to reach its target")
    else:
        loc_note, loc_sig, loc_impl = "levels unavailable", "warn", NEITHER
    rows.append({"factor": "Location", "signal": SIG[loc_sig], "note": loc_note, "implies": loc_impl})

    # 7) Volume — not directional on its own; it says whether to BELIEVE the move. So it stays 🟡:
    # a green dot here would read as "supports long" when it means "supports whatever is happening".
    vol_note_full = f"volume {volume_trend}" + (f" · {vol_note}" if vol_note else "")
    if volume_trend == "expanding":
        vol_impl = ("confirms whichever way price is going — real participation, so a break here is "
                    "more likely to be genuine than a fake")
    elif volume_trend == "fading":
        vol_impl = ("warns against the current move — it's running on fumes; breaks on fading volume "
                    "are the ones that snap back")
    else:
        vol_impl = "neutral — volume is neither confirming nor denying the move"
    rows.append({"factor": "Volume", "signal": SIG["warn"], "note": vol_note_full, "implies": vol_impl})

    # 8) Volatility — sizing and breakout readiness, both of which change what a valid trade is.
    vol_state = compression["state"]
    volat_note = (atr_note or "volatility unavailable") + f" · {vol_state}"
    if var is not None and var <= 0.85:
        volat_impl = ("favours a BREAKOUT trade over a fade — a coiled range usually resolves with a "
                      "run, and stops are cheap here because ATR is small")
    elif var is not None and var >= 1.4:
        volat_impl = ("favours patience — volatility is already spent, so entries are expensive "
                      "(wide stop) and fakeouts are common")
    else:
        volat_impl = "normal conditions — no special adjustment needed"
    rows.append({"factor": "Volatility", "signal": SIG["warn"], "note": volat_note, "implies": volat_impl})

    scorecard = rows

    # A one-line tally so the panel can be read in two seconds before reading it properly.
    n_long = sum(1 for r in rows if r["implies"].startswith("supports LONG"))
    n_short = sum(1 for r in rows if r["implies"].startswith("supports SHORT"))
    if n_long > n_short:
        tally = f"{n_long} of {len(rows)} factors lean LONG vs {n_short} short"
    elif n_short > n_long:
        tally = f"{n_short} of {len(rows)} factors lean SHORT vs {n_long} long"
    else:
        tally = f"evenly split — {n_long} long vs {n_short} short: no edge either way"

    # --- timeframe comparison: this chart vs the two above it -----------------------------------
    # Trading against the higher timeframes is the most expensive habit there is, and it's invisible
    # from a single chart — a clean 5m long looks identical whether the 4h is rising or collapsing.
    # Each rung gets the same four reads plus a verdict, then one sentence on whether they agree.
    def _tf_read(tfx: str, is_chart: bool) -> dict | None:
        t = next((x for x in tech.timeframes if x.timeframe == tfx), None)
        if t is None:
            return None
        i = t.indicators
        e20x, e50x = i.get("ema20"), i.get("ema50")
        trend = ("up" if (e20x and e50x and e20x > e50x)
                 else "down" if (e20x and e50x and e20x < e50x) else "flat")
        stx = i.get("structure")
        structure = "HH/HL" if (stx and stx > 0.5) else "LH/LL" if (stx and stx < -0.5) else "range"
        rx, rxp = i.get("rsi14"), i.get("rsi14_prev")
        arrow = ("↑" if (rx is not None and rxp is not None and rx > rxp)
                 else "↓" if (rx is not None and rxp is not None and rx < rxp) else "→")
        mh = i.get("macd_hist")
        macd_s = "+" if (mh is not None and mh > 0) else "−" if (mh is not None and mh < 0) else "0"
        # Three independent votes; 2 of 3 carries it, otherwise the timeframe is genuinely mixed.
        votes = sum([
            1 if trend == "up" else -1 if trend == "down" else 0,
            1 if structure == "HH/HL" else -1 if structure == "LH/LL" else 0,
            1 if macd_s == "+" else -1 if macd_s == "−" else 0,
        ])
        verdict = "bullish" if votes >= 2 else "bearish" if votes <= -2 else "mixed"
        rsi_txt = f"RSI {rx:.0f}{arrow}" if rx is not None else "RSI —"
        return {
            "tf": tfx, "is_chart": is_chart, "verdict": verdict, "votes": votes,
            "signal": SIG["bull"] if verdict == "bullish" else SIG["bear"] if verdict == "bearish" else SIG["warn"],
            "note": f"EMA {trend} · {structure} · {rsi_txt} · MACD {macd_s}",
        }

    tf_compare = [r for r in ([_tf_read(entry_tf, True)]
                              + [_tf_read(t, False) for t in higher_tfs]) if r]

    alignment = None
    if len(tf_compare) >= 2:
        me, above = tf_compare[0], tf_compare[1:]
        agree = [r for r in above if me["verdict"] != "mixed" and r["verdict"] == me["verdict"]]
        against = [r for r in above if me["verdict"] != "mixed" and r["verdict"] != "mixed"
                   and r["verdict"] != me["verdict"]]
        if me["verdict"] == "mixed" and all(r["verdict"] == "mixed" for r in above):
            alignment = (f"NO READ — {me['tf']} and the timeframes above it are all mixed. There is "
                         "nothing to align with; this is a wait, not a trade.")
        elif me["verdict"] == "mixed":
            clean = ", ".join(f"{r['tf']} is {r['verdict']}" for r in above if r["verdict"] != "mixed")
            alignment = (f"UNCLEAR — your {me['tf']} has no clean read while {clean}. Let the higher "
                         f"timeframe pick the side, then wait for the {me['tf']} to agree before entering.")
        elif len(agree) == len(above):
            side = "longs" if me["verdict"] == "bullish" else "shorts"
            alignment = (f"ALIGNED — {', '.join(r['tf'] for r in tf_compare)} are all {me['verdict']}. "
                         f"This is the highest-quality setup type: trade {side} only, and give them room.")
        elif against:
            alignment = (f"CONFLICTED — your {me['tf']} is {me['verdict']} but "
                         + ", ".join(f"{r['tf']} is {r['verdict']}" for r in against)
                         + ". You would be trading against the bigger picture: expect the move to "
                           "stall early, so take profit sooner, size smaller, or skip it.")
        else:
            # Some above agree, the rest are mixed. Only name the ones that AREN'T confirming —
            # listing the agreeing ones after a "but" reads as a contradiction.
            agreed = {r["tf"] for r in agree}
            quiet = ", ".join(f"{r['tf']} is {r['verdict']}" for r in above if r["tf"] not in agreed)
            alignment = (f"PARTIAL — {me['tf']} is {me['verdict']}"
                         + (f", {', '.join(sorted(agreed))} agrees" if agreed else "")
                         + f", but {quiet} — not confirming it. Workable, but not the A+ case: "
                           "keep the target modest.")

    # --- overall bias + scenarios + invalidation + playbook ---
    bull_struct = bool(struct and struct > 0.5 and up_trend)
    bear_struct = bool(struct and struct < -0.5 and not up_trend)
    weakening = shrinking or at_res or volume_trend == "fading" or (rsi_dir == "falling" and rsi is not None and rsi >= 55)
    if bull_struct:
        overall_bias = "🟢 bullish — but likely a pullback first" if weakening else "🟢 bullish"
    elif bear_struct:
        overall_bias = "🔴 bearish — but likely a bounce first" if (not weakening) else "🔴 bearish"
    else:
        overall_bias = "🟡 neutral / range — wait for a clean break"

    scenarios, invalidation, playbook = [], None, None
    if bull_struct:
        if weakening or at_res:
            scenarios = [
                {"prob": "~60%", "label": "Pullback first, then up",
                 "text": (f"dip toward {near_sup['tf'].upper()} support {_fmt(near_sup['price'])} / the "
                          "channel that holds, buyers return, then another attempt higher."
                          if near_sup else "a healthy pullback that holds, then buyers return.")},
                {"prob": "~40%", "label": "Immediate breakout",
                 "text": (f"a strong close above {near_res['tf'].upper()} resistance {_fmt(near_res['price'])} "
                          "on rising volume continues the trend." if near_res else
                          "a strong breakout continues higher.")},
            ]
            playbook = ("Don't chase a long here — you'd be buying into "
                        + (f"{near_res['tf'].upper()} resistance {_fmt(near_res['price'])} " if near_res else "resistance ")
                        + "after a run. Don't short — structure is still bullish. Wait for EITHER (1) a "
                        + (f"pullback into {_fmt(near_sup['price'])} " if near_sup else "pullback into support ")
                        + "that holds with buyers returning, OR (2) a strong breakout above "
                        + (f"{_fmt(near_res['price'])} " if near_res else "resistance ") + "on rising volume.")
        else:
            scenarios = [
                {"prob": "~65%", "label": "Continuation up",
                 "text": "trend + momentum intact; dips into support are buys."},
                {"prob": "~35%", "label": "Stall at resistance",
                 "text": (f"stalls at {near_res['tf'].upper()} {_fmt(near_res['price'])} and pulls back."
                          if near_res else "stalls and pulls back.")},
            ]
            playbook = "Trend is up and healthy — favour buying dips into support; don't chase far from value."
        if near_sup:
            invalidation = (f"Turns BEARISH on a close below {near_sup['tf'].upper()} support "
                            f"{_fmt(near_sup['price'])} (last Higher Low becomes a Lower Low — structure breaks).")
    elif bear_struct:
        scenarios = [
            {"prob": "~60%", "label": "Continuation down",
             "text": (f"rallies into {near_res['tf'].upper()} resistance {_fmt(near_res['price'])} get sold."
                      if near_res else "rallies get sold.")},
            {"prob": "~40%", "label": "Bounce first",
             "text": (f"a bounce off {near_sup['tf'].upper()} support {_fmt(near_sup['price'])} before lower."
                      if near_sup else "a relief bounce before lower.")},
        ]
        playbook = ("Don't chase a short into support; don't buy — structure is bearish. Wait for a "
                    "rally into resistance to sell, or a clean breakdown to confirm.")
        if near_res:
            invalidation = (f"Turns BULLISH on a close above {near_res['tf'].upper()} resistance "
                            f"{_fmt(near_res['price'])} (breaks the lower-high structure).")
    else:
        # Range. "Wait for a break" is useless without the numbers, so name both breaks with their
        # trigger, target, stop and R:R — that's the difference between advice and a plan.
        def _leg(cand: dict | None, up: bool) -> str:
            if not cand:
                return ("a clean break above the nearest resistance" if up
                        else "a clean break below the nearest support")
            side = "above" if up else "below"
            return (f"close {side} {_fmt(cand['trigger'])} ({cand['strength']}, {cand['tests']}x tested) "
                    f"→ target {_fmt(cand['target'])}, stop {_fmt(cand['stop'])}, "
                    f"about {cand['rr']}:1")

        scenarios = [
            {"prob": "break up", "label": "Long on a break of resistance", "text": _leg(breakout_up, True)},
            {"prob": "break down", "label": "Short on a break of support", "text": _leg(breakdown, False)},
        ]
        mid_warning = ""
        if near_res and near_sup and res_d is not None and sup_d is not None and res_d > 0.8 and sup_d > 0.8:
            mid_warning = (" Price is in the MIDDLE of the range right now — the worst place to enter, "
                           "because both a stop and a target are far away.")
        playbook = (
            "Range — there is no trend to ride, so don't pick a direction; let the range pick it for you. "
            + (f"The box is {_fmt(near_sup['price'])} to {_fmt(near_res['price'])}. " if (near_sup and near_res) else "")
            + "Two valid trades: a close and HOLD above resistance (long), or a close and hold below "
              "support (short). A wick through a level is not a break — wait for the candle to close "
              "beyond it, and prefer it on expanding volume." + mid_warning
            + (" Volatility is compressed, which is exactly when a range resolves into a real run — so "
               "the break is worth waiting for." if (var is not None and var <= 0.85) else "")
        )

    # --- key observations + summary paragraph ---
    bullets = []
    if near_res:
        bullets.append(f"Nearest resistance: {near_res['tf'].upper()} {_fmt(near_res['price'])}"
                       + (f" ({res_d:.1f} ATR above)" if res_d is not None else ""))
    if near_sup:
        bullets.append(f"Nearest support: {near_sup['tf'].upper()} {_fmt(near_sup['price'])}"
                       + (f" ({sup_d:.1f} ATR below)" if sup_d is not None else ""))
    bullets.append(f"Structure: {struct_label}" + (" · change-of-character just printed" if choch else ""))
    if chan_note:
        bullets.append(f"Channel: {chan_note}")
    conf = " · ".join(x for x in (rsi_note, vol_note, atr_note) if x)
    if conf:
        bullets.append(f"Confirmation: {conf}")

    loc = []
    if near_res and res_d is not None:
        loc.append(f"{res_d:.1f} ATR under {near_res['tf'].upper()} resistance {_fmt(near_res['price'])}")
    if near_sup and sup_d is not None:
        loc.append(f"{sup_d:.1f} ATR over {near_sup['tf'].upper()} support {_fmt(near_sup['price'])}")
    summary = (f"{symbol} at {_fmt(price)}: " + (", ".join(loc) + ". " if loc else "")
               + (f"It's {chan_note}. " if chan_note else "")
               + f"Structure is {struct_label}. " + (f"{conf}. " if conf else "")
               + f"Short-term: {short} Medium-term: {medium}")

    return {
        "symbol": symbol, "price": round(price, 8), "timeframe": entry_tf,
        "nearest_resistance": near_res, "nearest_support": near_sup,
        "resistance_ladder": res_ladder, "support_ladder": sup_ladder,
        "breakout_up": breakout_up, "breakdown": breakdown, "compression": compression,
        "level_strength": {
            "resistance": (f"{_strength(res_tests)} ({res_tests}x tested)" if near_res else None),
            "support": (f"{_strength(sup_tests)} ({sup_tests}x tested)" if near_sup else None),
        },
        "structure": struct_label, "choch": choch, "channel": chan_note,
        "rsi": rsi_note, "volume": vol_note, "atr": atr_note,
        "price_action": price_action, "volume_trend": volume_trend,
        "scorecard": scorecard, "tally": tally, "overall_bias": overall_bias,
        "tf_compare": tf_compare, "alignment": alignment,
        "scenarios": scenarios, "invalidation": invalidation, "playbook": playbook,
        "short_term": short, "medium_term": medium, "watch": watch,
        "bullets": bullets, "summary": summary,
    }
