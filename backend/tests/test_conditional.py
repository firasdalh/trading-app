"""Tests for conditional ('armed' / pending) setups: the engine suggestion + the trigger service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.agents.conditional as cond
import app.agents.pipeline as pipeline
from app.agents.orchestrator import _conditional_break, _conditional_pullback
from app.models.db import ConditionalSetup, TradeProposalRecord
from app.models.enums import Direction, ProposalStatus, RiskDecisionType
from app.models.schemas import AnalyzeResponse, RiskDecision, TradeProposal


# ---------------------------------------------------------------- engine helper

def test_conditional_break_short_enters_on_break_of_support():
    # SHORT 79.5 -> 77.4 with a support cluster at 78.2 blocking the path -> sell_stop below 78.2.
    c = _conditional_break(Direction.SHORT, entry=79.5, atr_v=0.3,
                           levels=[77.0, 78.2, 80.0], target=77.4, confidence=0.6)
    assert c is not None and c.order_type == "sell_stop"
    assert c.trigger_price < 78.2 < c.stop_loss   # trigger below the level, stop above it
    assert c.take_profit == 77.4 and c.rr >= 1.5


def test_conditional_break_long_enters_on_break_of_resistance():
    c = _conditional_break(Direction.LONG, entry=100.0, atr_v=1.0,
                           levels=[98.0, 104.0, 110.0], target=110.0, confidence=0.6)
    assert c is not None and c.order_type == "buy_stop"
    assert c.stop_loss < 104.0 < c.trigger_price
    assert c.take_profit == 110.0


def test_conditional_break_none_when_path_is_clear():
    # No key level between entry and target -> nothing to wait for.
    assert _conditional_break(Direction.SHORT, 79.5, 0.3, [80.0, 81.0], 77.4, 0.6) is None


def test_conditional_pullback_offers_better_long_entry_at_value():
    # Overextended LONG (entry 105 far above EMA20 100) -> buy_limit back at value (~100).
    c = _conditional_pullback(Direction.LONG, entry=105.0, ema20=100.0, atr_v=1.0,
                              ind={"swing_low": 99.0}, target=112.0, confidence=0.6)
    assert c is not None and c.order_type == "buy_limit"
    assert c.trigger_price == 100.0 and c.stop_loss < 99.0  # at value, stop below the swing
    assert c.take_profit == 112.0 and c.rr >= 1.5


def test_conditional_pullback_none_when_not_overextended():
    # Entry already at/below value -> no better pullback entry to wait for.
    assert _conditional_pullback(Direction.LONG, entry=100.0, ema20=100.0, atr_v=1.0,
                                 ind={}, target=112.0, confidence=0.6) is None


# ---------------------------------------------------------------- service

def _arm(session, **kw):
    defaults = dict(symbol="UKOILm", asset_class="energy", timeframe="1h", direction="short",
                    order_type="sell_stop", trigger_price=78.2, stop_loss=78.4, take_profit=77.4,
                    confidence=0.6, rr=2.0, status="armed", source="hybrid", auto_execute=True,
                    require_close_confirm=True,
                    valid_until=datetime.now(timezone.utc) + timedelta(hours=6))
    defaults.update(kw)
    s = ConditionalSetup(**defaults)
    session.add(s)
    session.commit()
    return s


def _stub_market(monkeypatch, price: float):
    broker = SimpleNamespace(get_quote=lambda sym: SimpleNamespace(price=price))
    monkeypatch.setattr(cond, "get_broker_for", lambda ac, bm: broker)
    monkeypatch.setattr(cond, "get_ohlcv_cached",
                        lambda b, sym, tf, limit=3: SimpleNamespace(candles=[SimpleNamespace(close=price)]))
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(cond, "kill_switch_active", lambda s: False)


def _fake_analyze(direction: str, approved: bool, status: str):
    def _impl(session, symbol, asset_class, timeframe, use_llm=True):
        rec = TradeProposalRecord(symbol=symbol, asset_class=asset_class.value, timeframe=timeframe,
                                  direction=direction, entry=78.0, stop_loss=78.4, take_profit=77.4,
                                  confidence=0.62, status=status)
        session.add(rec)
        session.commit()
        prop = TradeProposal(symbol=symbol, asset_class=asset_class, direction=Direction(direction),
                             entry=78.0, stop_loss=78.4, take_profit=77.4, confidence=0.62)
        risk = RiskDecision(
            decision=RiskDecisionType.APPROVED if approved else RiskDecisionType.VETOED,
            approved=approved, reason="ok" if approved else "no", symbol=symbol)
        return AnalyzeResponse(proposal_id=rec.id, status=rec.status, proposal=prop, risk=risk)
    return _impl


def test_trigger_fires_and_opens_on_break(db_session, monkeypatch):
    s = _arm(db_session)
    _stub_market(monkeypatch, price=78.0)  # below the 78.2 sell-stop trigger
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        _fake_analyze("short", approved=True, status=ProposalStatus.EXECUTED.value))
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1
    db_session.refresh(s)
    assert s.status == "triggered" and s.result_proposal_id is not None


def test_trigger_rearms_when_recheck_declines(db_session, monkeypatch):
    s = _arm(db_session)
    _stub_market(monkeypatch, price=78.0)
    # Double-check says NO_TRADE (timing miss) -> stays ARMED with a cooldown, nothing opens.
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        _fake_analyze("no_trade", approved=False, status=ProposalStatus.RISK_VETOED.value))
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed" and s.retries == 1 and s.cooldown_until is not None


def test_trigger_rejected_after_max_retries(db_session, monkeypatch):
    s = _arm(db_session, retries=cond._MAX_RETRIES - 1)
    _stub_market(monkeypatch, price=78.0)
    monkeypatch.setattr(pipeline, "analyze_symbol",
                        _fake_analyze("no_trade", approved=False, status=ProposalStatus.RISK_VETOED.value))
    cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert s.status == "rejected" and s.retries == cond._MAX_RETRIES


def test_no_trigger_when_price_above_sell_stop_trigger(db_session, monkeypatch):
    # A break short sits ABOVE its trigger (and its stop) until the break — it must stay ARMED,
    # not be wrongly invalidated for being on the far side of the stop.
    s = _arm(db_session)
    _stub_market(monkeypatch, price=78.5)  # above trigger 78.2 and above stop 78.4 -> still waiting
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed"


def test_expired_setup_is_marked(db_session, monkeypatch):
    s = _arm(db_session, valid_until=datetime.now(timezone.utc) - timedelta(hours=1))
    _stub_market(monkeypatch, price=78.0)
    out = cond.check_conditional_setups(db_session)
    assert out["expired"] == 1
    db_session.refresh(s)
    assert s.status == "expired"


def test_hybrid_arms_blocked_candidates(db_session, monkeypatch):
    """Hybrid arms a 'wait for the break' conditional for a blocked-but-valid candidate instead of
    discarding it — even when nothing is opened at market this tick."""
    import app.agents.hybrid as hybrid
    from app.models.db import WatchItem
    from app.models.enums import AssetClass, RiskDecisionType
    from app.models.schemas import ConditionalSuggestion, RiskDecision, TradeProposal

    db_session.add(WatchItem(symbol="UKOILm", asset_class="energy", timeframe="1h", enabled=True))
    cfg = hybrid.get_or_create_hybrid_config(db_session)
    cfg.enabled, cfg.conditional_enabled = True, True
    db_session.commit()

    sugg = ConditionalSuggestion(order_type="sell_stop", trigger_price=78.1, stop_loss=78.4,
                                 take_profit=77.4, confidence=0.62, rr=2.0, reason="break 78.2")
    prop = TradeProposal(symbol="UKOILm", asset_class=AssetClass.ENERGY, direction=Direction.NO_TRADE,
                         confidence=0.0, conditional=sugg)  # not actionable now, but valid on a break
    dec = RiskDecision(decision=RiskDecisionType.VETOED, approved=False, reason="blocked", symbol="UKOILm")

    monkeypatch.setattr(hybrid, "preview_symbol", lambda *a, **k: (prop, dec))
    monkeypatch.setattr(hybrid, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(hybrid, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])

    hybrid.run_hybrid(db_session)
    armed = cond.active_armed(db_session)
    assert any(a.symbol == "UKOILm" and a.order_type == "sell_stop" and a.source == "hybrid"
               for a in armed)


def test_arm_conditional_dedups_same_symbol_direction(db_session, monkeypatch):
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])
    first = cond.arm_conditional(db_session, symbol="UKOILm", asset_class="energy", timeframe="1h",
                                 direction="short", order_type="sell_stop", trigger_price=78.2,
                                 stop_loss=78.4, take_profit=77.4, confidence=0.6, rr=2.0)
    again = cond.arm_conditional(db_session, symbol="UKOILm", asset_class="energy", timeframe="1h",
                                 direction="short", order_type="sell_stop", trigger_price=78.2,
                                 stop_loss=78.4, take_profit=77.4, confidence=0.6, rr=2.0)
    assert first is not None and again is None
