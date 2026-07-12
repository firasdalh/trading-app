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


def test_auto_trade_arms_pending_setup(db_session, monkeypatch):
    # The AI decides to WAIT for a retest (arm a buy at support) -> the auto-trader places the pending
    # order (following the scenario level) instead of forcing a market entry.
    import app.agents.conditional as cond_mod
    from app.models.schemas import ConditionalSuggestion

    monkeypatch.setattr(at, "get_broker_for", lambda ac, bm: _Paper())
    rec = TradeProposalRecord(symbol="BTCUSDm", asset_class="crypto", timeframe="1h",
                              direction="no_trade", confidence=0.0, rationale="wait",
                              status=ProposalStatus.RISK_VETOED.value)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    cond = ConditionalSuggestion(order_type="buy_limit", trigger_price=63936.12, stop_loss=63800.0,
                                 take_profit=64500.0, confidence=0.65, rr=2.0, reason="buy the dip at support")
    prop = TradeProposal(symbol="BTCUSDm", asset_class=AssetClass.CRYPTO, direction=Direction.NO_TRADE,
                         confidence=0.0, watch=True, conditional=cond)
    risk = RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="no market trade",
                        symbol="BTCUSDm")
    monkeypatch.setattr(at, "analyze_symbol",
                        lambda *a, **k: AnalyzeResponse(proposal_id=rec.id, status=rec.status, proposal=prop, risk=risk))
    monkeypatch.setattr(cond_mod, "arm_conditional", lambda *a, **k: object())  # armed OK
    out = at._auto_trade_symbol(db_session, _cfg(db_session, min_conf=0.6), _pair("BTCUSDm"))
    assert "armed" in out and "long" in out["armed"]


def _approved_dec():
    return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="X",
                        approved_qty=0.1, risk_amount=50.0)


def _uptrend_tech(price=100.0, sup=98.0, res=106.0):
    from app.models.schemas import TechnicalRead, TimeframeRead
    return TechnicalRead(symbol="X", overall_trend="up", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="up", indicators={"last_close": price, "atr14": 2.0},
                      support_levels=[sup], resistance_levels=[res]),
        TimeframeRead(timeframe="4h", trend="up",
                      indicators={"ema20": 100.0, "ema50": 98.0, "ema200": 95.0}),
    ])


def test_opens_now_when_room_to_target(db_session, monkeypatch):
    # higher-TF up, price mid-range with room to resistance -> OPEN NOW at market (don't wait for a dip).
    import app.execution.executor as executor
    import app.risk.service as risk_service

    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved_dec())

    def _exec(session, record):
        record.status = ProposalStatus.EXECUTED.value
        session.commit()

    monkeypatch.setattr(executor, "execute_proposal", _exec)
    out = at._dip_or_open(db_session, "X", "crypto", "1h", _uptrend_tech(price=100.0), _cfg(db_session))
    assert out and out.get("opened") == "long" and "$" in out.get("note", "")


def test_arms_dip_when_price_near_target(db_session, monkeypatch):
    # price hugging resistance -> open-now R:R is too thin -> ARM the dip-buy at support instead.
    import app.agents.conditional as cond_mod
    import app.risk.service as risk_service

    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: _approved_dec())
    monkeypatch.setattr(cond_mod, "arm_conditional", lambda *a, **k: object())
    out = at._dip_or_open(db_session, "X", "crypto", "1h", _uptrend_tech(price=105.5), _cfg(db_session))
    assert out and "armed" in out and "long" in out["armed"]


def test_skips_when_profit_below_min_usd(db_session, monkeypatch):
    # A tiny risk_amount -> $ potential below the $20 floor -> skip (no trade worth <$20).
    import app.risk.service as risk_service
    from app.models.enums import RiskDecisionType as RDT

    monkeypatch.setattr(risk_service, "assess", lambda *a, **k: RiskDecision(
        decision=RDT.APPROVED, approved=True, reason="ok", symbol="X", approved_qty=0.001, risk_amount=1.0))
    # rr ~ up to 8 * $1 = $8 < $20 floor
    out = at._dip_or_open(db_session, "X", "crypto", "1h", _uptrend_tech(price=105.5), _cfg(db_session))
    assert out is None


def test_dip_none_when_higher_tf_not_up(db_session):
    from app.models.schemas import TechnicalRead, TimeframeRead

    tech = TechnicalRead(symbol="X", overall_trend="sideways", confidence=0.6, timeframes=[
        TimeframeRead(timeframe="1h", trend="sideways", indicators={"last_close": 100.0, "atr14": 2.0},
                      support_levels=[98.0], resistance_levels=[106.0]),
        TimeframeRead(timeframe="4h", trend="sideways",
                      indicators={"ema20": 100.0, "ema50": 100.0, "ema200": 100.0}),
    ])
    assert at._dip_or_open(db_session, "X", "crypto", "1h", tech, _cfg(db_session)) is None


def test_set_pair_toggles(db_session):
    at.set_pair(db_session, "ETHUSDm", "crypto", True)
    assert any(p["symbol"] == "ETHUSDm" for p in at.get_or_create_auto_trade_config(db_session).pairs)
    at.set_pair(db_session, "ETHUSDm", "crypto", False)
    assert not any(p["symbol"] == "ETHUSDm" for p in at.get_or_create_auto_trade_config(db_session).pairs)
