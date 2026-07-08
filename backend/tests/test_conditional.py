"""Tests for conditional ('armed' / pending) setups: the engine suggestion + the trigger service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.agents.conditional as cond
from app.agents.orchestrator import _conditional_break, _conditional_pullback, _conditional_resumption
from app.models.db import ConditionalSetup, TradeProposalRecord
from app.models.enums import Direction, ProposalStatus, RiskDecisionType
from app.models.schemas import RiskDecision


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


def test_conditional_break_skips_reclaimed_support_short():
    # Price already dipped BELOW the 78.2 support and traded back above it (recent_low < block) — a
    # failed breakdown / bull trap. Don't arm another short break of it (the XAGGBP re-short loop).
    trap = {"recent_low": 78.0, "recent_high": 80.5}
    assert _conditional_break(Direction.SHORT, 79.5, 0.3, [77.0, 78.2, 80.0], 77.4, 0.6, trap) is None
    # Recent low stayed ABOVE the level (untested support) -> still a clean break to arm.
    clean = {"recent_low": 78.3, "recent_high": 80.5}
    c = _conditional_break(Direction.SHORT, 79.5, 0.3, [77.0, 78.2, 80.0], 77.4, 0.6, clean)
    assert c is not None and c.order_type == "sell_stop"


def test_conditional_break_skips_reclaimed_resistance_long():
    # Price spiked ABOVE the 104 resistance and fell back below (recent_high > block) — a failed
    # breakout. Don't arm a long break of it.
    trap = {"recent_high": 104.5, "recent_low": 96.0}
    assert _conditional_break(Direction.LONG, 100.0, 1.0, [98.0, 104.0, 110.0], 110.0, 0.6, trap) is None


def test_conditional_pullback_offers_better_long_entry_at_value():
    # Overextended LONG (entry 105 far above EMA20 100) -> buy_limit back at value (~100).
    c = _conditional_pullback(Direction.LONG, entry=105.0, ema20=100.0, atr_v=1.0,
                              ind={"swing_low": 99.0}, target=112.0, confidence=0.6)
    assert c is not None and c.order_type == "buy_limit"
    assert c.trigger_price == 100.0 and c.stop_loss < 99.0  # at value, stop below the swing
    # Target is R:R-capped (4R from the value entry: 100 + 4*1.5 = 106) instead of the far 112.
    assert c.take_profit == 106.0 and c.rr == 4.0


def test_conditional_pullback_none_when_not_overextended():
    # Entry already at/below value -> no better pullback entry to wait for.
    assert _conditional_pullback(Direction.LONG, entry=100.0, ema20=100.0, atr_v=1.0,
                                 ind={}, target=112.0, confidence=0.6) is None


def test_conditional_resumption_long_arms_on_break_of_swing_high():
    # Uptrend pullback -> buy_stop above the last swing high, stop below it, target the next level.
    c = _conditional_resumption(Direction.LONG, entry=100.0,
                                ind={"swing_high": 100.5, "swing_low": 99.7},
                                atr_v=0.3, levels=[102.0], confidence=0.6)
    assert c is not None and c.order_type == "buy_stop"
    assert c.trigger_price > 100.5 and c.stop_loss < 100.5 and c.rr >= 1.5


def test_conditional_resumption_none_without_swing_above():
    # No swing high above the current price -> nothing to break for a resumption.
    assert _conditional_resumption(Direction.LONG, entry=100.0, ind={"swing_high": 99.0},
                                   atr_v=0.3, levels=[102.0], confidence=0.6) is None


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
    # 3 candles all at `price` so the lower-TF break confirmation (last 2 closes beyond the trigger)
    # is satisfied whenever the trigger is crossed (these tests set price already past the trigger).
    monkeypatch.setattr(cond, "get_ohlcv_cached",
                        lambda b, sym, tf, limit=3: SimpleNamespace(candles=[SimpleNamespace(close=price)] * 3))
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(cond, "kill_switch_active", lambda s: False)


def _tech():
    """A minimal deterministic TechnicalRead for the re-check's reviewer context + audit."""
    from app.models.schemas import TechnicalRead, TimeframeRead
    return TechnicalRead(
        symbol="UKOILm", overall_trend="down", confidence=0.5, notes="",
        timeframes=[TimeframeRead(timeframe="1h", trend="down", support_levels=[77.0],
                                  resistance_levels=[79.0], indicators={"adx": 28.0},
                                  patterns=[], comment="")])


