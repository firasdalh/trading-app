"""Let winners run: the whole position rides the trail instead of laddering out in thirds.

Why this setting exists. Across 296 closed trades only 3% ever passed +3R — but those 9 trades
produced +78.6R while the other 97% lost -109.1R. The entire result rides on a handful of outliers.
Banking a third at +1.5R and another at +3R turns a 10R runner into a blended ~4.8R: it caps exactly
the trades the book depends on, buying a smoother equity curve with the tail that pays for it.

Off = ladder (the historic behaviour, and what every open trade was entered under).
On  = no partials, and the fixed take-profit is lifted early so the trail alone decides the exit.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.agents.position_advisor import _auto_decision
from app.models.schemas import PositionAdvice


def _pos(entry=100.0, last=101.6, stop=99.0, tp=103.0, direction="long"):
    return SimpleNamespace(symbol="DE30m", direction=direction, entry_price=entry, last_price=last,
                           stop_loss=stop, take_profit=tp, qty=1.0)


def _advice(thesis="intact"):
    return PositionAdvice(symbol="DE30m", direction="long", unrealized_pnl=10.0, has_stop=True,
                          severity="info", headline="", detail="", thesis=thesis, r_multiple=1.6)


def _ctx(regime="trending"):
    return {"atr": 1.0, "last": 101.6, "regime": regime,
            "swing_low": 100.2, "swing_high": 103.5}


# --- the ladder (default) -----------------------------------------------------------------------

def test_ladder_banks_a_third_at_1_5R_by_default():
    d = _auto_decision(_advice(), _pos(), _ctx(), plan_risk=1.0, tranche=0, has_plan=True)
    assert d is not None and d["action"] == "take_partial"


def test_scale_out_off_takes_no_partial():
    """The whole position stays on — that's the point."""
    d = _auto_decision(_advice(), _pos(), _ctx(), plan_risk=1.0, tranche=0, has_plan=True,
                       scale_out=False)
    assert d is None or d["action"] != "take_partial"


# --- the second ceiling: the fixed target -------------------------------------------------------

def test_target_is_lifted_early_when_the_ladder_is_off():
    """Turning the ladder off must not just swap one ceiling for another — with no partials the
    fixed take-profit becomes the ONLY cap, so it is removed as soon as the trade is +1R."""
    d = _auto_decision(_advice(), _pos(last=101.2), _ctx(), plan_risk=1.0, tranche=0,
                       has_plan=True, scale_out=False)
    assert d is not None and d["action"] == "run_target"
    assert "removing the target" in d["reason"]


def test_with_the_ladder_on_the_target_survives_until_1_8R():
    """Historic behaviour is unchanged when the setting is left alone."""
    d = _auto_decision(_advice(), _pos(last=101.2), _ctx(), plan_risk=1.0, tranche=0, has_plan=True)
    assert d is None or d["action"] != "run_target"


def test_running_still_requires_a_trend_and_an_intact_thesis():
    """Uncapping a winner is only safe while the move is actually working."""
    broken = _auto_decision(_advice(thesis="weakening"), _pos(last=101.2), _ctx(), plan_risk=1.0,
                            scale_out=False)
    ranging = _auto_decision(_advice(), _pos(last=101.2), _ctx(regime="ranging"), plan_risk=1.0,
                             scale_out=False)
    for d in (broken, ranging):
        assert d is None or d["action"] != "run_target"


def test_run_never_loosens_the_stop():
    """Removing the target must not also remove protection — the stop only ever tightens."""
    d = _auto_decision(_advice(), _pos(last=101.2, stop=100.9), _ctx(), plan_risk=1.0,
                       scale_out=False)
    assert d is not None and d["action"] == "run_target"
    assert d["stop"] >= 100.9


# --- config ---------------------------------------------------------------------------------

def test_default_is_the_ladder(db_session):
    from app.agents.position_advisor import get_or_create_advisor_config

    assert get_or_create_advisor_config(db_session).scale_out_enabled is True
