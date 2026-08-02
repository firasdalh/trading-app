"""Spread gate: refuse an entry whose live bid/ask spread eats too much of its own R.

The spread is a deterministic execution tax charged the instant a trade opens. Measured against
the stop distance it says how much of the risk budget is spent before the thesis is tested — the
real XNGUSDm case (spread 0.012 on a 0.029157 stop = 41% of R) is the reference scenario
throughout: a "95% confidence" short that could not win because it had to be right by 1.41R to
make 1R.

ON by default (unlike the opt-in entry breakers) because it can only ever BLOCK a trade, and it
FAILS OPEN — an unavailable spread never halts trading.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.enums import AssetClass, Direction, RiskDecisionType
from app.models.schemas import AccountState, RiskLimits, TradeProposal
from app.risk.manager import evaluate_proposal

NOW = datetime.now(timezone.utc)

# The live XNGUSDm short that prompted this gate.
XNG_ENTRY, XNG_STOP = 2.7346, 2.763757      # stop distance 0.029157
XNG_SPREAD_ROLLOVER = 0.012                  # 22:30 UTC daily rollover -> 41% of R
XNG_SPREAD_LIQUID = 0.0066                   # its liquid window -> 23% of R


def _acct(**kw):
    base = dict(equity=100_000.0, cash=100_000.0, open_positions=0,
                total_risk_amount=0.0, daily_realized_pnl=0.0, trading_paused=False)
    base.update(kw)
    return AccountState(**base)


def _limits(**kw):
    base = dict(risk_per_trade=0.01, max_open_positions=3, max_daily_loss=0.03,
                max_total_exposure=0.06, per_pair_cooldown_minutes=30,
                risk_per_trade_ceiling=0.02)
    base.update(kw)
    return RiskLimits(**base)


def _prop(entry=100.0, stop=95.0, tp=110.0, direction=Direction.LONG, symbol="AAPL"):
    return TradeProposal(symbol=symbol, asset_class=AssetClass.STOCK, direction=direction,
                         entry=entry, stop_loss=stop, take_profit=tp, confidence=0.7)


def _evaluate(prop, *, spread=None, limits=None, **kw):
    return evaluate_proposal(prop, _acct(), limits or _limits(), now=NOW, qty_step=1,
                             spread=spread, **kw)


# --- the reference case ------------------------------------------------------

def test_vetoes_the_real_xngusd_rollover_spread():
    """0.012 spread on a 0.029157 stop = 41% of R — the trade that started this."""
    prop = _prop(entry=XNG_ENTRY, stop=XNG_STOP, tp=2.6747,
                 direction=Direction.SHORT, symbol="XNGUSDm")
    d = _evaluate(prop, spread=XNG_SPREAD_ROLLOVER)
    assert d.approved is False
    assert d.decision == RiskDecisionType.VETOED
    assert "spread too wide" in d.reason
    assert "41%" in d.reason           # the actual fraction is surfaced to the user
    assert d.checks.get("spread_ok") is False


def test_same_symbol_passes_in_its_liquid_window():
    """The gate is time-aware by construction: the SAME setup at a 0.0066 spread (23% of R) is
    under the 25% default, so the symbol is blocked only when its book is actually too wide."""
    prop = _prop(entry=XNG_ENTRY, stop=XNG_STOP, tp=2.6747,
                 direction=Direction.SHORT, symbol="XNGUSDm")
    d = _evaluate(prop, spread=XNG_SPREAD_LIQUID)
    assert d.approved is True
    assert d.checks.get("spread_ok") is True


# --- journal-calibrated: the default must not block the profitable book ------

def test_default_passes_the_liquid_index_entries():
    """Live spreads vs mean stop distance from the journal for the symbols that make the money:
    JP225 1.3%, USTEC 1.7%, USOIL 2.3%, DE30 1.2% of R — all far under the 0.25 default."""
    for symbol, spread, entry, stop in [
        ("JP225m", 7.10, 61448.7, 60893.9),      # 554.8 stop  -> 1.3%
        ("USTECm", 3.60, 27342.2, 27131.3),      # 210.9 stop  -> 1.7%
        ("USOILm", 0.02, 80.82, 79.96),          # 0.860 stop  -> 2.3%
        ("DE30m", 1.60, 25448.7, 25316.2),       # 132.5 stop  -> 1.2%
    ]:
        d = _evaluate(_prop(entry=entry, stop=stop, tp=entry + (entry - stop) * 2, symbol=symbol),
                      spread=spread)
        assert d.approved is True, f"{symbol} should pass the default gate"
        assert d.checks.get("spread_ok") is True


def test_default_blocks_the_structurally_broken_symbols():
    """The journal's persistent losers, by live spread as a share of their own mean stop:
    AUDCHF 124%, EURUSD 79%, XAGGBP 33% — every one a net-negative symbol."""
    for symbol, spread, entry, stop in [
        ("AUDCHFm", 0.00069, 0.5600, 0.55944),   # 0.00056 stop -> 124%
        ("EURUSDm", 0.00048, 1.14684, 1.14623),  # 0.00061 stop ->  79%
        ("XAGGBPm", 0.02500, 24.0000, 23.9233),  # 0.0767 stop  ->  33%
    ]:
        d = _evaluate(_prop(entry=entry, stop=stop, tp=entry + (entry - stop) * 2, symbol=symbol),
                      spread=spread)
        assert d.approved is False, f"{symbol} should be blocked"
        assert "spread too wide" in d.reason


# --- boundary ----------------------------------------------------------------

def test_spread_exactly_at_the_limit_passes():
    """<= is the boundary: 25% of a 5.0 stop is exactly 1.25, which must not veto."""
    d = _evaluate(_prop(entry=100.0, stop=95.0), spread=1.25)
    assert d.approved is True
    assert d.checks.get("spread_ok") is True


def test_spread_just_over_the_limit_vetoes():
    d = _evaluate(_prop(entry=100.0, stop=95.0), spread=1.30)
    assert d.approved is False
    assert d.checks.get("spread_ok") is False


# --- fail-open behaviour -----------------------------------------------------

def test_unknown_spread_skips_the_gate():
    """A broker that can't report a spread (sim, or a failed tick read) must not block trading."""
    d = _evaluate(_prop(), spread=None)
    assert d.approved is True
    assert d.checks.get("spread_ok") is True


