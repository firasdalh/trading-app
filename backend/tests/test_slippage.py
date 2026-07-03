"""Task 8 — the backtest execution-cost (slippage/spread) model. Backtest-scope only."""
from __future__ import annotations

from datetime import datetime, timezone

from app.backtest.slippage import _near_round, _round_step, _session, spread_cost_r
from app.models.enums import AssetClass

THIN = datetime(2026, 1, 5, 2, 0, tzinfo=timezone.utc)     # 02:00 UTC -> thin
ACTIVE = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)  # 14:00 UTC -> London-NY overlap


def test_round_step_scales_with_price():
    assert _round_step(1.1) == 0.1          # FX big figures
    assert _round_step(2000.0) == 100.0     # gold
    assert _round_step(39000.0) == 1000.0   # index


def test_near_round_number():
    assert _near_round(1.1000, atr=0.0040) is True     # sits on 1.1000
    assert _near_round(1.1237, atr=0.0040) is False    # mid-figure


def test_session_classification():
    assert _session(AssetClass.FOREX, THIN) == "thin"
    assert _session(AssetClass.FOREX, ACTIVE) == "active"
    assert _session(AssetClass.CRYPTO, THIN) == "normal"   # 24/7


def test_zero_cost_when_unpriceable():
    # No ATR or non-positive risk -> can't estimate -> 0.
    assert spread_cost_r(entry=1.1, stop=1.1, atr=0.004, entry_time=ACTIVE,
                         asset_class=AssetClass.FOREX, outcome="target") == 0.0
    assert spread_cost_r(entry=1.1, stop=1.09, atr=None, entry_time=ACTIVE,
                         asset_class=AssetClass.FOREX, outcome="target") == 0.0


def test_thin_stop_round_costs_more_than_active_target_midfigure():
    worst = spread_cost_r(entry=1.1000, stop=1.0950, atr=0.0040, entry_time=THIN,
                          asset_class=AssetClass.FOREX, outcome="stop")       # round + thin + stop
    best = spread_cost_r(entry=1.1237, stop=1.1200, atr=0.0040, entry_time=ACTIVE,
                         asset_class=AssetClass.FOREX, outcome="target")      # mid + active + target
    assert worst > best > 0


def test_stop_exit_costs_more_than_target_same_context():
    kw = dict(entry=1.1237, stop=1.1200, atr=0.0040, entry_time=ACTIVE, asset_class=AssetClass.FOREX)
    assert spread_cost_r(outcome="stop", **kw) > spread_cost_r(outcome="target", **kw)


def test_prior_level_widens_cost():
    kw = dict(entry=1.1237, stop=1.1200, atr=0.0040, entry_time=ACTIVE,
              asset_class=AssetClass.FOREX, outcome="target")
    near_level = spread_cost_r(prior_levels=[1.1238], **kw)   # entry sits on a prior swing high
    no_level = spread_cost_r(prior_levels=[1.1500], **kw)     # far away
    assert near_level > no_level
