"""RSI-Over strategy: RSI-extreme mean-reversion confirmed by EMA10 (overbought->short, oversold->
long), the deterministic stop/target, and the run_orchestrator routing + one-click scan."""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.rsi_over as scan_mod
from app.agents.orchestrator import _rsi_over_decision, run_orchestrator
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import FundamentalRead, RiskDecision, TechnicalRead, TimeframeRead, TradeProposal
from app.models.enums import RiskDecisionType

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _base() -> TradeProposal:
    return TradeProposal(symbol="X", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0)


def _ind(rsi, ema10, last, atr=2.0, rec_hi=None, rec_lo=None, cross=0.0, div_bull=0.0, div_bear=0.0,
         rdiv_bull=0.0, rdiv_bear=0.0, adx=None, rej_bull=0.0, rej_bear=0.0,
         fbreak_bull=0.0, fbreak_bear=0.0):
    return {"rsi14": rsi, "ema10": ema10, "last_close": last, "atr14": atr,
            "recent_high": rec_hi, "recent_low": rec_lo,
            "macd_cross": cross, "macd_div_bull": div_bull, "macd_div_bear": div_bear,
            "div_bull": rdiv_bull, "div_bear": rdiv_bear, "adx": adx,
            "rej_bull": rej_bull, "rej_bear": rej_bear,        # div_* = RSI divergence, rej_* = candle
            "fbreak_bull": fbreak_bull, "fbreak_bear": fbreak_bear}  # failed break / trap


class _TF0:
    """Minimal entry-TF read stub carrying just the S/R levels the at-level filter reads."""
    def __init__(self, support=None, resistance=None, timeframe="1h"):
        self.support_levels = support or []
        self.resistance_levels = resistance or []
        self.timeframe = timeframe


# ---- core decision ----

def test_overbought_and_closed_below_ema10_goes_short():
    # RSI 78 overbought + close 100 below EMA10 101 -> SHORT; stop beyond recent high, target 1.5R.
    ind = _ind(78, ema10=101, last=100, atr=2.0, rec_hi=103, rec_lo=95)
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.SHORT and p.strategy == "rsi_over"
    assert p.stop_loss == 103.4          # recent_high 103 + 0.2*ATR(2)
    assert p.take_profit == 94.9         # 100 - 1.5*(103.4-100)
    assert "overbought" in p.rationale and "EMA10 close" in p.rationale


def test_oversold_and_closed_above_ema10_goes_long():
    ind = _ind(22, ema10=99, last=100, atr=2.0, rec_hi=105, rec_lo=97)
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.LONG
    assert p.stop_loss == 96.6           # recent_low 97 - 0.2*ATR
    assert p.take_profit == 105.1        # 100 + 1.5*(100-96.6)


def test_overbought_but_not_confirmed_is_no_trade():
    # RSI overbought but price is still ABOVE EMA10 -> the turn hasn't confirmed -> wait.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, rec_lo=95)
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.NO_TRADE and "not confirmed" in p.rationale


def test_oversold_but_not_confirmed_is_no_trade():
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_hi=105, rec_lo=97)
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.NO_TRADE and "not confirmed" in p.rationale


def test_rsi_not_extreme_is_no_trade():
    ind = _ind(55, ema10=100, last=100, atr=2.0, rec_hi=105, rec_lo=95)
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.NO_TRADE and "not in an extreme zone" in p.rationale


def test_missing_data_is_no_trade():
    ind = {"rsi14": 80, "last_close": 100}  # no ema10
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.NO_TRADE and "not enough data" in p.rationale


def test_stop_falls_back_to_atr_when_swing_on_wrong_side():
    # Overbought short, but the recent high sits BELOW price (degenerate) -> stop can't be there;
    # falls back to 1.5*ATR above entry so the short still has a valid stop.
    ind = _ind(78, ema10=101, last=100, atr=2.0, rec_hi=99, rec_lo=95)
    p = _rsi_over_decision(_base(), ind, None, "X")
    assert p.direction == Direction.SHORT and p.stop_loss == 103.0  # 100 + 1.5*ATR(2)


