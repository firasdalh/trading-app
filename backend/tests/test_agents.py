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


def test_orchestrator_no_trade_on_conflict():
    tech = run_technical("TEST", [_uptrend_series()])  # up
    bearish = FundamentalRead(symbol="TEST", bias=TradingBias.BEARISH)
    prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, bearish, now=NOW)
    assert prop.direction == Direction.NO_TRADE


def test_orchestrator_stands_aside_in_event_window():
    tech = run_technical("TEST", [_uptrend_series()])
    window = EventWindow(label="CPI", start=NOW - timedelta(minutes=5), end=NOW + timedelta(minutes=30))
    fund = _neutral_fundamental(windows=[window])
    prop = run_orchestrator("TEST", AssetClass.STOCK, "1h", tech, fund, now=NOW)
    assert prop.direction == Direction.NO_TRADE
    assert "event window" in prop.rationale.lower()


# ---- full pipeline (DB + risk) ----

@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """Fresh SQLite DB per test, with deterministic synthetic data."""
    import importlib

    db_url = f"sqlite:///{tmp_path/'test.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # force deterministic agents

    from app.core import config
    config.get_settings.cache_clear()

    # Rebuild modules bound to the engine/registry with the new DB + clean caches.
    from app.core import database as db_mod
    importlib.reload(db_mod)
    from app.brokers import registry
    registry.reset_registry()

    db_mod.init_db()
    with db_mod.session_scope() as s:
        yield s


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
