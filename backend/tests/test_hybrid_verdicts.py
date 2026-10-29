"""What the Hybrid records about each pair it scanned (shown in the Monitor)."""
from types import SimpleNamespace as NS

from app.agents.hybrid import _verdict

OK = NS(approved=True, reason="")
VETO = NS(approved=False, reason="spread is 31% of the stop")


def _prop(direction: str, conf: float, conditional=None):
    return NS(direction=NS(value=direction), confidence=conf, strategy="trend",
              rationale="Downtrend pullback - waiting for the break.", conditional=conditional)


def test_directional_setups_by_why_they_did_or_did_not_qualify():
    assert _verdict("EURUSDm", _prop("long", 0.75), OK, 0.68)["outcome"] == "candidate"
    assert _verdict("EURUSDm", _prop("long", 0.61), OK, 0.68)["outcome"] == "below_bar"
    v = _verdict("EURUSDm", _prop("short", 0.75), VETO, 0.68)
    assert (v["outcome"], v["risk"]) == ("risk_veto", "spread is 31% of the stop")


def test_wait_for_the_break_and_stand_aside():
    cond = NS(order_type="sell_limit", trigger_price=157.547, confidence=0.48)
    a = _verdict("USDJPYm", _prop("no_trade", 0.0, cond), VETO, 0.68)
    assert (a["outcome"], a["arm_side"], a["trigger"], a["arm_confidence"]) == ("arm", "short", 157.547, 0.48)
    s = _verdict("USOILm", _prop("no_trade", 0.0), VETO, 0.68)
    assert s["outcome"] == "aside" and s["reason"].startswith("Downtrend pullback")