# ---- confirm=False: fire on the RSI extreme alone ----

def test_confirm_off_fires_short_without_ema10_confirmation():
    # RSI overbought but price is ABOVE EMA10 (unconfirmed) -> with confirm off it still shorts.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, rec_lo=95)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False)
    assert p.direction == Direction.SHORT and "RSI extreme only" in p.rationale


def test_confirm_off_fires_long_without_ema10_confirmation():
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_hi=105, rec_lo=97)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False)
    assert p.direction == Direction.LONG and "RSI extreme only" in p.rationale


def test_confirm_off_needs_no_ema10():
    # No EMA10 at all -> confirm=False still trades on the RSI extreme; confirm=True would not.
    ind = {"rsi14": 80, "last_close": 100, "atr14": 2.0, "recent_high": 103}
    assert _rsi_over_decision(_base(), ind, None, "X", confirm=False).direction == Direction.SHORT
    assert _rsi_over_decision(_base(), ind, None, "X", confirm=True).direction == Direction.NO_TRADE


# ---- macd_signals indicator ----

def test_macd_signals_structure_and_ranges():
    from types import SimpleNamespace
    from app.agents.indicators import macd_signals
    closes = [100 + (i % 7) * 0.3 for i in range(80)]  # enough bars, mild oscillation
    candles = [SimpleNamespace(open=c, high=c + 0.2, low=c - 0.2, close=c, volume=100.0) for c in closes]
    out = macd_signals(candles)
    assert out is not None
    assert {"macd", "signal", "hist", "cross", "div_bull", "div_bear"} <= set(out)
    assert out["cross"] in (-1.0, 0.0, 1.0)
    assert out["div_bull"] in (0.0, 1.0) and out["div_bear"] in (0.0, 1.0)


def test_macd_signals_none_when_too_short():
    from types import SimpleNamespace
    from app.agents.indicators import macd_signals
    candles = [SimpleNamespace(open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0) for _ in range(10)]
    assert macd_signals(candles) is None


# ---- MACD early confirmation (macd=True) ----

def test_macd_cross_gives_early_short_before_ema10():
    # Overbought, price still ABOVE EMA10 (EMA10 not confirmed) but a bearish MACD cross -> early SHORT.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, cross=-1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, macd=True)
    assert p.direction == Direction.SHORT and "MACD cross" in p.rationale


def test_macd_divergence_alone_does_not_confirm():
    # MACD now requires a REAL signal-line cross — a MACD divergence alone (no cross) must NOT confirm,
    # so a trade never fires on "MACD" without a proper line intersection.
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_lo=97, div_bull=1.0)  # divergence only, no cross
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, macd=True)
    assert p.direction == Direction.NO_TRADE and "not confirmed" in p.rationale


def test_macd_wrong_direction_does_not_confirm():
    # Overbought but the MACD signal is BULLISH (cross up) -> not a short confirmation; EMA10 also not
    # met (price above) -> no trade.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, cross=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, macd=True)
    assert p.direction == Direction.NO_TRADE and "not confirmed" in p.rationale


def test_ema10_still_fires_when_macd_absent():
    # confirm+macd both on, no MACD signal, but EMA10 confirms -> still trades (OR semantics).
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)  # price below EMA10
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, macd=True)
    assert p.direction == Direction.SHORT and "EMA10 close" in p.rationale


# ---- #1 trend filter (don't fade against the higher-TF trend) ----

def test_trend_filter_blocks_fade_against_higher_tf_uptrend():
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)  # overbought -> would short
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False, trend_filter=True, macro="up")
    assert p.direction == Direction.NO_TRADE and "not fading against" in p.rationale.lower()


def test_trend_filter_allows_fade_in_range():
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False, trend_filter=True, macro="sideways")
    assert p.direction == Direction.SHORT


def test_trend_filter_off_allows_fade_against_trend():
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False, trend_filter=False, macro="up")
    assert p.direction == Direction.SHORT


