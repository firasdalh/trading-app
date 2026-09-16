"""Two anti-duplication rules around ARMED setups, both from real losing trades.

1. A stopless arm is refused. Position size is derived from the stop distance, so with no stop the
   Risk Manager's first gate vetoes it at EVERY trigger — it re-arms, triggers, is vetoed, forever,
   while looking active in the panel. (ETHUSDm #26, XAUUSDm #39.)

2. The Hybrid skips a symbol that already has an armed setup. Otherwise it opens the SAME idea at
   market underneath your planned entry, and the arm is then auto-cancelled because "a position is
   already open" — quietly replacing your chosen price with the market one. (XAUUSDm 2026-08-03:
   armed 18:53 @ 4044.2, Hybrid opened 18:59 @ 4043.7, arm cancelled, trade lost $50.67.)
"""
from __future__ import annotations

from app.agents.conditional import active_armed, arm_conditional
from app.models.enums import ConditionalStatus


def _arm(session, **kw):
    base = dict(symbol="XAUUSDm", asset_class="metal", timeframe="15m", direction="short",
                order_type="sell_limit", trigger_price=4044.217, stop_loss=4060.0,
                take_profit=4028.42, confidence=0.65, rr=1.5, source="manual")
    base.update(kw)
    return arm_conditional(session, **base)


# --- 1. stopless arms are refused --------------------------------------------------------------

def test_arm_without_stop_is_refused(db_session):
    assert _arm(db_session, stop_loss=None) is None
    assert active_armed(db_session) == []


def test_arm_with_stop_is_accepted(db_session):
    s = _arm(db_session)
    assert s is not None and s.status == ConditionalStatus.ARMED.value
    assert s.stop_loss == 4060.0


def test_stopless_arm_refused_for_every_source(db_session):
    """Hybrid auto-arming must not create zombies either."""
    assert _arm(db_session, source="hybrid", auto_execute=True, stop_loss=None) is None


def test_arm_route_explains_the_missing_stop(db_session):
    """The API says WHY rather than returning a generic conflict."""
    from fastapi import HTTPException

    from app.api.conditional_routes import ArmRequest, arm

    req = ArmRequest(symbol="XAUUSDm", asset_class="metal", timeframe="15m", direction="short",
                     order_type="sell_limit", trigger_price=4044.217, stop_loss=None,
                     take_profit=4028.42, confidence=0.65)
    try:
        arm(req, session=db_session)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "no stop-loss" in exc.detail
    else:
        raise AssertionError("expected the arm to be refused")


# --- 2. the Hybrid stands aside on an armed symbol ----------------------------------------------

def test_hybrid_skips_a_symbol_that_is_already_armed(db_session, monkeypatch):
    """The scan must not even consider a symbol with a setup waiting — that's the duplicate entry."""
    from app.agents import hybrid as H
    from app.models.db import WatchItem

    armed = _arm(db_session)
    assert armed is not None
    db_session.add(WatchItem(symbol="XAUUSDm", asset_class="metal", timeframe="15m", enabled=True))
    db_session.commit()

    monkeypatch.setattr(H, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(H, "live_broker_positions", lambda s: [])

    analysed: list[str] = []

    def _boom(session, symbol, *a, **k):       # records any symbol the scan tries to analyse
        analysed.append(symbol)
        raise AssertionError(f"{symbol} should have been skipped — it is already armed")

    monkeypatch.setattr(H, "preview_symbol", _boom)
    out = H.run_hybrid(db_session)

    assert out["ran"] is True
    assert out["opened"] is None
    assert analysed == []                      # never even looked at the armed symbol


def test_hybrid_still_scans_symbols_that_are_not_armed(db_session, monkeypatch):
    """The guard must be narrow: only the ARMED symbol is skipped, others still scan."""
    from app.agents import hybrid as H
    from app.models.db import WatchItem

    _arm(db_session)                            # XAUUSDm armed
    for sym, ac in (("XAUUSDm", "metal"), ("DE30m", "index")):
        db_session.add(WatchItem(symbol=sym, asset_class=ac, timeframe="15m", enabled=True))
    db_session.commit()

    monkeypatch.setattr(H, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(H, "live_broker_positions", lambda s: [])

    seen: list[str] = []

    def _preview(session, symbol, *a, **k):
        seen.append(symbol)
        raise RuntimeError("stop here — we only care which symbols were reached")

    monkeypatch.setattr(H, "preview_symbol", _preview)
    H.run_hybrid(db_session)

    assert "XAUUSDm" not in seen               # armed -> skipped
    assert "DE30m" in seen                     # not armed -> still scanned


# --- 3. ranking: a fresh higher-timeframe leg goes first ------------------------------------------

def _prop_with_4h_leg(symbol, confidence, age, st_dir=1.0):
    from app.models.enums import AssetClass, Direction
    from app.models.schemas import TechnicalRead, TimeframeRead, TradeProposal

    tech = TechnicalRead(symbol=symbol, overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators={"supertrend_dir": 1.0,
                                                             "supertrend_bars_since_flip": 5.0}),
        TimeframeRead(timeframe="4h", trend="up", indicators={"supertrend_dir": st_dir,
                                                             "supertrend_bars_since_flip": age}),
    ])
    return TradeProposal(symbol=symbol, asset_class=AssetClass.INDEX, timeframe="1h",
                         direction=Direction.LONG, confidence=confidence, technical=tech)


def test_fresh_higher_leg_reads_the_next_timeframe_up():
    from app.agents.hybrid import _FRESH_HTF_LEG_BARS, _fresh_higher_leg

    assert _fresh_higher_leg(_prop_with_4h_leg("A", 0.8, float(_FRESH_HTF_LEG_BARS))) is True
    assert _fresh_higher_leg(_prop_with_4h_leg("A", 0.8, float(_FRESH_HTF_LEG_BARS + 1))) is False
    assert _fresh_higher_leg(_prop_with_4h_leg("A", 0.8, 2.0, st_dir=-1.0)) is False   # leg points the other way


def test_hybrid_opens_the_fresh_higher_tf_leg_before_higher_confidence(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.agents import hybrid as H
    from app.models.db import WatchItem

    for sym in ("XAUUSDm", "DE30m"):
        db_session.add(WatchItem(symbol=sym, asset_class="index", timeframe="1h", enabled=True))
    db_session.commit()
    monkeypatch.setattr(H, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(H, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(H, "get_broker_for", lambda ac, bm: SimpleNamespace(market_open=lambda sym: True))
    props = {"XAUUSDm": _prop_with_4h_leg("XAUUSDm", 0.90, 40.0),   # higher confidence, old 4h leg
             "DE30m": _prop_with_4h_leg("DE30m", 0.75, 3.0)}        # lower confidence, fresh 4h leg
    monkeypatch.setattr(H, "preview_symbol",
                        lambda session, symbol, *a, **k: (props[symbol], SimpleNamespace(approved=True)))
    chosen: list[str] = []

    def _analyze(session, symbol, *a, **k):
        chosen.append(symbol)
        return SimpleNamespace(proposal_id=-1)        # stop right after the pick

    monkeypatch.setattr(H, "analyze_symbol", _analyze)
    H.run_hybrid(db_session)
    assert chosen == ["DE30m"]
