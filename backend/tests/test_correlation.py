"""Tests for the correlation / concentration model and the Risk Manager's correlation veto."""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import AccountState, RiskLimits, TradeProposal
from app.risk.correlation import correlated_concentration, exposure_factors
from app.risk.manager import evaluate_proposal


def test_exposure_factors_fx():
    assert exposure_factors("EURUSDm", "long") == {"EUR": 1, "USD": -1}
    assert exposure_factors("USDJPYm", "short") == {"USD": -1, "JPY": 1}


def test_exposure_factors_blocs():
    assert exposure_factors("BTCUSDm", "long") == {"CRYPTO": 1, "USD": -1}
    assert exposure_factors("US500m", "long") == {"EQUITY": 1}
    assert exposure_factors("XAUUSDm", "long") == {"METAL": 1, "USD": -1}
    assert exposure_factors("USOILm", "short") == {"ENERGY": -1}


def test_correlation_blocks_third_usd_bet():
    # short EURUSD + short GBPUSD are both "long USD" (net +2); a 3rd long-USD trade is blocked.
    book = [("EURUSDm", "short"), ("GBPUSDm", "short")]
    reason = correlated_concentration(book, "AUDUSDm", "short")
    assert reason is not None and "USD" in reason


def test_correlation_allows_offsetting():
    # long EURUSD is short USD; long USDJPY is long USD — they offset, not concentrate.
    assert correlated_concentration([("EURUSDm", "long")], "USDJPYm", "long") is None


def test_correlation_blocks_third_crypto():
    book = [("BTCUSDm", "long"), ("ETHUSDm", "long")]  # CRYPTO net +2
    assert correlated_concentration(book, "LTCUSDm", "long") is not None


def test_correlation_empty_book():
    assert correlated_concentration([], "EURUSDm", "long") is None


def test_manager_vetoes_on_correlated_exposure():
    prop = TradeProposal(symbol="AUDUSDm", asset_class=AssetClass.FOREX, direction=Direction.SHORT,
                         entry=0.65, stop_loss=0.655, take_profit=0.64, confidence=0.7)
    acct = AccountState(equity=1000.0, cash=1000.0, open_positions=2)
    d = evaluate_proposal(prop, acct, RiskLimits(), now=datetime.now(timezone.utc),
                          correlated_exposure="2 open positions already net long USD — concentration")
    assert d.decision == RiskDecisionType.VETOED and d.checks.get("not_correlated") is False


def test_manager_approves_without_correlation():
    prop = TradeProposal(symbol="AUDUSDm", asset_class=AssetClass.FOREX, direction=Direction.SHORT,
                         entry=0.65, stop_loss=0.655, take_profit=0.64, confidence=0.7)
    acct = AccountState(equity=1000.0, cash=1000.0, open_positions=0)
    d = evaluate_proposal(prop, acct, RiskLimits(), now=datetime.now(timezone.utc))
    assert d.approved and d.checks.get("not_correlated") is True