def test_trend_filter_blocks_long_fade_against_downtrend():
    ind = _ind(20, ema10=99, last=100, atr=2.0, rec_lo=97)  # oversold -> would long
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False, trend_filter=True, macro="down")
    assert p.direction == Direction.NO_TRADE


def test_trend_filter_blocks_fade_into_strong_immediate_uptrend():
    # The BTC loss: MACRO sideways (old filter would allow) but the IMMEDIATE higher TF is UP and STRONG
    # (ADX 54) -> refuse the overbought short.
    ind = _ind(78, ema10=101, last=100, atr=2.0, rec_hi=103, adx=54.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False, trend_filter=True,
                           macro="sideways", htf="up", htf_name="1h")
    assert p.direction == Direction.NO_TRADE and "STRONG" in p.rationale and "1h" in p.rationale


def test_trend_filter_allows_fade_into_weak_immediate_uptrend():
    # Same setup but the immediate higher TF is only WEAKLY up (ADX 22 < 30) -> still fadeable (the
    # mean-reversion edge lives on weak/choppy higher-TF trends).
    ind = _ind(78, ema10=101, last=100, atr=2.0, rec_hi=103, adx=22.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=False, trend_filter=True,
                           macro="sideways", htf="up", htf_name="1h")
    assert p.direction == Direction.SHORT


# ---- #4 RSI divergence confirmation ----

def test_rsi_divergence_confirms_short():
    # Overbought, price still ABOVE EMA10 (EMA10 not confirmed), but a bearish RSI divergence -> SHORT.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, rdiv_bear=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rsi_div=True, macro="sideways")
    assert p.direction == Direction.SHORT and "RSI divergence" in p.rationale


def test_rsi_divergence_confirms_long():
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_lo=97, rdiv_bull=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rsi_div=True, macro="sideways")
    assert p.direction == Direction.LONG and "RSI divergence" in p.rationale


# ---- rejection-candle indicator ----

def test_rejection_candle_bearish_engulfing():
    from types import SimpleNamespace as C
    from app.agents.indicators import rejection_candle
    prev = C(open=100, high=101, low=99.5, close=100.8)      # green
    last = C(open=101, high=101.2, low=99.0, close=99.2)     # red body engulfs the prior green body
    out = rejection_candle([prev, last])
    assert out["rej_bear"] == 1.0 and out["rej_bull"] == 0.0


def test_rejection_candle_bullish_engulfing():
    from types import SimpleNamespace as C
    from app.agents.indicators import rejection_candle
    prev = C(open=100, high=100.5, low=99.0, close=99.2)     # red
    last = C(open=99.1, high=101.0, low=99.0, close=100.5)   # green body engulfs the prior red body
    out = rejection_candle([prev, last])
    assert out["rej_bull"] == 1.0 and out["rej_bear"] == 0.0


def test_rejection_candle_shooting_star():
    from types import SimpleNamespace as C
    from app.agents.indicators import rejection_candle
    prev = C(open=100, high=100, low=100, close=100)
    last = C(open=100.0, high=103.0, low=99.8, close=100.1)  # long UPPER wick, closes low -> bearish pin
    assert rejection_candle([prev, last])["rej_bear"] == 1.0


def test_rejection_candle_hammer():
    from types import SimpleNamespace as C
    from app.agents.indicators import rejection_candle
    prev = C(open=100, high=100, low=100, close=100)
    last = C(open=100.0, high=100.2, low=97.0, close=99.9)   # long LOWER wick, closes high -> bullish pin
    assert rejection_candle([prev, last])["rej_bull"] == 1.0


def test_rejection_candle_none_for_plain_bar():
    from types import SimpleNamespace as C
    from app.agents.indicators import rejection_candle
    prev = C(open=100, high=100.5, low=99.5, close=100.2)
    last = C(open=100.2, high=100.8, low=100.0, close=100.6)  # ordinary bar, no dominant wick, no engulf
    out = rejection_candle([prev, last])
    assert out["rej_bear"] == 0.0 and out["rej_bull"] == 0.0


