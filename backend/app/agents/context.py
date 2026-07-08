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


def build_context(session: Session, symbol: str, asset_class: AssetClass) -> dict | None:
    """Return the structured + plain-language context read, or None if data is unavailable."""
    from app.agents.pipeline import _timeframes_for
    from app.agents.technical import run_technical
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.data.ohlcv_cache import get_ohlcv_cached

    settings = get_or_create_settings(session)
    broker = get_broker_for(asset_class, settings.broker_map)
    wanted = _timeframes_for("1h")
    series, candle_by_tf = [], {}
    for tf in ("1h", "4h", "1d"):
        try:
            s = get_ohlcv_cached(broker, symbol, tf, 200)
        except Exception as exc:  # noqa: BLE001
            log.warning("context ohlcv failed", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
            continue
        if s and s.candles:
            candle_by_tf[tf] = list(s.candles)
            if tf in wanted:
                series.append(s)
    if "1h" not in candle_by_tf or not series:
        return None
    tech = run_technical(symbol, series, use_llm=False)
    prim = next((x for x in tech.timeframes if x.timeframe == "1h"), None) if tech.timeframes else None
    if prim is None:
        return None
    ind = prim.indicators
    price = ind.get("last_close") or candle_by_tf["1h"][-1].close
    atr = ind.get("atr14") or 0.0

    # --- nearest multi-TF support/resistance ---
    all_levels: dict[str, list[dict]] = {}
    for tf, cs in candle_by_tf.items():
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
        c1h = candle_by_tf["1h"][-60:]
        return sum(1 for c in c1h if (c.low - tol) <= level <= (c.high + tol))

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
    cs = candle_by_tf["1h"]

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

    # --- factor scorecard (🟢 bull / 🟡 weakening / 🔴 bear-or-strong-resistance) ---
    SIG = {"bull": "🟢", "warn": "🟡", "bear": "🔴"}
    st_sig = "bull" if (struct and struct > 0.5) else "bear" if (struct and struct < -0.5) else "warn"
    tr_sig = "bull" if up_trend else "bear" if (e20 and e50 and e20 < e50) else "warn"
    mom_sig = "warn" if shrinking else ("bull" if (macd is not None and ((macd > 0) == up_trend)) else "bear")
    rsi_sig = ("bear" if (rsi is not None and (rsi >= 70 or rsi <= 30)) else
               "warn" if (rsi_dir == "falling" and rsi is not None and rsi >= 55) else
               "bull" if rsi_dir == "rising" else "warn")
    res_sig = "bear" if at_res else "bull"
    vol_sig = "bull" if volume_trend == "expanding" else "bear" if volume_trend == "fading" else "warn"
    scorecard = [
        {"factor": "Structure", "signal": SIG[st_sig], "note": struct_label},
        {"factor": "Trend", "signal": SIG[tr_sig], "note": "EMA20 vs EMA50"},
        {"factor": "Momentum", "signal": SIG[mom_sig], "note": price_action},
        {"factor": "RSI", "signal": SIG[rsi_sig], "note": rsi_note or "—"},
        {"factor": "Resistance", "signal": SIG[res_sig],
         "note": (f"at {near_res['tf'].upper()} {_fmt(near_res['price'])}" if at_res and near_res
                  else "clear overhead")},
        {"factor": "Volume", "signal": SIG[vol_sig], "note": f"volume {volume_trend}"},
    ]

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
        scenarios = [{"prob": "range", "label": "Wait for a break",
                      "text": "no clear swing trend — trade the break of the nearest level, not the middle."}]
        playbook = "Range / unclear — stand aside until price breaks and holds beyond a key level."

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
        "symbol": symbol, "price": round(price, 8), "timeframe": "1h",
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
        "scorecard": scorecard, "overall_bias": overall_bias,
        "scenarios": scenarios, "invalidation": invalidation, "playbook": playbook,
        "short_term": short, "medium_term": medium, "watch": watch,
        "bullets": bullets, "summary": summary,
    }