def test_zero_spread_skips_the_gate():
    d = _evaluate(_prop(), spread=0.0)
    assert d.approved is True
    assert d.checks.get("spread_ok") is True


# --- configuration -----------------------------------------------------------

def test_disabled_gate_lets_a_ruinous_spread_through():
    d = _evaluate(_prop(entry=100.0, stop=95.0), spread=4.0,
                  limits=_limits(spread_gate_enabled=False))
    assert d.approved is True
    assert d.checks.get("spread_ok") is True


def test_zero_fraction_disables_the_gate():
    d = _evaluate(_prop(entry=100.0, stop=95.0), spread=4.0,
                  limits=_limits(max_spread_r_fraction=0.0))
    assert d.approved is True


def test_tighter_fraction_blocks_what_the_default_allows():
    """User can tighten to 10%: a 15%-of-R spread passes at 0.25 but not at 0.10."""
    prop = _prop(entry=100.0, stop=95.0)
    assert _evaluate(prop, spread=0.75).approved is True                       # 15% vs default 25%
    tight = _evaluate(prop, spread=0.75, limits=_limits(max_spread_r_fraction=0.10))
    assert tight.approved is False
    assert "max 10%" in tight.reason


def test_gate_is_on_by_default():
    """Unlike the opt-in entry breakers, this one defaults ON — a bare RiskLimits blocks."""
    assert RiskLimits().spread_gate_enabled is True
    assert RiskLimits().max_spread_r_fraction == 0.25


# --- ordering: the gate must not mask a more fundamental rejection -----------

def test_missing_stop_still_reports_the_stop_problem_not_the_spread():
    prop = TradeProposal(symbol="XNGUSDm", asset_class=AssetClass.ENERGY,
                         direction=Direction.SHORT, entry=2.7346, stop_loss=None,
                         take_profit=2.6747, confidence=0.95)
    d = _evaluate(prop, spread=XNG_SPREAD_ROLLOVER)
    assert d.approved is False
    assert "missing entry/stop" in d.reason


def test_short_direction_uses_absolute_stop_distance():
    """A short's stop sits ABOVE entry; the fraction must use |entry - stop|, not a signed value."""
    d = _evaluate(_prop(entry=100.0, stop=105.0, tp=90.0, direction=Direction.SHORT), spread=4.0)
    assert d.approved is False
    assert "spread too wide" in d.reason