def test_rejection_candle_too_few_bars():
    from types import SimpleNamespace as C
    from app.agents.indicators import rejection_candle
    assert rejection_candle([C(open=1, high=1, low=1, close=1)]) == {"rej_bull": 0.0, "rej_bear": 0.0}


# ---- failed-break / trap indicator ----

def test_failed_break_bearish_trap():
    from types import SimpleNamespace as C
    from app.agents.indicators import failed_break
    prior = [C(open=100, high=101, low=99, close=100) for _ in range(20)]      # prior high ~101
    recent = [C(open=100, high=103, low=99.5, close=102),                      # poked above 101
              C(open=102, high=102.5, low=100, close=100.5),
              C(open=100.5, high=101, low=99.8, close=100.2),
              C(open=100.2, high=100.4, low=99.5, close=100.0)]                # closed back below 101
    out = failed_break(prior + recent)
    assert out["fbreak_bear"] == 1.0 and out["fbreak_bull"] == 0.0


def test_failed_break_bullish_trap():
    from types import SimpleNamespace as C
    from app.agents.indicators import failed_break
    prior = [C(open=100, high=101, low=99, close=100) for _ in range(20)]      # prior low ~99
    recent = [C(open=100, high=100.2, low=97, close=98),                       # poked below 99
              C(open=98, high=99.5, low=97.5, close=99.2),
              C(open=99.2, high=100, low=98.5, close=99.6),
              C(open=99.6, high=100.5, low=99.4, close=100.0)]                 # closed back above 99
    out = failed_break(prior + recent)
    assert out["fbreak_bull"] == 1.0 and out["fbreak_bear"] == 0.0


def test_failed_break_none_for_clean_range():
    from types import SimpleNamespace as C
    from app.agents.indicators import failed_break
    bars = [C(open=100, high=100.5, low=99.5, close=100) for _ in range(24)]   # never pierces
    out = failed_break(bars)
    assert out["fbreak_bull"] == 0.0 and out["fbreak_bear"] == 0.0


def test_failed_break_too_few_bars():
    from types import SimpleNamespace as C
    from app.agents.indicators import failed_break
    assert failed_break([C(open=1, high=1, low=1, close=1) for _ in range(3)]) == \
        {"fbreak_bull": 0.0, "fbreak_bear": 0.0}


def test_failed_break_confirms_short_in_rsi_over():
    # Overbought, price ABOVE EMA10 (not confirmed), no rejection candle, but a failed upside break -> SHORT.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, fbreak_bear=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rej_candle=True)
    assert p.direction == Direction.SHORT and "rejection" in p.rationale


def test_failed_break_confirms_long_in_rsi_over():
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_lo=97, fbreak_bull=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rej_candle=True)
    assert p.direction == Direction.LONG and "rejection" in p.rationale


# ---- rejection-candle confirmation (rej_candle=True) ----

def test_rejection_candle_confirms_short():
    # Overbought, price ABOVE EMA10 (EMA not confirmed), no MACD/div, but a bearish rejection candle -> SHORT.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, rej_bear=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rej_candle=True)
    assert p.direction == Direction.SHORT and "rejection" in p.rationale


def test_rejection_candle_confirms_long():
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_lo=97, rej_bull=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rej_candle=True)
    assert p.direction == Direction.LONG and "rejection" in p.rationale


def test_rejection_candle_wrong_side_does_not_confirm():
    # Overbought but only a BULLISH rejection candle -> not a short confirm; EMA also not met -> no trade.
    ind = _ind(80, ema10=101, last=102, atr=2.0, rec_hi=103, rej_bull=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, rej_candle=True)
    assert p.direction == Direction.NO_TRADE and "not confirmed" in p.rationale


# ---- at-level location filter ----

def test_at_level_allows_fade_near_resistance():
    # Overbought short, EMA10 confirmed, resistance 100.5 within 0.6*ATR(2)=1.2 of price 100 -> allowed.
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[100.5]), "X", confirm=True, at_level=True)
    assert p.direction == Direction.SHORT


