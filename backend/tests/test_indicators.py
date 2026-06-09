"""Tests for the indicator bundle (ATR/ADX/MACD/Bollinger/EMA/volume) and the ADX chop gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agents.indicators import adx, atr, bollinger, ema, macd, market_structure, volume_ratio
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
    # Moderate trend (ADX 22): risk = 1.5*ATR(2) = 3; raw 2R target = 106; resistance 104 caps it.
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("up", "up", resistance=104.0, adx=22.0), _fund(), now=NOW)
    assert p.direction == Direction.LONG and p.take_profit == 104.0


def test_too_little_room_to_resistance_vetoes():
    # Moderate trend: resistance 102 is past the breakout buffer but < 1R (3) above entry -> sit out.
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("up", "up", resistance=102.0, adx=22.0), _fund(), now=NOW)
    assert p.direction == Direction.NO_TRADE and "room" in p.rationale.lower()


def test_strong_trend_ignores_immediate_structure():
    # Strong downtrend (ADX 35) with support only ~0.8R below entry: the XAUUSDm case.
    # Should still SHORT (breakout runner), not veto on "too little room".
    p = _deterministic_decision("X", AssetClass.FOREX, "1h",
                                _multi_tf("down", "down", support=97.6, adx=35.0), _fund(), now=NOW)
    assert p.direction == Direction.SHORT
    assert p.take_profit is not None and p.take_profit < p.entry


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
