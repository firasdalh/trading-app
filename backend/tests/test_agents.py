"""Tests for the deterministic agent path (no LLM key needed).

Covers indicators, the technical read, orchestrator confluence/stand-aside logic, and the
end-to-end pipeline persisting + risk-gating a proposal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.indicators import rsi, sma, swing_levels, trend_from_smas
from app.agents.orchestrator import run_orchestrator
from app.agents.technical import run_technical
from app.data.market import SyntheticDataProvider
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import (
    Candle,
    EventWindow,
    FundamentalRead,
    OHLCVSeries,
    TechnicalRead,
    TimeframeRead,
)

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def _uptrend_series(symbol="TEST", n=80, start=100.0, step=0.5) -> OHLCVSeries:
    candles = []
    price = start
    for i in range(n):
        o = price
        price += step
        candles.append(Candle(ts=NOW - timedelta(hours=n - i), open=o, high=price + 0.2,
                              low=o - 0.2, close=price, volume=1000))
    return OHLCVSeries(symbol=symbol, timeframe="1h", candles=candles)


def _downtrend_series(symbol="TEST", n=80, start=100.0, step=0.5) -> OHLCVSeries:
    candles = []
    price = start
    for i in range(n):
        o = price
        price -= step
        candles.append(Candle(ts=NOW - timedelta(hours=n - i), open=o, high=o + 0.2,
                              low=price - 0.2, close=price, volume=1000))
    return OHLCVSeries(symbol=symbol, timeframe="1h", candles=candles)


# ---- indicators ----

def test_sma_and_rsi_and_swings():
    closes = [float(i) for i in range(1, 31)]
    assert sma(closes, 10) == pytest.approx(25.5)
    assert rsi(closes) == 100.0  # only gains
    candles = _uptrend_series().candles
    support, resistance = swing_levels(candles, lookback=20)
    assert support is not None and resistance is not None and resistance > support


def test_trend_classification():
    up = [c.close for c in _uptrend_series().candles]
    down = [c.close for c in _downtrend_series().candles]
    assert trend_from_smas(up) == "up"
    assert trend_from_smas(down) == "down"


# ---- technical agent (deterministic) ----

def test_technical_read_uptrend():
    read = run_technical("TEST", [_uptrend_series()])
    assert read.overall_trend == "up"
    assert read.timeframes[0].indicators.get("last_close") is not None
    assert 0 <= read.confidence <= 1


# ---- orchestrator ----

def _neutral_fundamental(symbol="TEST", windows=None) -> FundamentalRead:
    return FundamentalRead(symbol=symbol, bias=TradingBias.NEUTRAL, stand_aside_windows=windows or [])


def test_orchestrator_long_on_uptrend_confluence():
    tech = run_technical("TEST", [_uptrend_series()])
    prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), now=NOW)
    assert prop.direction == Direction.LONG
    assert prop.entry and prop.stop_loss and prop.stop_loss < prop.entry
    assert prop.take_profit and prop.take_profit > prop.entry


def test_orchestrator_short_on_downtrend():
    tech = run_technical("TEST", [_downtrend_series()])
    prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), now=NOW)
    assert prop.direction == Direction.SHORT
    assert prop.stop_loss and prop.stop_loss > prop.entry


def test_orchestrator_arms_pullback_when_overbought_and_not_strong():
    # A moderate (not strong) uptrend already overbought -> don't chase at market; arm the pullback.
    from app.agents.orchestrator import _deterministic_decision
    tech = run_technical("TEST", [_uptrend_series()])
    ind = tech.timeframes[0].indicators
    ind["adx"] = 22.0        # moderate trend (< strong 25) -> stays in the trend path
    ind["rsi14"] = 72.0      # overbought zone (>= 70)
    ind["macd_hist"] = 0.05  # momentum not against (avoid the separate momentum-pullback branch)
    prop = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW)
    assert prop.direction == Direction.NO_TRADE and prop.watch is True
    assert "overbought" in prop.rationale.lower() and "pullback" in prop.rationale.lower()


def test_orchestrator_arms_pullback_when_oversold_short_and_not_strong():
    from app.agents.orchestrator import _deterministic_decision
    tech = run_technical("TEST", [_downtrend_series()])
    ind = tech.timeframes[0].indicators
    ind["adx"] = 22.0
    ind["rsi14"] = 28.0      # oversold zone (<= 30)
    ind["macd_hist"] = -0.05
    prop = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW)
    assert prop.direction == Direction.NO_TRADE and prop.watch is True
    assert "oversold" in prop.rationale.lower()


def test_disable_rsi_extreme_filter_takes_market_instead_of_arming():
    # Toggling a checklist filter OFF changes the LIVE deterministic decision: with rsi_extreme ON
    # (default) an overbought moderate trend arms the pullback; with it OFF it takes the market entry.
    from app.agents.orchestrator import _deterministic_decision
    tech = run_technical("TEST", [_uptrend_series()])
    ind = tech.timeframes[0].indicators
    ind["adx"] = 22.0        # moderate (not strong) -> the rsi_extreme arm path applies
    ind["rsi14"] = 72.0
    ind["macd_hist"] = 0.05
    armed = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW)
    assert armed.direction == Direction.NO_TRADE and armed.watch is True   # default: arms
    took = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW,
                                   disable=frozenset({"rsi_extreme"}))
    assert took.direction == Direction.LONG                                # filter off: takes market


def test_macd_histogram_rising_lifts_confidence_vs_fading():
    # The new "MACD histogram rising" filter: an expanding histogram (momentum building) confers more
    # confidence than a fading one; toggling it off removes the difference.
    from app.agents.orchestrator import _deterministic_decision

    def prop(hist, hist_prev, disable=frozenset()):
        tech = run_technical("TEST", [_uptrend_series()])
        ind = tech.timeframes[0].indicators
        ind["adx"] = 22.0        # moderate -> market entry, confidence not maxed at the cap
        ind["rsi14"] = 55.0      # not extreme -> no pullback arm
        ind["macd_hist"] = hist
        ind["macd_hist_prev"] = hist_prev
        return _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW,
                                       disable=disable)

    rising, fading = prop(0.5, 0.2), prop(0.2, 0.5)
    assert rising.direction == Direction.LONG and fading.direction == Direction.LONG
    assert rising.confidence > fading.confidence
    off_r = prop(0.5, 0.2, disable=frozenset({"macd_rising"}))
    off_f = prop(0.2, 0.5, disable=frozenset({"macd_rising"}))
    assert off_r.confidence == off_f.confidence   # filter off -> histogram slope no longer scored


def test_ema200_filter_gates_its_confidence_factor():
    # A newly-exposed filter: with ema200 ON, being on the right side of the 200-EMA adds confidence;
    # disabling the filter removes that contribution entirely.
    from app.agents.orchestrator import _deterministic_decision
    tech = run_technical("TEST", [_uptrend_series()])
    ind = tech.timeframes[0].indicators
    ind["adx"] = 22.0        # moderate -> market entry, confidence not clamped
    ind["rsi14"] = 55.0
    ind["ema200"] = 90.0     # entry (~140) is above EMA200 -> with the long-term trend -> +0.05
    on = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW)
    off = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW,
                                  disable=frozenset({"ema200"}))
    assert on.direction == Direction.LONG and off.direction == Direction.LONG
    assert on.confidence > off.confidence     # the EMA200 bonus is dropped when the filter is off


def test_adx_filter_maps_to_trend_only_mode(db_session):
    # The "adx" panel filter is a proxy for trend_only_mode (the existing ADX-strength gate).
    from app.api.settings_routes import DetFiltersRequest, get_det_filters, set_det_filters
    from app.core.state import get_or_create_settings

    assert "adx" not in get_det_filters(session=db_session).disabled       # trend-only on by default
    out = set_det_filters(DetFiltersRequest(disabled=["adx"]), session=db_session)
    assert "adx" in out.disabled and get_or_create_settings(db_session).trend_only_mode is False
    out2 = set_det_filters(DetFiltersRequest(disabled=[]), session=db_session)
    assert "adx" not in out2.disabled and get_or_create_settings(db_session).trend_only_mode is True


def test_det_filters_endpoint_persists_and_validates(db_session):
    from app.agents.orchestrator import DET_FILTER_KEYS
    from app.api.settings_routes import DetFiltersRequest, get_det_filters, set_det_filters
    from app.core.state import get_or_create_settings

    view = get_det_filters(session=db_session)
    assert len(view.filters) >= 8 and view.disabled == []
    assert all(f.key in DET_FILTER_KEYS for f in view.filters)
    out = set_det_filters(DetFiltersRequest(disabled=["mtf", "chase", "bogus"]), session=db_session)
    assert set(out.disabled) == {"mtf", "chase"}                           # "bogus" validated out
    assert set(get_or_create_settings(db_session).disabled_filters) == {"mtf", "chase"}


def test_orchestrator_rides_overbought_when_strong_trend():
    # A STRONG trend (ADX >= 25) is allowed to ride an overbought RSI and still enter at market.
    from app.agents.orchestrator import _deterministic_decision
    tech = run_technical("TEST", [_uptrend_series()])
    ind = tech.timeframes[0].indicators
    ind["adx"] = 35.0        # strong trend
    ind["rsi14"] = 80.0      # deep overbought
    prop = _deterministic_decision("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), NOW)
    assert prop.direction == Direction.LONG  # strong trend -> market entry, not armed


def test_fundamental_bias_nudges_confidence_not_vetoes():
    # An opposing fundamental bias is a soft macro lean now — it must NOT veto a clean technical
    # trend, only lower confidence. (Trend decides direction; bias is a confidence factor.)
    tech = run_technical("TEST", [_uptrend_series()])  # up
    bearish = FundamentalRead(symbol="TEST", bias=TradingBias.BEARISH)
    neutral_prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(), now=NOW)
    bearish_prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, bearish, now=NOW)
    assert bearish_prop.direction == Direction.LONG          # not vetoed
    assert bearish_prop.confidence < neutral_prop.confidence  # only down-weighted


def test_orchestrator_stands_aside_in_event_window():
    tech = run_technical("TEST", [_uptrend_series()])
    window = EventWindow(label="CPI", start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=30))
    fund = _neutral_fundamental(windows=[window])
    prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, fund, now=NOW)
    assert prop.direction == Direction.NO_TRADE
    assert "event window" in prop.rationale.lower()


def test_llm_review_can_veto(monkeypatch):
    from app.agents import orchestrator
    from app.models.enums import ReviewDecision
    from app.models.schemas import TradeReviewLLM

    tech = run_technical("TEST", [_uptrend_series()])  # deterministic -> LONG
    monkeypatch.setattr(orchestrator, "llm_available", lambda: True)
    monkeypatch.setattr(orchestrator, "analyze",
                        lambda **k: TradeReviewLLM(decision=ReviewDecision.VETO, confidence=0.2,
                                                   rationale="higher-timeframe conflict"))
    p = orchestrator.run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(),
                                      now=NOW, use_llm=True)
    assert p.direction == Direction.NO_TRADE and "veto" in p.rationale.lower()
    assert p.entry is None and p.stop_loss is None


def test_llm_review_confirm_only_lowers_confidence(monkeypatch):
    from app.agents import orchestrator
    from app.models.enums import ReviewDecision
    from app.models.schemas import TradeReviewLLM

    tech = run_technical("TEST", [_uptrend_series()])
    det = orchestrator._deterministic_decision("TEST", AssetClass.STOCK, "1h", tech,
                                               _neutral_fundamental(), NOW)
    assert det.direction == Direction.LONG
    monkeypatch.setattr(orchestrator, "llm_available", lambda: True)
    # Reviewer tries to set a HIGH confidence; we must cap to the deterministic value.
    monkeypatch.setattr(orchestrator, "analyze",
                        lambda **k: TradeReviewLLM(decision=ReviewDecision.CONFIRM, confidence=0.99,
                                                   rationale="looks good"))
    p = orchestrator.run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, _neutral_fundamental(),
                                      now=NOW, use_llm=True)
    assert p.direction == Direction.LONG
    assert p.confidence <= det.confidence  # LLM can only narrow, never raise
    # levels unchanged (LLM cannot move them)
    assert p.entry == det.entry and p.stop_loss == det.stop_loss


def test_llm_cannot_create_trade_when_rules_decline(monkeypatch):
    from app.agents import orchestrator
    from app.models.enums import ReviewDecision
    from app.models.schemas import TradeReviewLLM

    tech = run_technical("TEST", [_uptrend_series()])  # up
    # Force a deterministic stand-aside via an imminent high-impact event window (bias no longer
    # vetoes, so use a rule that still declines).
    window = EventWindow(label="CPI", start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=30))
    fund = _neutral_fundamental(windows=[window])
    called = []
    monkeypatch.setattr(orchestrator, "llm_available", lambda: True)
    monkeypatch.setattr(orchestrator, "analyze",
                        lambda **k: called.append(1) or TradeReviewLLM(decision=ReviewDecision.CONFIRM, confidence=0.9))
    p = orchestrator.run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, fund, now=NOW, use_llm=True)
    assert p.direction == Direction.NO_TRADE
    assert called == []  # the LLM is never even consulted when the rules decline


def test_use_llm_flag_gates_llm_calls(monkeypatch):
    from app.agents import technical
    calls = []
    monkeypatch.setattr(technical, "llm_available", lambda: True)
    monkeypatch.setattr(technical, "analyze", lambda **kw: calls.append(kw.get("schema")) or None)

    series = [_uptrend_series()]
    technical.run_technical("X", series, use_llm=False)
    assert calls == []  # scanner path: LLM never called

    technical.run_technical("X", series, use_llm=True)
    assert len(calls) == 1  # manual path: LLM attempted once


# ---- full pipeline (DB + risk); db_session fixture is in conftest.py ----

def test_pipeline_persists_and_risk_gates(db_session):
    from app.agents.pipeline import analyze_symbol

    res = analyze_symbol(db_session, "AAPL", AssetClass.STOCK, "1h")
    assert res.proposal_id > 0
    # Either a risk-approved proposal awaiting approval, or a veto/no-trade — both valid.
    assert res.status in ("pending_approval", "risk_vetoed")
    if res.risk.approved:
        assert res.status == "pending_approval"
        assert res.risk.approved_qty > 0
    else:
        assert res.status == "risk_vetoed"