def test_at_level_blocks_fade_in_open_space():
    # Nearest resistance is far (110, > 1.2 from 100) -> the fade-at-level filter stands aside.
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[110.0]), "X", confirm=True, at_level=True)
    assert p.direction == Direction.NO_TRADE and "at a key resistance" in p.rationale


def test_at_level_off_ignores_location():
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[110.0]), "X", confirm=True, at_level=False)
    assert p.direction == Direction.SHORT  # location ignored when the filter is off


def test_at_level_long_near_support():
    ind = _ind(20, ema10=99, last=100, atr=2.0, rec_lo=97)
    p = _rsi_over_decision(_base(), ind, _TF0(support=[99.5]), "X", confirm=True, at_level=True)
    assert p.direction == Direction.LONG


# ---- AI price-action level check (pa_confirm): fade only if the level is HOLDING, not GIVING WAY ----

def _fake_pa(category, confidence=0.8):
    from app.agents.priceaction_read import PriceActionRead
    return PriceActionRead(category=category, evidence="mom fading, RSI turning", confidence=confidence)


def test_pa_confirm_off_never_calls_the_classifier(monkeypatch):
    # With pa_confirm off, the AI level check must NOT run (no LLM call) and the fade proceeds as usual.
    import app.agents.priceaction_read as pa_mod

    def _boom(*a, **k):
        raise AssertionError("interpret_price_action called with pa_confirm off")

    monkeypatch.setattr(pa_mod, "interpret_price_action", _boom)
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[100.5]), "X", confirm=True, pa_confirm=False)
    assert p.direction == Direction.SHORT


def test_pa_confirm_breaking_level_stands_aside(monkeypatch):
    # Overbought short with a resistance in reach, but the AI reads the level as BREAKING -> stand aside.
    import app.agents.priceaction_read as pa_mod
    monkeypatch.setattr(pa_mod, "interpret_price_action", lambda *a, **k: _fake_pa("likely_break", 0.7))
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[100.5]), "X", confirm=True, pa_confirm=True)
    assert p.direction == Direction.NO_TRADE and "BREAKING" in p.rationale and "likely_break" in p.rationale


def test_pa_confirm_rejecting_level_allows_the_fade(monkeypatch):
    # Same setup, but the AI reads the level as HOLDING (reject) -> the fade proceeds.
    import app.agents.priceaction_read as pa_mod
    monkeypatch.setattr(pa_mod, "interpret_price_action", lambda *a, **k: _fake_pa("likely_reject", 0.8))
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[100.5]), "X", confirm=True, pa_confirm=True)
    assert p.direction == Direction.SHORT


def test_pa_confirm_low_confidence_break_fails_open(monkeypatch):
    # A "likely_break" below the trust threshold (0.55) is not acted on -> the fade still stands.
    import app.agents.priceaction_read as pa_mod
    monkeypatch.setattr(pa_mod, "interpret_price_action", lambda *a, **k: _fake_pa("likely_break", 0.40))
    ind = _ind(80, ema10=101, last=100, atr=2.0, rec_hi=103)
    p = _rsi_over_decision(_base(), ind, _TF0(resistance=[100.5]), "X", confirm=True, pa_confirm=True)
    assert p.direction == Direction.SHORT


def test_pa_confirm_no_llm_fails_open(monkeypatch):
    # No LLM configured -> interpret_price_action returns None -> the fade proceeds (fails open).
    import app.agents.priceaction_read as pa_mod
    monkeypatch.setattr(pa_mod, "interpret_price_action", lambda *a, **k: None)
    ind = _ind(20, ema10=99, last=100, atr=2.0, rec_lo=97)
    p = _rsi_over_decision(_base(), ind, _TF0(support=[99.5]), "X", confirm=True, pa_confirm=True)
    assert p.direction == Direction.LONG


# ---- routing through the orchestrator (mechanical, no LLM) ----

def _tech(ind):
    clean = {k: v for k, v in ind.items() if v is not None}  # real reads omit None-valued indicators
    return TechnicalRead(symbol="X", overall_trend="down", confidence=0.5,
                         timeframes=[TimeframeRead(timeframe="1h", trend="down", indicators=clean)])