def _stub_fire(monkeypatch, *, approved=True, qty=0.05, risk_amount=12.0,
               veto=None, technical=True, captured=None):
    """Stub the re-validation dependencies of ``_fire``: the market re-read, the AI thesis review,
    the Risk Manager verdict, and the executor — so a test drives the double-check deterministically.

    ``_fire`` now validates the ARMED levels (it no longer re-runs ``analyze_symbol``)."""
    import app.agents.orchestrator as orch
    import app.execution.executor as executor
    import app.risk.service as risk_service

    monkeypatch.setattr(cond, "_reread_technical", lambda session, s: (_tech() if technical else None))
    monkeypatch.setattr(orch, "review_armed_setup",
                        lambda *a, **k: ((False, veto) if veto else (True, "re-confirmed")))

    def fake_assess(session, proposal, **k):
        if captured is not None:
            captured["proposal"] = proposal
        return RiskDecision(
            decision=RiskDecisionType.APPROVED if approved else RiskDecisionType.VETOED,
            approved=approved, reason="ok" if approved else "exposure full",
            symbol=proposal.symbol, approved_qty=qty if approved else 0.0,
            risk_amount=risk_amount if approved else 0.0)
    monkeypatch.setattr(risk_service, "assess", fake_assess)

    def fake_exec(session, record):
        record.status = ProposalStatus.EXECUTED.value
        session.commit()
        return None
    monkeypatch.setattr(executor, "execute_proposal", fake_exec)


def test_mechanical_invalidation_target_and_stop():
    # Pure unit: between the levels -> ok; at/through the target -> terminal (reject); snapped back
    # through the stop -> non-terminal (re-arm). Both directions.
    short = SimpleNamespace(direction="short", stop_loss=78.4, take_profit=77.4)
    assert cond._mechanical_invalidation(short, 78.0) == (None, False)
    assert cond._mechanical_invalidation(short, 77.3)[1] is True and "target" in cond._mechanical_invalidation(short, 77.3)[0]
    assert cond._mechanical_invalidation(short, 78.5)[1] is False and "stop" in cond._mechanical_invalidation(short, 78.5)[0]




def _stub_market_series(monkeypatch, closes):
    """Like _stub_market, but returns a full candle window so the trend-EMA invalidation can run.
    The live quote and the confirmed close are both the last close in the series."""
    candles = [SimpleNamespace(close=c) for c in closes]
    broker = SimpleNamespace(get_quote=lambda sym: SimpleNamespace(price=closes[-1]))
    monkeypatch.setattr(cond, "get_broker_for", lambda ac, bm: broker)
    monkeypatch.setattr(cond, "get_ohlcv_cached",
                        lambda b, sym, tf, limit=60: SimpleNamespace(candles=candles))
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(cond, "kill_switch_active", lambda s: False)


def test_buy_stop_breakout_waits_for_trigger(db_session, monkeypatch):
    # The JP225 bug: a buy_stop breakout long, price waiting BELOW the trigger (100) and far below the
    # EMA50 — and even below its own stop (98) — must stay ARMED. A breakout order legitimately sits on
    # the far side of its (post-entry) stop while it waits; only the TARGET or valid_until invalidate.
    s = _arm(db_session, direction="long", order_type="buy_stop", trigger_price=100.0,
             stop_loss=98.0, take_profit=106.0)
    _stub_market_series(monkeypatch, [110.0] * 59 + [97.0])   # below EMA and below the stop, but < target
    out = cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert s.status == "armed" and out["invalidated"] == 0 and out["triggered"] == 0


def test_buy_stop_fires_only_after_lower_tf_confirms(db_session, monkeypatch):
    # buy_stop breakout: price tagged the trigger AND the last 2 (lower-TF) closes hold above it -> fires
    s = _arm(db_session, direction="long", order_type="buy_stop", trigger_price=100.0,
             stop_loss=98.0, take_profit=106.0)
    _stub_market_series(monkeypatch, [99.0] * 59 + [100.5, 101.0])   # last 2 closes above the trigger
    _stub_fire(monkeypatch)                                          # approve + execute the re-check
    out = cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert out["triggered"] == 1 and s.status == "triggered"


