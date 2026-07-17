"""Per-pair AI auto-trader: opens qualifying setups, respects flat/cooldown/confidence/paper gates."""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.auto_trade as at
from app.models.db import Position, TradeProposalRecord
from app.models.enums import (AssetClass, Direction, PositionStatus, ProposalStatus,
                              RiskDecisionType)
from app.models.schemas import AnalyzeResponse, RiskDecision, TradeProposal


class _Paper:
    is_paper = True


class _Live:
    is_paper = False


def _resp(session, symbol="ETHUSDm", direction="long", conf=0.7, approved=True):
    rec = TradeProposalRecord(symbol=symbol, asset_class="crypto", timeframe="1h", direction=direction,
                              entry=100.0, stop_loss=98.0, take_profit=104.0, confidence=conf,
                              rationale="x", status=ProposalStatus.PENDING_APPROVAL.value)
    session.add(rec)
    session.commit()
    session.refresh(rec)
    prop = TradeProposal(symbol=symbol, asset_class=AssetClass.CRYPTO,
                         direction=Direction.LONG if direction == "long" else Direction.SHORT,
                         entry=100.0, stop_loss=98.0, take_profit=104.0, confidence=conf)
    risk = RiskDecision(decision=RiskDecisionType.APPROVED if approved else RiskDecisionType.VETOED,
                        approved=approved, reason="ok" if approved else "exposure full", symbol=symbol,
                        approved_qty=0.1, risk_amount=30.0)
    return AnalyzeResponse(proposal_id=rec.id, status=rec.status, proposal=prop, risk=risk)


def _cfg(session, min_conf=0.6, cooldown=5):
    cfg = at.get_or_create_auto_trade_config(session)
    cfg.min_confidence, cfg.cooldown_minutes = min_conf, cooldown
    session.commit()
    return cfg


def _pair(symbol="ETHUSDm"):
    return {"symbol": symbol, "asset_class": "crypto", "timeframe": "1h"}


def test_auto_trade_opens_when_qualifies(db_session, monkeypatch):
    import app.execution.executor as executor
    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    monkeypatch.setattr(at, "analyze_symbol", lambda *a, **k: _resp(db_session, conf=0.7))

    def _exec(session, record):
        record.status = ProposalStatus.EXECUTED.value
        session.commit()

    monkeypatch.setattr(executor, "execute_proposal", _exec)
    out = at._auto_trade_symbol(db_session, _cfg(db_session), _pair())
    assert out.get("opened") == "long"


def test_auto_trade_skips_below_min_confidence(db_session, monkeypatch):
    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    monkeypatch.setattr(at, "analyze_symbol", lambda *a, **k: _resp(db_session, conf=0.5))  # < 0.6
    out = at._auto_trade_symbol(db_session, _cfg(db_session, min_conf=0.6), _pair())
    assert "below" in out.get("skipped", "")


def test_auto_trade_vetoed_not_opened(db_session, monkeypatch):
    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    monkeypatch.setattr(at, "analyze_symbol", lambda *a, **k: _resp(db_session, conf=0.8, approved=False))
    out = at._auto_trade_symbol(db_session, _cfg(db_session), _pair())
    assert "risk vetoed" in out.get("skipped", "")


def test_auto_trade_rides_open_position(db_session, monkeypatch):
    db_session.add(Position(symbol="ETHUSDm", asset_class="crypto", direction="long", qty=1.0,
                            entry_price=100.0, status=PositionStatus.OPEN.value, last_price=100.0))
    db_session.commit()
    out = at._auto_trade_symbol(db_session, _cfg(db_session), _pair())
    assert "position open" in out.get("skipped", "")


def test_auto_trade_respects_cooldown(db_session, monkeypatch):
    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    db_session.add(Position(symbol="ETHUSDm", asset_class="crypto", direction="long", qty=1.0,
                            entry_price=100.0, status=PositionStatus.CLOSED.value, last_price=100.0,
                            closed_at=datetime.now(timezone.utc)))
    db_session.commit()
    out = at._auto_trade_symbol(db_session, _cfg(db_session, cooldown=5), _pair())
    assert "cooldown" in out.get("skipped", "")


def test_auto_trade_paper_only(db_session, monkeypatch):
    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Live())
    out = at._auto_trade_symbol(db_session, _cfg(db_session), _pair())
    assert "live" in out.get("skipped", "").lower()