def test_run_orchestrator_routes_to_rsi_over():
    ind = _ind(78, ema10=101, last=100, atr=2.0, rec_hi=103, rec_lo=95)
    p = run_orchestrator("X", AssetClass.FOREX, "1h", _tech(ind),
                         FundamentalRead(symbol="X", bias=TradingBias.NEUTRAL),
                         now=NOW, use_llm=False, rsi_over=True)
    assert p.strategy == "rsi_over" and p.direction == Direction.SHORT


# ---- the one-click scan: stop at the first tradeable signal ----

class _WI:
    def __init__(self, symbol, ac="forex", tf="1h"):
        self.symbol, self.asset_class, self.timeframe, self.enabled = symbol, ac, tf, True


def _approved(ok=True):
    return RiskDecision(decision=RiskDecisionType.APPROVED if ok else RiskDecisionType.VETOED,
                        approved=ok, reason="ok" if ok else "exposure full", symbol="X",
                        approved_qty=1.0 if ok else 0.0, risk_amount=10.0 if ok else 0.0)


def test_scan_stages_first_tradeable_signal(db_session, monkeypatch):
    # Universe (across asset classes) = AAA (no signal) then BBB (confirmed short). Sweep stops at BBB.
    monkeypatch.setattr(scan_mod, "_universe", lambda s: [("AAA", "forex"), ("BBB", "metal")])
    monkeypatch.setattr(scan_mod, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)

    def fake_preview(session, symbol, ac, tf, **kw):
        if symbol == "BBB":
            prop = TradeProposal(symbol="BBB", asset_class=ac, timeframe=tf, direction=Direction.SHORT,
                                 confidence=0.7, technical=_tech(_ind(80, 101, 100, rec_hi=103)))
            return prop, _approved(True)
        return TradeProposal(symbol="AAA", asset_class=ac, timeframe=tf, direction=Direction.NO_TRADE,
                             confidence=0.0), _approved(False)

    class _Res:
        proposal_id, status = 42, "pending_approval"
        risk = type("R", (), {"approved": True})()

    monkeypatch.setattr(scan_mod, "preview_symbol", fake_preview)
    monkeypatch.setattr(scan_mod, "analyze_symbol", lambda *a, **k: _Res())

    out = scan_mod.run_rsi_over_scan(db_session, "1h")
    assert out["found"] and out["found"]["symbol"] == "BBB" and out["found"]["direction"] == "short"
    assert out["found"]["proposal_id"] == 42 and out["scanned"] == 2 and out["signals"] == 1


# ---- auto-watch tick (timer that re-runs the sweep) ----

def test_auto_watch_tick_noop_when_disabled(db_session):
    assert scan_mod.rsi_over_tick(db_session) == {"ran": False, "reason": "disabled"}


def test_scan_persists_snapshot_to_config(db_session, monkeypatch):
    # A sweep with no tradeable pair must persist its reason + candidates on the config so the panel
    # can restore them after a refresh.
    import json
    monkeypatch.setattr(scan_mod, "_universe", lambda s: [("AAA", "forex")])
    monkeypatch.setattr(scan_mod, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)
    prop = TradeProposal(symbol="AAA", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0, technical=_tech(_ind(76, 100, 99, rec_hi=101)))
    monkeypatch.setattr(scan_mod, "preview_symbol", lambda *a, **k: (prop, _approved(False)))
    scan_mod.run_rsi_over_scan(db_session, "1h")
    cfg = scan_mod.get_or_create_rsi_over_config(db_session)
    assert cfg.last_candidates is not None and cfg.last_scanned == 1
    saved = json.loads(cfg.last_candidates)
    assert saved["overbought"][0]["symbol"] == "AAA"


