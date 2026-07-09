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


def _ind(rsi, ema10, last, atr=2.0, rec_hi=None, rec_lo=None, cross=0.0, div_bull=0.0, div_bear=0.0):
    return {"rsi14": rsi, "ema10": ema10, "last_close": last, "atr14": atr,
            "recent_high": rec_hi, "recent_low": rec_lo,
            "macd_cross": cross, "macd_div_bull": div_bull, "macd_div_bear": div_bear}


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


def test_macd_divergence_gives_early_long():
    # Oversold, price below EMA10 (not confirmed) but a bullish MACD divergence -> early LONG.
    ind = _ind(20, ema10=99, last=98, atr=2.0, rec_lo=97, div_bull=1.0)
    p = _rsi_over_decision(_base(), ind, None, "X", confirm=True, macd=True)
    assert p.direction == Direction.LONG and "MACD divergence" in p.rationale


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
                        lambda s, tf, confirm=True, macd=False: {"ran": True, "reason": f"tf={tf} c={confirm}", "found": None})
    out = scan_mod.rsi_over_tick(db_session)
    assert out["ran"] and out["reason"] == "tf=1h c=True"
    db_session.refresh(cfg)
    assert cfg.last_run_at is not None  # last_result/candidates are persisted by the sweep's done(), tested separately


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