def test_auto_trade_does_not_arm(db_session, monkeypatch):
    # The AI returns a WAIT/conditional and no tradeable scenario here -> the auto-trader NEVER arms a
    # pending order; it skips. (Arming was removed — it only ever opens at market.)
    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    rec = TradeProposalRecord(symbol="BTCUSDm", asset_class="crypto", timeframe="1h",
                              direction="no_trade", confidence=0.0, rationale="wait",
                              status=ProposalStatus.RISK_VETOED.value)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    prop = TradeProposal(symbol="BTCUSDm", asset_class=AssetClass.CRYPTO, direction=Direction.NO_TRADE,
                         confidence=0.0, watch=True)  # no technical -> the scenario open can't run
    risk = RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="no market trade",
                        symbol="BTCUSDm")
    monkeypatch.setattr(at, "analyze_symbol",
                        lambda *a, **k: AnalyzeResponse(proposal_id=rec.id, status=rec.status, proposal=prop, risk=risk))
    out = at._auto_trade_symbol(db_session, _cfg(db_session, min_conf=0.6), _pair("BTCUSDm"))
    assert "armed" not in out and out.get("skipped")


def test_auto_trade_skips_when_book_full(db_session, monkeypatch):
    # ROOM gate: with max_open_positions (3) already open on OTHER pairs, don't fire (before any LLM).
    from app.core.state import get_or_create_risk_config

    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    rc = get_or_create_risk_config(db_session)
    rc.max_open_positions = 3
    for s in ("AAAUSDm", "BBBUSDm", "CCCUSDm"):
        db_session.add(Position(symbol=s, asset_class="crypto", direction="long", qty=1.0,
                                entry_price=100.0, status=PositionStatus.OPEN.value, last_price=100.0))
    db_session.commit()
    out = at._auto_trade_symbol(db_session, _cfg(db_session), _pair("ETHUSDm"))
    assert "no room" in out.get("skipped", "")


def _approved_dec():
    return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="X",
                        approved_qty=0.1, risk_amount=50.0)


def _uptrend_tech(price=100.0, sup=98.0, res=106.0, rsi=None, macd=None):
    from app.models.schemas import TechnicalRead, TimeframeRead
    ind = {"last_close": price, "atr14": 2.0}
    if rsi is not None:
        ind["rsi14"] = rsi
    if macd is not None:
        ind["macd_hist"] = macd
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators=ind, support_levels=[sup], resistance_levels=[res]),
        TimeframeRead(timeframe="4h", trend="up",
                      indicators={"ema20": 100.0, "ema50": 98.0, "ema200": 95.0}),
    ])


def _mock_scen(monkeypatch, *, direction="down", prob=65):
    """Patch the AI scenario read so the primary forward scenario has a given direction + probability."""
    import app.agents.scenarios as scen_mod
    monkeypatch.setattr(scen_mod, "ai_scenarios", lambda *a, **k: {"scenarios": [
        {"label": "test scenario", "direction": direction, "prob": prob, "path": "x", "reasoning": "y"}]})


def _exec_ok(monkeypatch):
    import app.execution.executor as executor

    def _exec(session, record):
        record.status = ProposalStatus.EXECUTED.value
        session.commit()

    monkeypatch.setattr(executor, "execute_proposal", _exec)


def test_scenario_opens_down_toward_support(db_session, monkeypatch):
    # Primary scenario is DOWN (65%); price near resistance -> OPEN a short at market toward support.
    import app.risk.service as risk_service

    _mock_scen(monkeypatch, direction="down", prob=65)
    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved_dec())
    _exec_ok(monkeypatch)
    out = at._open_scenario_move(db_session, "X", "crypto", "1h", _uptrend_tech(price=104.0), _cfg(db_session))
    assert out and out.get("opened") == "short" and "scenario" in out.get("note", "")


def test_scenario_opens_up_toward_resistance(db_session, monkeypatch):
    # Primary scenario is UP (70%); price near support -> OPEN a long at market toward resistance.
    import app.risk.service as risk_service

    _mock_scen(monkeypatch, direction="up", prob=70)
    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved_dec())
    _exec_ok(monkeypatch)
    out = at._open_scenario_move(db_session, "X", "crypto", "1h", _uptrend_tech(price=100.0), _cfg(db_session))
    assert out and out.get("opened") == "long"