def test_auto_watch_tick_runs_and_records_when_enabled(db_session, monkeypatch):
    cfg = scan_mod.get_or_create_rsi_over_config(db_session)
    cfg.enabled = True
    db_session.commit()
    monkeypatch.setattr(scan_mod, "run_rsi_over_scan",
                        lambda s, tf, confirm=True, **kw: {"ran": True, "reason": f"tf={tf} c={confirm}", "found": None})
    out = scan_mod.rsi_over_tick(db_session)
    assert out["ran"] and out["reason"] == "tf=1h c=True"
    db_session.refresh(cfg)
    assert cfg.last_run_at is not None  # last_result/candidates are persisted by the sweep's done(), tested separately


def test_auto_watch_skips_sweep_when_book_full(db_session, monkeypatch):
    # ROOM gate: with max_open_positions already open, the auto-watch (skip_if_full=True) must NOT sweep.
    from types import SimpleNamespace
    from app.core.state import get_or_create_risk_config

    rc = get_or_create_risk_config(db_session)
    rc.max_open_positions = 3
    db_session.commit()
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(scan_mod, "live_broker_positions",
                        lambda s: [SimpleNamespace(symbol=f"P{i}", direction="long") for i in range(3)])

    def _boom(s):
        raise AssertionError("swept the universe despite a full book")

    monkeypatch.setattr(scan_mod, "_universe", _boom)  # proves the sweep is skipped
    out = scan_mod.run_rsi_over_scan(db_session, "1h", skip_if_full=True)
    assert out["found"] is None and "no room" in out["reason"]


def test_manual_scan_runs_even_when_book_full(db_session, monkeypatch):
    # The manual one-click scan (skip_if_full default False) still sweeps when full — it's user-initiated.
    from types import SimpleNamespace
    from app.core.state import get_or_create_risk_config

    rc = get_or_create_risk_config(db_session)
    rc.max_open_positions = 3
    db_session.commit()
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(scan_mod, "live_broker_positions",
                        lambda s: [SimpleNamespace(symbol=f"P{i}", direction="long") for i in range(3)])
    monkeypatch.setattr(scan_mod, "_universe", lambda s: [("AAA", "forex")])
    prop = TradeProposal(symbol="AAA", asset_class=AssetClass.FOREX, timeframe="1h",
                         direction=Direction.NO_TRADE, confidence=0.0,
                         technical=_tech(_ind(76, 100, 99, rec_hi=101)))
    monkeypatch.setattr(scan_mod, "preview_symbol", lambda *a, **k: (prop, _approved(False)))
    out = scan_mod.run_rsi_over_scan(db_session, "1h")  # skip_if_full defaults False
    assert out["scanned"] == 1  # it DID sweep


def test_auto_watch_tick_skips_within_interval(db_session, monkeypatch):
    from datetime import datetime, timezone
    cfg = scan_mod.get_or_create_rsi_over_config(db_session)
    cfg.enabled = True
    cfg.last_run_at = datetime.now(timezone.utc)  # just ran
    db_session.commit()
    calls = {"n": 0}
    monkeypatch.setattr(scan_mod, "run_rsi_over_scan",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {})
    assert scan_mod.rsi_over_tick(db_session) == {"ran": False, "reason": "interval not elapsed"}
    assert calls["n"] == 0