def test_buy_stop_waits_when_break_not_confirmed(db_session, monkeypatch):
    # the trigger is tagged on the last bar, but the prior bar closed BACK below it (a wick) -> the
    # 2-candle break confirmation fails -> stays armed, doesn't open on the false break.
    s = _arm(db_session, direction="long", order_type="buy_stop", trigger_price=100.0,
             stop_loss=98.0, take_profit=106.0)
    _stub_market_series(monkeypatch, [99.0] * 59 + [99.5, 100.5])    # only the last close is above
    out = cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert out["triggered"] == 0 and s.status == "armed"
    assert "confirm the break" in (s.last_note or "")


def test_sell_stop_breakout_waits_when_price_above_stop(db_session, monkeypatch):
    # The USOIL bug: a sell_stop short with price ABOVE its future stop (78.4) while waiting for the
    # break down through the trigger (78.2) must stay armed, not be invalidated for sitting past the stop.
    s = _arm(db_session)  # short sell_stop, trigger 78.2, SL 78.4, TP 77.4
    _stub_market_series(monkeypatch, [79.0] * 59 + [78.6])   # above the stop, above the trigger
    out = cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert s.status == "armed" and out["invalidated"] == 0 and out["triggered"] == 0




def test_trigger_fires_and_opens_on_break(db_session, monkeypatch):
    s = _arm(db_session)
    _stub_market(monkeypatch, price=78.0)  # below the 78.2 sell-stop trigger
    _stub_fire(monkeypatch, approved=True)
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1
    db_session.refresh(s)
    assert s.status == "triggered" and s.result_proposal_id is not None


def test_break_entry_validated_on_its_own_levels_not_from_market(db_session, monkeypatch):
    # THE FIX: the double-check sizes the trade from the ARMED trigger/stop/target (where its R:R is
    # real), NOT a fresh trade re-derived from the current price. So a break whose next level sits
    # <1R below the *current* price (the old USOIL decline) still opens. We assert the proposal handed
    # to the Risk Manager carries the armed levels, with entry == trigger.
    s = _arm(db_session, trigger_price=73.935, stop_loss=74.321, take_profit=73.12)
    _stub_market(monkeypatch, price=73.93)  # just through the break
    captured: dict = {}
    _stub_fire(monkeypatch, approved=True, captured=captured)
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1
    p = captured["proposal"]
    assert p.entry == 73.935 and p.stop_loss == 74.321 and p.take_profit == 73.12
    assert p.direction == Direction.SHORT


def test_trigger_rearms_when_risk_declines(db_session, monkeypatch):
    # Risk Manager says no at the trigger (e.g. exposure full) -> stays ARMED with a cooldown.
    s = _arm(db_session)
    _stub_market(monkeypatch, price=78.0)
    _stub_fire(monkeypatch, approved=False)
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed" and s.retries == 1 and s.cooldown_until is not None


def test_trigger_rearms_when_thesis_vetoed(db_session, monkeypatch):
    # The AI reviewer vetoes the armed thesis at the trigger (e.g. higher-TF trend flipped against
    # it) -> the level stays armed with a cooldown rather than opening into a broken thesis.
    s = _arm(db_session)
    _stub_market(monkeypatch, price=78.0)
    _stub_fire(monkeypatch, veto="higher-timeframe trend flipped up")
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed" and s.retries == 1


def test_trigger_rejected_when_move_already_hit_target(db_session, monkeypatch):
    # Price gapped straight through to the target before we could fire -> the move is done; reject
    # (re-arming the same level is pointless). A mechanical miss — no risk/LLM call needed.
    s = _arm(db_session)  # short trigger 78.2, target 77.4
    _stub_market(monkeypatch, price=77.3)  # already below the 77.4 target
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "rejected" and "target" in (s.last_note or "")


def test_trigger_rejected_after_max_retries(db_session, monkeypatch):
    s = _arm(db_session, retries=cond._MAX_RETRIES - 1)
    _stub_market(monkeypatch, price=78.0)
    _stub_fire(monkeypatch, approved=False)
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


