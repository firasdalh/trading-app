"""Tests for the indicator bundle (ATR/ADX/MACD/Bollinger/EMA/volume) and the ADX chop gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.indicators import (
    adx, atr, bollinger, ema, macd, market_structure, regression_channel, supertrend, volume_ratio,
)
from app.agents.orchestrator import _deterministic_decision, _regime, _session_quality
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import Candle, FundamentalRead, TechnicalRead, TimeframeRead

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ramp(n=120, start=100.0, step=0.5, rng=2.0) -> list[Candle]:
    out, price = [], start
    for i in range(n):
        o = price
        price += step
        out.append(Candle(ts=NOW + timedelta(hours=i), open=o, high=max(o, price) + rng / 2,
                          low=min(o, price) - rng / 2, close=price, volume=1000 + i))
    return out


def _flat(n=120, price=100.0, rng=2.0) -> list[Candle]:
    return [Candle(ts=NOW + timedelta(hours=i), open=price, high=price + rng / 2,
                   low=price - rng / 2, close=price, volume=1000) for i in range(n)]


def test_regression_channel_up_and_position():
    c = _ramp(80, step=0.5)                       # clean up-channel
    ch = regression_channel(c)
    assert ch is not None
    assert ch["slope"] > 0 and ch["r2"] > 0.8     # rising, clean fit
    base_pos = ch["pos"]
    # Push the last close far above the fit -> it sits at/above the upper (resistance) band.
    hi = list(c)
    last = hi[-1]
    hi[-1] = Candle(ts=last.ts, open=last.open, high=last.close + 20, low=last.low,
                    close=last.close + 15, volume=1000)
    assert regression_channel(hi)["pos"] > base_pos and regression_channel(hi)["pos"] > 0.9
    assert regression_channel(c[:10]) is None      # too little data


# ---- indicator math ----

def test_ema_in_range_and_lags_price():
    closes = [float(i) for i in range(1, 31)]
    e = ema(closes, 10)
    # On a rising series the EMA sits below the latest price but above the window start.
    assert e is not None and closes[20] < e < closes[-1]


def test_atr_constant_range():
    a = atr(_flat(rng=2.0))
    assert a is not None and abs(a - 2.0) < 1e-6


def test_adx_strong_in_trend_weak_in_range():
    up = adx(_ramp())
    flat = adx(_flat())
    assert up is not None and up["adx"] > 25 and up["plus_di"] > up["minus_di"]
    assert flat is not None and flat["adx"] < 20


def test_macd_sign_follows_direction():
    # MACD line = fast EMA - slow EMA: positive in an uptrend, negative in a downtrend.
    # (On a perfectly linear ramp the histogram is ~0, which is correct, so assert the line.)
    up = macd([float(i) for i in range(1, 80)])
    down = macd([float(i) for i in range(80, 1, -1)])
    assert up is not None and up["macd"] > 0
    assert down is not None and down["macd"] < 0


def _vol_trend(vols: list[float]):
    """The vol_trend indicator for a gentle uptrend carrying the given per-bar volumes."""
    from app.agents.technical import run_technical
    from app.models.schemas import OHLCVSeries

    out, price = [], 100.0
    for i, v in enumerate(vols):
        o = price
        price += 0.2
        out.append(Candle(ts=NOW + timedelta(hours=i), open=o, high=price + 0.5,
                          low=o - 0.5, close=price, volume=v))
    tr = run_technical("TEST", [OHLCVSeries(symbol="TEST", timeframe="1h", candles=out)], use_llm=False)
    return tr.timeframes[0].indicators.get("vol_trend")


def test_vol_trend_expanding_flat_fading():
    # +1 when the last 3 bars' volume expands vs the prior 5, -1 when it fades, 0 when flat.
    n = 30
    assert _vol_trend([1000.0] * n) == 0.0
    assert _vol_trend([1000.0] * (n - 3) + [2000.0, 2000.0, 2000.0]) == 1.0
    assert _vol_trend([1000.0] * (n - 3) + [400.0, 400.0, 400.0]) == -1.0


def test_supertrend_direction_and_side():
    r_up = _ramp()
    up = supertrend(r_up)
    r_dn = _ramp(start=160.0, step=-0.5)
    dn = supertrend(r_dn)
    # Uptrend: line below price (green support); downtrend: line above price (red resistance).
    assert up is not None and up["dir"] == 1 and up["line"] < r_up[-1].close
    assert dn is not None and dn["dir"] == -1 and dn["line"] > r_dn[-1].close


def test_bollinger_flat_zero_width():
    bb = bollinger([100.0] * 30)
    assert bb is not None and bb["width"] == 0.0 and bb["upper"] == bb["lower"] == bb["mid"]


def test_volume_ratio():
    candles = _flat(n=25)
    candles[-1] = Candle(ts=candles[-1].ts, open=100, high=101, low=99, close=100, volume=2000)
    vr = volume_ratio(candles, period=20)
    assert vr is not None and vr > 1.5


# ---- orchestrator gates ----

def _tech(adx_v=30.0, macd_hist=1.0, trend="up", entry=100.0, atr_v=2.0) -> TechnicalRead:
    ind = {"last_close": entry, "atr14": atr_v, "adx": adx_v, "macd_hist": macd_hist,
           "ema20": entry, "vol_ratio": 1.3}
    return TechnicalRead(symbol="X", overall_trend=trend, confidence=0.6,
                         timeframes=[TimeframeRead(timeframe="1h", trend=trend,
                                                   support_levels=[entry - 5], resistance_levels=[entry + 5],
                                                   indicators=ind)])


def _fund(bias=TradingBias.NEUTRAL) -> FundamentalRead:
    return FundamentalRead(symbol="X", bias=bias)


def test_adx_chop_gate_blocks_low_adx():
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech(adx_v=12.0), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and "ranging" in p.rationale.lower()


def test_macd_conflict_blocks_long():
    # Uptrend but negative MACD momentum -> sit out (a "watching" pullback, not a flat reject).
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech(trend="up", macd_hist=-1.0), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE
    assert p.watch is True and "pullback" in p.rationale.lower()


def test_atr_based_stop_distance():
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech(entry=100.0, atr_v=2.0), _fund(), now=NOW)
    assert p.direction == Direction.LONG
    # Stop ~ 1.5 * ATR below entry (or tighter to structure).
    assert p.stop_loss is not None and p.entry is not None
    assert p.entry - p.stop_loss <= 1.5 * 2.0 + 1e-6
    assert p.take_profit and p.take_profit > p.entry


def test_strong_trend_high_confidence():
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech(adx_v=35.0, macd_hist=2.0), _fund(), now=NOW)
    assert p.direction == Direction.LONG and p.confidence >= 0.6


def _tech_ext(trend, *, entry, ema20, atr_v=2.0, adx_v=30.0, macd_hist=None):
    if macd_hist is None:
        macd_hist = 1.0 if trend == "up" else -1.0
    ind = {"last_close": entry, "atr14": atr_v, "adx": adx_v, "macd_hist": macd_hist,
           "ema20": ema20, "vol_ratio": 1.3}
    return TechnicalRead(symbol="X", overall_trend=trend, confidence=0.6,
                         timeframes=[TimeframeRead(timeframe="1h", trend=trend, indicators=ind,
                                                   support_levels=[entry - 10], resistance_levels=[entry + 10])])


def test_overextension_lowers_confidence_but_still_trades():
    # Short stretched > 2.5 ATR below EMA20 still trades (you can't always wait for a pullback in
    # a trend) but at LOWER confidence than a near-the-mean entry.
    stretched = _tech_ext("down", entry=100.0, ema20=106.0, atr_v=2.0)  # 100 < 106 - 5
    near = _tech_ext("down", entry=104.0, ema20=106.0, atr_v=2.0)       # within 2.5 ATR
    p_stretched = _deterministic_decision("X", AssetClass.FOREX, "1h", stretched, _fund(), now=NOW)
    p_near = _deterministic_decision("X", AssetClass.FOREX, "1h", near, _fund(), now=NOW)
    assert p_stretched.direction == Direction.SHORT and p_near.direction == Direction.SHORT
    assert p_stretched.confidence < p_near.confidence
    assert "stretched" in p_stretched.rationale.lower()


def test_trivial_counter_momentum_does_not_block():
    # Tiny counter-momentum (|hist| < 10% of ATR) shouldn't sit the trade out.
    t = _tech_ext("up", entry=100.0, ema20=100.0, atr_v=2.0, macd_hist=-0.1)  # |0.1| < 0.2
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(), now=NOW)
    assert p.direction == Direction.LONG


def test_cross_tf_momentum_conflict_lowers_confidence():
    def build(macro_macd):
        ind1 = {"last_close": 100.0, "atr14": 2.0, "adx": 30.0, "macd_hist": -1.0, "ema20": 100.0}
        return TechnicalRead(symbol="X", overall_trend="down", confidence=0.6, timeframes=[
            TimeframeRead(timeframe="1h", trend="down", indicators=ind1,
                          support_levels=[90], resistance_levels=[110]),
            TimeframeRead(timeframe="1d", trend="down", indicators={"macd_hist": macro_macd}),
        ])

    agree = _deterministic_decision("X", AssetClass.FOREX, "1h", build(-1.0), _fund(), now=NOW)
    conflict = _deterministic_decision("X", AssetClass.FOREX, "1h", build(1.0), _fund(), now=NOW)
    assert agree.direction == Direction.SHORT and conflict.direction == Direction.SHORT
    assert conflict.confidence < agree.confidence
    assert "conflict" in conflict.rationale.lower()


def _multi_tf(primary_trend, macro_trend, *, resistance=120.0, support=90.0, entry=100.0, adx=30.0):
    ind = {"last_close": entry, "atr14": 2.0, "adx": adx, "macd_hist": 1.0 if primary_trend == "up" else -1.0}
    return TechnicalRead(
        symbol="X", overall_trend=primary_trend, confidence=0.6,
        timeframes=[
            TimeframeRead(timeframe="1h", trend=primary_trend, indicators=ind,
                          support_levels=[support], resistance_levels=[resistance]),
            TimeframeRead(timeframe="1d", trend=macro_trend, indicators={},
                          support_levels=[], resistance_levels=[]),
        ],
    )


def test_mtf_blocks_long_against_higher_tf_downtrend():
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _multi_tf("up", "down"), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and "higher-timeframe" in p.rationale.lower()


def test_mtf_allows_long_when_higher_tf_agrees():
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _multi_tf("up", "up"), _fund(), now=NOW)
    assert p.direction == Direction.LONG


def test_target_capped_at_resistance():
    # Moderate trend (ADX 22): risk = 1.5*ATR(2) = 3; raw 2R target = 106; resistance 105 (1.67R,
    # above the 1.5R floor but below 2R) caps the target there.
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("up", "up", resistance=105.0, adx=22.0), _fund(), now=NOW)
    assert p.direction == Direction.LONG and p.take_profit == 105.0


def test_too_little_room_to_resistance_vetoes():
    # Moderate trend: resistance 102 is past the breakout buffer but only ~0.7R above entry — below
    # the 1.5R minimum to take a market entry, so the engine stands aside (no actionable trade).
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("up", "up", resistance=102.0, adx=22.0), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and "aside" in p.rationale.lower()


def test_thin_rr_market_entry_stands_aside_at_1r():
    # The USOIL case: a moderate trend whose only target gives ~1R at market (wide ATR stop) is ~1:1
    # — negative expectancy after costs. The OLD engine took any >=1R trade; now it must stand aside
    # (the 1.5R floor) and stay a non-actionable watch rather than propose the thin trade.
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("up", "up", resistance=103.0, adx=22.0), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and p.watch is True and not p.is_actionable
    assert "1.5r" in p.rationale.lower()


def test_strong_trend_ignores_immediate_structure():
    # Strong downtrend (ADX 35) with support only ~0.8R below entry: the XAUUSDm case.
    # Should still SHORT (breakout runner), not veto on "too little room".
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("down", "down", support=97.6, adx=35.0), _fund(), now=NOW)
    assert p.direction == Direction.SHORT
    assert p.take_profit is not None and p.take_profit < p.entry


def test_trend_only_stands_aside_in_moderate_regime():
    # A moderate-regime (ADX 22) setup trades normally, but stands aside under trend-only mode.
    tech = _multi_tf("up", "up", resistance=105.0, adx=22.0)
    on = _deterministic_decision("X", AssetClass.FOREX, "1h", tech, _fund(), now=NOW, trend_only=True)
    off = _deterministic_decision("X", AssetClass.FOREX, "1h", tech, _fund(), now=NOW, trend_only=False)
    assert on.direction == Direction.NO_TRADE and on.watch and "trend-only" in on.rationale.lower()
    assert off.direction == Direction.LONG


def test_trend_only_still_trades_clear_trend():
    # A clear trend (ADX 30 -> "trending") is still traded under trend-only mode.
    tech = _multi_tf("up", "up", resistance=110.0, adx=30.0)
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", tech, _fund(), now=NOW, trend_only=True)
    assert p.direction == Direction.LONG


def _long_read(swing_low, recent_low):
    # A clean LONG (trend up on both TFs, ADX strong, entry at value) with a swing-low and optional
    # recent wick low, so the structural-stop placement can be exercised.
    ind = {"last_close": 100.0, "atr14": 1.0, "adx": 30.0, "macd_hist": 0.5,
           "ema20": 100.0, "ema50": 99.0, "ema200": 98.0, "structure": 1.0, "swing_low": swing_low}
    if recent_low is not None:
        ind["recent_low"] = recent_low
    macro = {"ema20": 100.0, "ema50": 99.0, "ema200": 98.0}
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators=ind, support_levels=[], resistance_levels=[110.0]),
        TimeframeRead(timeframe="1d", trend="up", indicators=macro, support_levels=[], resistance_levels=[]),
    ])


def test_stop_extends_beyond_recent_wick_long():
    # Beyond-the-wick: when a recent wick (97.5) already pierced the swing low (98.0), the stop is
    # placed BELOW that wick — further than the plain swing stop — to dodge stop-hunts.
    base = _deterministic_decision("X", AssetClass.FOREX, "1h", _long_read(98.0, None), _fund(), now=NOW)
    wick = _deterministic_decision("X", AssetClass.FOREX, "1h", _long_read(98.0, 97.5), _fund(), now=NOW)
    assert base.direction == Direction.LONG and wick.direction == Direction.LONG
    assert wick.stop_loss < base.stop_loss   # wick-aware stop sits further from entry
    assert wick.stop_loss < 97.5             # ...and below the recent wick extreme


def test_stop_unchanged_when_no_wick_beyond_swing_long():
    # If recent lows stayed ABOVE the swing (no wicking), placement is the plain swing stop.
    plain = _deterministic_decision("X", AssetClass.FOREX, "1h", _long_read(98.0, None), _fund(), now=NOW)
    nowick = _deterministic_decision("X", AssetClass.FOREX, "1h", _long_read(98.0, 98.3), _fund(), now=NOW)
    assert plain.stop_loss == nowick.stop_loss


# ---- market structure (swing highs/lows) ----

def _zigzag(n: int, step_up: float, step_dn: float, up_len: int, dn_len: int) -> list[Candle]:
    """A saw-tooth: up_len bars rising by step_up, then dn_len bars falling by step_dn."""
    prices: list[float] = []
    p = 100.0
    while len(prices) < n:
        for _ in range(up_len):
            prices.append(p)
            p += step_up
        for _ in range(dn_len):
            prices.append(p)
            p -= step_dn
    return [Candle(ts=NOW + timedelta(hours=i), open=v, high=v + 0.5, low=v - 0.5, close=v, volume=1.0)
            for i, v in enumerate(prices[:n])]


def test_market_structure_reads_uptrend():
    ms = market_structure(_zigzag(40, step_up=2, step_dn=1, up_len=5, dn_len=3))  # net up
    assert ms["structure"] == "up"
    assert ms["swing_high"] is not None and ms["swing_low"] is not None


def test_market_structure_reads_downtrend():
    ms = market_structure(_zigzag(40, step_up=1, step_dn=2, up_len=3, dn_len=5))  # net down
    assert ms["structure"] == "down"


def test_market_structure_range_on_insufficient_data():
    few = [Candle(ts=NOW + timedelta(hours=i), open=100, high=101, low=99, close=100, volume=1.0)
           for i in range(3)]
    assert market_structure(few)["structure"] == "range"


def test_market_structure_flags_choch():
    candles = _zigzag(36, step_up=2, step_dn=1, up_len=5, dn_len=3)
    sl = market_structure(candles)["swing_low"]
    assert sl is not None
    candles.append(Candle(ts=NOW + timedelta(hours=99), open=sl, high=sl, low=sl - 5,
                          close=sl - 5, volume=1.0))  # close well below the last swing low
    assert market_structure(candles)["choch"] is True


# ---- structural stops (place the stop where the trade is invalidated) ----

def _tech_struct(trend, entry, atr_v=2.0, *, swing_low=None, swing_high=None, adx_v=30.0):
    ind = {"last_close": entry, "atr14": atr_v, "adx": adx_v, "ema20": entry, "vol_ratio": 1.3,
           "macd_hist": 1.0 if trend == "up" else -1.0,
           "structure": 1.0 if trend == "up" else -1.0}
    if swing_low is not None:
        ind["swing_low"] = swing_low
    if swing_high is not None:
        ind["swing_high"] = swing_high
    return TechnicalRead(symbol="X", overall_trend=trend, confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend=trend, indicators=ind,
                      support_levels=[entry - 10], resistance_levels=[entry + 10])])


def test_structural_stop_uses_swing_low_for_long():
    # Swing low 96, ATR 2 -> structural stop = 96 - 0.2*2 = 95.6 (4.4 below entry, within 3xATR).
    t = _tech_struct("up", entry=100.0, atr_v=2.0, swing_low=96.0)
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(), now=NOW)
    assert p.direction == Direction.LONG
    assert p.stop_loss is not None and abs(p.stop_loss - 95.6) < 0.01
    assert "swing" in p.rationale.lower()


def test_structural_stop_uses_swing_high_for_short():
    t = _tech_struct("down", entry=100.0, atr_v=2.0, swing_high=104.0)
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(), now=NOW)
    assert p.direction == Direction.SHORT
    assert p.stop_loss is not None and abs(p.stop_loss - 104.4) < 0.01  # 104 + 0.2*ATR


def test_too_far_swing_falls_back_to_atr_stop():
    # Swing low 80 is 10xATR away -> not a practical stop -> fall back to the 1.5xATR stop (97).
    t = _tech_struct("up", entry=100.0, atr_v=2.0, swing_low=80.0)
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(), now=NOW)
    assert p.direction == Direction.LONG
    assert p.stop_loss is not None and abs(p.stop_loss - 97.0) < 0.01  # 1.5 * ATR


def test_too_close_swing_respects_anti_wick_floor():
    # Swing low 99.5 is only 0.25xATR away -> floor to 1xATR (stop = 98), never get wicked off.
    t = _tech_struct("up", entry=100.0, atr_v=2.0, swing_low=99.5)
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(), now=NOW)
    assert p.direction == Direction.LONG
    assert p.stop_loss is not None and abs(p.stop_loss - 98.0) < 0.01  # entry - 1xATR floor


# ---- regime classification ----

def test_regime_classification():
    assert _regime({"adx": 30.0}) == "trending"
    assert _regime({"adx": 15.0}) == "ranging"
    assert _regime({"adx": 22.0, "vol_atr_ratio": 1.8}) == "volatile"
    assert _regime({"adx": 22.0, "vol_atr_ratio": 1.1}) == "moderate"
    assert _regime({"adx": 30.0, "vol_atr_ratio": 2.5}) == "trending"  # strong trend beats vol


def _tech_regime(vr):
    ind = {"last_close": 100.0, "atr14": 2.0, "adx": 22.0, "macd_hist": -1.0, "ema20": 100.0}
    if vr is not None:
        ind["vol_atr_ratio"] = vr
    return TechnicalRead(symbol="X", overall_trend="down", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="down", indicators=ind,
                      support_levels=[90.0], resistance_levels=[110.0])])


def test_extreme_volatility_stands_aside():
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech_regime(2.5), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and p.watch is True and "whipsaw" in p.rationale.lower()


def test_volatile_regime_lowers_confidence():
    calm = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech_regime(1.1), _fund(), now=NOW)
    volatile = _deterministic_decision("X", AssetClass.FOREX, "1h", _tech_regime(1.8), _fund(), now=NOW)
    assert calm.direction == Direction.SHORT and volatile.direction == Direction.SHORT
    assert volatile.confidence < calm.confidence


# ---- pullback-to-value entry location ----

def test_pullback_entry_at_value_scores_higher_than_chasing():
    at_value = _tech_ext("up", entry=100.0, ema20=100.0, atr_v=2.0)  # at the 20-EMA (pullback)
    mid = _tech_ext("up", entry=104.0, ema20=100.0, atr_v=2.0)       # 2xATR above value (chasing-ish)
    p_val = _deterministic_decision("X", AssetClass.FOREX, "1h", at_value, _fund(), now=NOW)
    p_mid = _deterministic_decision("X", AssetClass.FOREX, "1h", mid, _fund(), now=NOW)
    assert p_val.direction == Direction.LONG and p_mid.direction == Direction.LONG
    assert p_val.confidence > p_mid.confidence
    assert "value" in p_val.rationale.lower()


# ---- session / liquidity weighting ----

def test_session_quality():
    fx = AssetClass.FOREX
    assert _session_quality(fx, "EURUSDm", datetime(2026, 1, 5, 13, tzinfo=timezone.utc))[0] == "active"
    assert _session_quality(fx, "EURUSDm", datetime(2026, 1, 5, 23, tzinfo=timezone.utc))[0] == "thin"
    # JPY pairs trade the Asian session, so thin hours are still 'normal' for them.
    assert _session_quality(fx, "USDJPYm", datetime(2026, 1, 5, 23, tzinfo=timezone.utc))[0] == "normal"
    assert _session_quality(AssetClass.CRYPTO, "BTCUSDm", datetime(2026, 1, 5, 3, tzinfo=timezone.utc))[0] == "normal"
    assert _session_quality(AssetClass.INDEX, "US500m", datetime(2026, 1, 5, 15, tzinfo=timezone.utc))[0] == "active"
    assert _session_quality(AssetClass.INDEX, "US500m", datetime(2026, 1, 5, 2, tzinfo=timezone.utc))[0] == "thin"


def test_session_weighting_lifts_confidence_in_liquid_window():
    t = _tech_ext("up", entry=104.0, ema20=100.0, atr_v=2.0)
    active = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(),
                                     now=datetime(2026, 1, 5, 13, tzinfo=timezone.utc))  # overlap
    thin = _deterministic_decision("X", AssetClass.FOREX, "1h", t, _fund(),
                                   now=datetime(2026, 1, 5, 23, tzinfo=timezone.utc))    # dead zone
    assert active.direction == Direction.LONG and thin.direction == Direction.LONG
    assert active.confidence > thin.confidence


# ---- analytics enhancements: divergence, institutional levels, key-level targets ----

def test_reference_levels_prior_day_and_week():
    from app.agents.indicators import reference_levels
    mon = datetime(2026, 6, 8, tzinfo=timezone.utc)  # ISO week 24, Monday
    days = [
        Candle(ts=mon, open=10, high=12, low=9, close=11, volume=1),                      # wk24 Mon
        Candle(ts=mon + timedelta(days=1), open=11, high=13, low=10, close=12, volume=1),  # wk24 Tue
        Candle(ts=mon + timedelta(days=7), open=12, high=15, low=11, close=14, volume=1),  # wk25 Mon (prior day)
        Candle(ts=mon + timedelta(days=8), open=14, high=16, low=13, close=15, volume=1),  # wk25 Tue (forming)
    ]
    r = reference_levels(days)
    assert r["prior_day_high"] == 15.0 and r["prior_day_low"] == 11.0   # the wk25 Monday bar
    assert r["prior_week_high"] == 13.0 and r["prior_week_low"] == 9.0   # the completed week 24


def test_divergence_detects_bearish():
    from app.agents.indicators import divergence
    closes = ([100.0] * 16
              + [104, 108, 112, 116, 120, 116, 112, 108]   # hump 1: sharp -> peak 120 @ idx20 (RSI~100)
              + [110, 113, 116, 119, 121, 118, 115, 112])  # hump 2: gentle -> higher peak 121 @ idx28 (RSI lower)
    candles = [Candle(ts=NOW + timedelta(hours=i), open=c, high=c + 0.2, low=c - 0.2, close=c, volume=1000)
               for i, c in enumerate(closes)]
    d = divergence(candles)
    assert d["bear"] is True  # higher price high, lower RSI high = bearish divergence


def test_divergence_against_extended_setup_watches():
    # Short with a stretched entry (100 well below EMA20 106) AND regular bullish divergence against
    # it -> exhaustion; the engine should WATCH, not enter.
    ind = {"last_close": 100.0, "atr14": 2.0, "adx": 30.0, "macd_hist": -1.0, "ema20": 106.0,
           "div_bull": 1.0}
    tech = TechnicalRead(symbol="X", overall_trend="down", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="down", indicators=ind,
                      support_levels=[80], resistance_levels=[110]),
        TimeframeRead(timeframe="1d", trend="down", indicators={}, support_levels=[], resistance_levels=[]),
    ])
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", tech, _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and p.watch and "divergence" in p.rationale.lower()


def test_target_snaps_to_prior_day_high():
    # Moderate long; the only nearby level is the institutional prior-day-high (104) on the daily TF.
    # risk = 1.5*ATR(1.5) = 2.25, so 104 is ~1.78R — above the 1.5R floor; the target snaps to it.
    ind = {"last_close": 100.0, "atr14": 1.5, "adx": 22.0, "macd_hist": 1.0}
    tech = TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators=ind,
                      support_levels=[80], resistance_levels=[130]),       # pivots far away
        TimeframeRead(timeframe="1d", trend="up", indicators={"prior_day_high": 104.0},
                      support_levels=[], resistance_levels=[]),
    ])
    p = _deterministic_decision("X", AssetClass.FOREX, "1h", tech, _fund(), now=NOW)
    assert p.direction == Direction.LONG and p.take_profit == 104.0  # capped at the institutional level