def test_auto_approve_executes_the_found_proposal(db_session, monkeypatch):
    # With auto_approve, a found (pending) proposal is executed directly instead of left pending.
    from app.models.db import TradeProposalRecord
    from app.models.enums import ProposalStatus
    import app.execution.executor as executor

    # Start APPROVED (not pending) so the scan's anti-stacking pending-symbol filter doesn't skip BBB;
    # analyze_symbol (mocked) is what "creates" the pending proposal the auto-approve branch executes.
    rec = TradeProposalRecord(symbol="BBB", asset_class="forex", timeframe="1h", direction="short",
                              entry=100.0, stop_loss=103.0, take_profit=95.0, confidence=0.7,
                              rationale="x", reasoning={}, status=ProposalStatus.APPROVED.value)
    db_session.add(rec)
    db_session.commit()

    monkeypatch.setattr(scan_mod, "_universe", lambda s: [("BBB", "forex")])
    monkeypatch.setattr(scan_mod, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)
    prop = TradeProposal(symbol="BBB", asset_class=AssetClass.FOREX, timeframe="1h", direction=Direction.SHORT,
                         confidence=0.7, technical=_tech(_ind(80, 101, 100, rec_hi=103)))
    monkeypatch.setattr(scan_mod, "preview_symbol", lambda *a, **k: (prop, _approved(True)))

    class _Res:
        proposal_id = rec.id
        status = ProposalStatus.PENDING_APPROVAL.value
        risk = type("R", (), {"approved": True})()
    monkeypatch.setattr(scan_mod, "analyze_symbol", lambda *a, **k: _Res())

    calls = {"n": 0}
    def fake_exec(session, record):
        calls["n"] += 1
        record.status = ProposalStatus.EXECUTED.value  # simulate a fill
        session.add(record)
        session.commit()
    monkeypatch.setattr(executor, "execute_proposal", fake_exec)

    out = scan_mod.run_rsi_over_scan(db_session, "1h", confirm=False, auto_approve=True)
    assert calls["n"] == 1
    assert out["found"]["status"] == ProposalStatus.EXECUTED.value and "opened" in out["reason"]


def test_auto_approve_blocked_expires_the_proposal(db_session, monkeypatch):
    # If execution is blocked (e.g. the drift guard), auto-approve EXPIRES the stale proposal instead
    # of leaving it in Pending — so it doesn't pile up waiting for a manual click.
    from app.models.db import TradeProposalRecord
    from app.models.enums import ProposalStatus
    from app.execution.executor import ExecutionBlocked
    import app.execution.executor as executor

    rec = TradeProposalRecord(symbol="BBB", asset_class="forex", timeframe="1h", direction="short",
                              entry=100.0, stop_loss=103.0, take_profit=95.0, confidence=0.7,
                              rationale="x", reasoning={}, status=ProposalStatus.APPROVED.value)
    db_session.add(rec)
    db_session.commit()

    monkeypatch.setattr(scan_mod, "_universe", lambda s: [("BBB", "forex")])
    monkeypatch.setattr(scan_mod, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)
    prop = TradeProposal(symbol="BBB", asset_class=AssetClass.FOREX, timeframe="1h", direction=Direction.SHORT,
                         confidence=0.7, technical=_tech(_ind(80, 101, 100, rec_hi=103)))
    monkeypatch.setattr(scan_mod, "preview_symbol", lambda *a, **k: (prop, _approved(True)))

    class _Res:
        proposal_id = rec.id
        status = ProposalStatus.PENDING_APPROVAL.value
        risk = type("R", (), {"approved": True})()
    monkeypatch.setattr(scan_mod, "analyze_symbol", lambda *a, **k: _Res())

    def blocked_exec(session, record):
        raise ExecutionBlocked("price moved — R:R at the real fill is 0.6")
    monkeypatch.setattr(executor, "execute_proposal", blocked_exec)

    out = scan_mod.run_rsi_over_scan(db_session, "1h", confirm=False, auto_approve=True)
    db_session.refresh(rec)
    assert rec.status == ProposalStatus.EXPIRED.value
    assert out["found"]["status"] == ProposalStatus.EXPIRED.value and "expired" in out["reason"]


def test_scan_reports_when_signals_all_risk_blocked(db_session, monkeypatch):
    monkeypatch.setattr(scan_mod, "_universe", lambda s: [("AAA", "forex")])
    monkeypatch.setattr(scan_mod, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(scan_mod, "kill_switch_active", lambda s: False)

    def fake_preview(session, symbol, ac, tf, **kw):
        prop = TradeProposal(symbol=symbol, asset_class=ac, timeframe=tf, direction=Direction.SHORT,
                             confidence=0.7, technical=_tech(_ind(80, 101, 100, rec_hi=103)))
        return prop, _approved(False)  # a real signal, but risk-blocked

    monkeypatch.setattr(scan_mod, "preview_symbol", fake_preview)
    out = scan_mod.run_rsi_over_scan(db_session, "1h")
    assert out["found"] is None and out["signals"] == 1 and "blocked" in out["reason"]