def test_armed_auto_cancelled_when_symbol_already_open(db_session, monkeypatch):
    """If a position for the symbol is already open at the broker, the armed setup is redundant and
    is auto-cancelled (so a trigger can't try to stack a second position)."""
    s = _arm(db_session)  # UKOILm short armed
    monkeypatch.setattr(cond, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(cond, "live_broker_positions",
                        lambda s: [SimpleNamespace(symbol="UKOILm", direction="short")])
    out = cond.check_conditional_setups(db_session)
    assert out["cancelled"] == 1
    db_session.refresh(s)
    assert s.status == "cancelled" and "already open" in (s.last_note or "")


def test_conditional_size_preview_prices_from_trigger(db_session, monkeypatch):
    """The armed-setup size preview prices off the trigger (as entry) via the standard sizer."""
    import app.risk.service as risk_service
    from app.api.conditional_routes import LotRequest, size_preview
    from app.models.enums import RiskDecisionType
    from app.models.schemas import RiskDecision

    s = _arm(db_session)
    seen: dict = {}

    def fake_sp(session, record, desired_lots=None):
        seen.update(entry=record.entry, symbol=record.symbol, lots=desired_lots)
        return {"risk": RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok",
                                     symbol=record.symbol, approved_qty=0.05, risk_amount=12.0),
                "economics": {"lots": 0.05, "margin_usd": 30.0}, "capped": False, "max_lots": 0.1}

    monkeypatch.setattr(risk_service, "size_preview", fake_sp)
    res = size_preview(s.id, LotRequest(lots=0.05), session=db_session)
    assert seen["entry"] == s.trigger_price and seen["symbol"] == s.symbol and seen["lots"] == 0.05
    assert res.economics.margin_usd == 30.0 and res.max_lots == 0.1


def test_set_conditional_lots_persists(db_session):
    from app.api.conditional_routes import LotRequest, set_lots
    s = _arm(db_session)
    res = set_lots(s.id, LotRequest(lots=0.07), session=db_session)
    assert res.desired_lots == 0.07
    db_session.refresh(s)
    assert s.desired_lots == 0.07


def test_desired_lots_resizes_proposal_on_fire(db_session, monkeypatch):
    import app.risk.service as risk_service

    s = _arm(db_session, auto_execute=False, desired_lots=0.07)  # Mode A -> queue for approval
    _stub_market(monkeypatch, price=78.0)
    _stub_fire(monkeypatch, approved=True, qty=0.05)  # default size 0.05; user wants 0.07
    monkeypatch.setattr(risk_service, "size_preview",
                        lambda session, record, desired_lots=None: {
                            "risk": RiskDecision(decision=RiskDecisionType.APPROVED, approved=True,
                                                 reason="ok", symbol=record.symbol, approved_qty=0.07,
                                                 risk_amount=21.0),
                            "economics": {"lots": 0.07}, "capped": False, "max_lots": 0.1})
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1
    db_session.refresh(s)
    rec = db_session.get(TradeProposalRecord, s.result_proposal_id)
    assert rec.approved_qty == 0.07 and s.status == "triggered"


def test_clear_finished_removes_only_terminal(db_session):
    _arm(db_session, symbol="UKOILm", status="armed")
    _arm(db_session, symbol="HK50m", status="rejected")
    _arm(db_session, symbol="US500m", status="cancelled")
    n = cond.clear_finished(db_session)
    assert n == 2
    remaining = cond.list_conditionals(db_session)
    assert len(remaining) == 1 and remaining[0].status == "armed"


def test_arm_conditional_dedups_same_symbol_direction(db_session, monkeypatch):
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])
    first = cond.arm_conditional(db_session, symbol="UKOILm", asset_class="energy", timeframe="1h",
                                 direction="short", order_type="sell_stop", trigger_price=78.2,
                                 stop_loss=78.4, take_profit=77.4, confidence=0.6, rr=2.0)
    again = cond.arm_conditional(db_session, symbol="UKOILm", asset_class="energy", timeframe="1h",
                                 direction="short", order_type="sell_stop", trigger_price=78.2,
                                 stop_loss=78.4, take_profit=77.4, confidence=0.6, rr=2.0)
    assert first is not None and again is None