def test_scenario_skips_below_confidence(db_session, monkeypatch):
    # Primary scenario prob 55 < min_confidence 60 -> no trade.
    _mock_scen(monkeypatch, direction="down", prob=55)
    assert at._open_scenario_move(db_session, "X", "crypto", "1h", _uptrend_tech(price=104.0),
                                  _cfg(db_session)) is None


def test_scenario_skips_when_momentum_against(db_session, monkeypatch):
    # Down scenario but MACD is strongly UP (against the short) -> the move may not happen -> skip.
    _mock_scen(monkeypatch, direction="down", prob=65)
    assert at._open_scenario_move(db_session, "X", "crypto", "1h", _uptrend_tech(price=104.0, macd=1.0),
                                  _cfg(db_session)) is None


def test_scenario_skips_low_rr(db_session, monkeypatch):
    # Down scenario but price hugs support (tiny reward, far stop) -> R:R below min -> skip.
    _mock_scen(monkeypatch, direction="down", prob=65)
    assert at._open_scenario_move(db_session, "X", "crypto", "1h", _uptrend_tech(price=99.0),
                                  _cfg(db_session)) is None


def test_scenario_skips_sideways(db_session, monkeypatch):
    # No clear directional lean -> nothing to trade.
    _mock_scen(monkeypatch, direction="sideways", prob=80)
    assert at._open_scenario_move(db_session, "X", "crypto", "1h", _uptrend_tech(price=104.0),
                                  _cfg(db_session)) is None


def test_run_auto_trade_persists_last_results(db_session, monkeypatch):
    # The panel shows the last check time + per-pair result/reason -> run_auto_trade must persist them.
    at.set_pair(db_session, "ETHUSDm", "crypto", True)
    monkeypatch.setattr(at, "_auto_trade_symbol",
                        lambda s, cfg, pair: {"symbol": pair["symbol"], "skipped": "cooldown"})
    at.run_auto_trade(db_session)
    cfg = at.get_or_create_auto_trade_config(db_session)
    assert cfg.last_run_at is not None
    assert cfg.last_results and cfg.last_results[0]["symbol"] == "ETHUSDm"
    assert cfg.last_results[0]["skipped"] == "cooldown"


def _closed_auto(db_session, pnl, when=None):
    from datetime import datetime, timezone
    db_session.add(Position(symbol="X", asset_class="crypto", direction="short", qty=1.0,
                            entry_price=100.0, status=PositionStatus.CLOSED.value, last_price=100.0,
                            source="auto_trade", realized_pnl=pnl,
                            closed_at=when or datetime.now(timezone.utc)))


def test_breaker_pauses_after_losing_run(db_session, monkeypatch):
    # 5 recent closed auto-trades net-negative -> the circuit-breaker pauses the whole pass.
    for _ in range(5):
        _closed_auto(db_session, -20.0)
    db_session.commit()
    at.set_pair(db_session, "ETHUSDm", "crypto", True)

    def _boom(*a, **k):
        raise AssertionError("opened a trade despite the circuit-breaker")

    monkeypatch.setattr(at, "_auto_trade_symbol", _boom)
    out = at.run_auto_trade(db_session)
    assert out["opened"] == 0 and "circuit-breaker" in (out.get("breaker") or "")


def test_breaker_resets_after_cooldown(db_session):
    # Same losing run, but the most recent close is older than the pause window -> a probe is allowed.
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    for _ in range(5):
        _closed_auto(db_session, -20.0, when=old)
    db_session.commit()
    assert at._auto_trade_breaker(db_session) is None


def test_breaker_off_when_net_positive(db_session):
    for _ in range(5):
        _closed_auto(db_session, 30.0)
    db_session.commit()
    assert at._auto_trade_breaker(db_session) is None


def test_set_pair_toggles(db_session):
    at.set_pair(db_session, "ETHUSDm", "crypto", True)
    assert any(p["symbol"] == "ETHUSDm" for p in at.get_or_create_auto_trade_config(db_session).pairs)
    at.set_pair(db_session, "ETHUSDm", "crypto", False)
    assert not any(p["symbol"] == "ETHUSDm" for p in at.get_or_create_auto_trade_config(db_session).pairs)
