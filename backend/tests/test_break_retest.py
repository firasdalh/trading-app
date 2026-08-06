"""BREAK-AND-RETEST: a two-stage armed entry.

Buying the first thrust through a level is how you get caught by a fake break — price pokes through,
traps the breakout buyers, and snaps back. This setup refuses to look at its entry until the level
has CLOSE-broken, and only then buys the RETEST back at it (old resistance holding as new support).

Two things improve together: the fill moves from above the level to AT it (less risk, better R:R),
and a fake break filters itself out, because a break that immediately reverses never produces a
retest to buy. The honest cost is a break that runs away without coming back — that trade is missed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.agents import conditional as cond
from app.models.db import ConditionalSetup

NOW = datetime.now(timezone.utc)
LEVEL = 100.0
RETEST = 100.1      # limit sits just above the broken level
STOP = 98.8


def _arm(session, **kw):
    base = dict(symbol="DE30m", asset_class="index", timeframe="1h", direction="long",
                order_type="buy_limit", trigger_price=RETEST, stop_loss=STOP, take_profit=106.0,
                confidence=0.6, rr=2.0, status="armed", source="hybrid", auto_execute=True,
                require_close_confirm=True, break_level=LEVEL,
                valid_until=NOW + timedelta(hours=12))
    base.update(kw)
    s = ConditionalSetup(**base)
    session.add(s)
    session.commit()
    return s


def _market(monkeypatch, closes, price=None, lows=None, highs=None):
    """Feed a close series; the live quote defaults to the last close.

    Highs/lows default to the close (a doji) — tests that care about the retest ZONE pass real
    wicks, since a zone touch is measured on the low/high, not the close."""
    candles = [
        SimpleNamespace(close=c,
                        low=(lows[i] if lows else c),
                        high=(highs[i] if highs else c))
        for i, c in enumerate(closes)
    ]
    broker = SimpleNamespace(
        get_quote=lambda sym: SimpleNamespace(price=price if price is not None else closes[-1]),
        market_open=lambda sym: True,
    )
    monkeypatch.setattr(cond, "get_broker_for", lambda ac, bm: broker)
    monkeypatch.setattr(cond, "get_ohlcv_cached",
                        lambda b, sym, tf, limit=60: SimpleNamespace(candles=candles))
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(cond, "kill_switch_active", lambda s: False)


# --- the break detector -----------------------------------------------------------------------

def test_break_needs_consecutive_closes_beyond_the_level():
    assert cond._break_held([99.0, 100.5, 101.0], LEVEL, "long") is True
    assert cond._break_held([99.0, 101.0, 99.5], LEVEL, "long") is False   # closed back inside
    assert cond._break_held([101.0], LEVEL, "long") is False               # only one close


def test_a_wick_through_the_level_is_not_a_break():
    """Closes only — a wick past a level is the fake break itself."""
    assert cond._break_held([99.0, 99.4, 99.8], LEVEL, "long") is False


def test_short_break_is_measured_downward():
    assert cond._break_held([101.0, 99.5, 99.0], LEVEL, "short") is True
    assert cond._break_held([101.0, 99.5, 100.5], LEVEL, "short") is False


def test_failed_break_detects_a_close_back_through():
    assert cond._break_failed([99.5], LEVEL, "long") is True
    assert cond._break_failed([101.0], LEVEL, "long") is False
    assert cond._break_failed([100.5], LEVEL, "short") is True


# --- stage 1: nothing fires before the break ---------------------------------------------------

def test_price_at_the_limit_does_NOT_fire_before_the_break(db_session, monkeypatch):
    """The whole point. Price is sitting right at the entry, but the level never broke — so this is
    just 'buying under resistance', the opposite of the intended trade."""
    s = _arm(db_session)
    _market(monkeypatch, [99.0] * 5 + [99.9, RETEST])
    out = cond.check_conditional_setups(db_session)

    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed"
    assert s.break_confirmed_at is None
    assert "waiting for" in s.last_note and "BREAK" in s.last_note


def test_break_is_recorded_but_does_not_fire_on_the_breakout_bar(db_session, monkeypatch):
    """Stage 1 completing is not an entry — the retest is a separate event."""
    s = _arm(db_session)
    _market(monkeypatch, [99.0] * 5 + [100.6, 101.2])
    out = cond.check_conditional_setups(db_session)

    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed"
    assert s.break_confirmed_at is not None            # stage 1 done
    assert "RETEST" in s.last_note


# --- stage 2: the retest ------------------------------------------------------------------------

def test_retest_after_a_confirmed_break_reaches_the_trigger(db_session, monkeypatch):
    """Break already confirmed; price comes back to the level -> the limit is live and triggers."""
    s = _arm(db_session, break_confirmed_at=NOW - timedelta(minutes=30))
    _market(monkeypatch, [101.5, 101.0, 100.05])       # pulled back onto the level
    monkeypatch.setattr(cond, "_fire", lambda session, setup, ref: 1)

    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1


def test_price_still_above_the_retest_keeps_waiting(db_session, monkeypatch):
    """Broke and ran without coming back — no fill. This is the cost of the strategy, not a bug."""
    s = _arm(db_session, break_confirmed_at=NOW - timedelta(minutes=30))
    _market(monkeypatch, [101.5, 102.0, 103.0])
    out = cond.check_conditional_setups(db_session)

    assert out["triggered"] == 0
    db_session.refresh(s)
    assert s.status == "armed"


def test_failed_break_cancels_the_setup(db_session, monkeypatch):
    """Confirmed break, then price closed back UNDER the level — the premise is void."""
    s = _arm(db_session, break_confirmed_at=NOW - timedelta(minutes=30))
    _market(monkeypatch, [101.5, 100.8, 99.2])
    out = cond.check_conditional_setups(db_session)

    db_session.refresh(s)
    assert s.status == "cancelled"
    assert out["invalidated"] == 1
    assert "failed break" in s.last_note


# --- ordinary arms are untouched ----------------------------------------------------------------

def test_setup_without_a_break_level_behaves_as_before(db_session, monkeypatch):
    """One-stage arms must not be affected by any of this."""
    s = _arm(db_session, break_level=None, order_type="buy_stop", trigger_price=101.0)
    _market(monkeypatch, [101.5, 101.6, 101.7])
    monkeypatch.setattr(cond, "_fire", lambda session, setup, ref: 1)
    monkeypatch.setattr(cond, "_break_confirmed", lambda broker, setup: True)

    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1


def test_arm_conditional_stores_the_break_level(db_session):
    s = cond.arm_conditional(
        db_session, symbol="DE30m", asset_class="index", timeframe="1h", direction="long",
        order_type="buy_limit", trigger_price=RETEST, stop_loss=STOP, take_profit=106.0,
        confidence=0.6, rr=2.0, source="manual", break_level=LEVEL)
    assert s is not None
    assert s.break_level == LEVEL
    assert s.break_confirmed_at is None


# --- the conversion is shared by EVERY break arm -------------------------------------------------

def test_all_break_arms_share_one_conversion():
    """The retest transform lives in `_as_retest` and is applied by every break-style builder, so
    they can't drift apart. A plain stop arm in, a two-stage limit arm out."""
    from app.agents.orchestrator import _as_retest
    from app.models.schemas import ConditionalSuggestion

    stop_arm = ConditionalSuggestion(order_type="buy_stop", trigger_price=101.2, stop_loss=99.4,
                                     take_profit=107.0, confidence=0.6, rr=3.2)
    out = _as_retest(stop_arm, 101.0, 2.0, 100.0)
    assert out.order_type == "buy_limit"
    assert out.break_level == 101.0
    assert out.trigger_price < stop_arm.trigger_price     # entry AT the level, not above it
    assert out.stop_loss == stop_arm.stop_loss            # stop unchanged
    assert out.rr > stop_arm.rr                           # ...so the payoff improves


def test_conversion_is_skipped_when_it_would_be_invalid():
    """If the retest price would land the wrong side of the stop, keep the original stop arm rather
    than emit something unsizeable."""
    from app.agents.orchestrator import _as_retest
    from app.models.schemas import ConditionalSuggestion

    # Stop sits ABOVE the level, so a long retest at the level would be below its own stop.
    bad = ConditionalSuggestion(order_type="buy_stop", trigger_price=101.2, stop_loss=101.5,
                                take_profit=107.0, confidence=0.6, rr=3.2)
    assert _as_retest(bad, 101.0, 2.0, 100.0) is bad
    assert _as_retest(None, 101.0, 2.0, 100.0) is None
    # No ATR -> nothing to size the buffer from.
    ok = ConditionalSuggestion(order_type="buy_stop", trigger_price=101.2, stop_loss=99.4,
                               take_profit=107.0, confidence=0.6, rr=3.2)
    assert _as_retest(ok, 101.0, None, 100.0) is ok


def test_resumption_arm_also_becomes_a_retest():
    """The pullback-resumption arm breaks a SWING rather than an S/R level, but it's the same trade
    shape and gets the same treatment."""
    from app.agents.orchestrator import _conditional_resumption
    from app.models.enums import Direction

    ind = {"swing_high": 102.0, "swing_low": 98.0}
    plain = _conditional_resumption(Direction.LONG, 100.0, ind, 2.0, [110.0], 0.6, False)
    conv = _conditional_resumption(Direction.LONG, 100.0, ind, 2.0, [110.0], 0.6, True)
    assert plain is not None and plain.order_type == "buy_stop" and plain.break_level is None
    assert conv is not None and conv.order_type == "buy_limit"
    assert conv.break_level == 102.0                      # the swing must break first


def test_toggle_off_restores_plain_break_stops():
    """`break_retest` in the disabled set = arm the thrust, as before."""
    from datetime import datetime, timezone

    from app.agents.orchestrator import _deterministic_decision
    from app.backtest.simulator import _neutral_fundamental
    from app.models.enums import AssetClass
    from tests.test_level_break_arm import _tech

    tech = _tech(d1_res=[101.0])
    args = ("X", AssetClass.STOCK, "1h", tech, _neutral_fundamental("X"),
            datetime.now(timezone.utc))
    on = _deterministic_decision(*args)
    off = _deterministic_decision(*args, disable=frozenset({"break_retest"}))
    assert on.conditional.order_type == "buy_limit" and on.conditional.break_level == 101.0
    assert off.conditional.order_type == "buy_stop" and off.conditional.break_level is None


# --- conviction gate on the auto-open path -------------------------------------------------------

def _fire_ready(session, monkeypatch, *, confidence, min_arm_conf):
    """An armed setup at its trigger with every re-check passing, so only the conviction gate
    decides whether it opens by itself or asks first."""
    from app.agents.hybrid import get_or_create_hybrid_config
    from app.models.enums import RiskDecisionType
    from app.models.schemas import RiskDecision

    cfg = get_or_create_hybrid_config(session)
    cfg.min_arm_confidence = min_arm_conf
    session.commit()

    s = _arm(session, break_level=None, order_type="buy_stop", trigger_price=101.0,
             confidence=confidence, auto_execute=True)
    # Only just past the trigger — far enough beyond and the anti-chase guard declines it first,
    # which would test the wrong thing.
    _market(monkeypatch, [101.05, 101.1, 101.15])
    monkeypatch.setattr(cond, "_break_confirmed", lambda broker, setup: True)
    # `_fire` imports these lazily, so patch them at their source modules.
    import app.execution.executor as executor
    import app.risk.service as risk_service

    monkeypatch.setattr(cond, "_reread_technical", lambda session, setup: None)
    monkeypatch.setattr(
        risk_service, "assess",
        lambda session, proposal, **kw: RiskDecision(
            decision=RiskDecisionType.APPROVED, approved=True, reason="ok",
            symbol=proposal.symbol, approved_qty=0.1, risk_amount=50.0))

    opened: list[str] = []

    def fake_exec(session, record):
        from app.models.enums import ProposalStatus
        opened.append(record.symbol)
        record.status = ProposalStatus.EXECUTED.value
        session.commit()

    monkeypatch.setattr(executor, "execute_proposal", fake_exec)
    return s, opened


def test_high_conviction_arm_opens_itself(db_session, monkeypatch):
    s, opened = _fire_ready(db_session, monkeypatch, confidence=0.70, min_arm_conf=0.62)
    cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert opened == ["DE30m"]                       # auto-executed


def test_low_conviction_arm_asks_instead_of_opening(db_session, monkeypatch):
    """The point: a marginal setup becomes YOUR decision, not an automatic trade."""
    s, opened = _fire_ready(db_session, monkeypatch, confidence=0.55, min_arm_conf=0.62)
    cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    assert opened == []                              # nothing opened
    assert s.status == "triggered"
    assert "queued for your approval" in s.last_note
    assert "under the 62% auto-open bar" in s.last_note


def test_low_conviction_arm_is_not_rejected(db_session, monkeypatch):
    """It must reach the approval queue — silently killing a valid setup would be worse."""
    from app.models.db import TradeProposalRecord
    from app.models.enums import ProposalStatus

    s, _ = _fire_ready(db_session, monkeypatch, confidence=0.55, min_arm_conf=0.62)
    cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    rec = db_session.get(TradeProposalRecord, s.result_proposal_id)
    assert rec is not None and rec.status == ProposalStatus.PENDING_APPROVAL.value


def test_gate_at_zero_keeps_the_old_behaviour(db_session, monkeypatch):
    s, opened = _fire_ready(db_session, monkeypatch, confidence=0.30, min_arm_conf=0.0)
    cond.check_conditional_setups(db_session)
    assert opened == ["DE30m"]


# --- journal attribution -------------------------------------------------------------------------

def test_armed_source_records_who_armed_it():
    """`Position.source` says how a trade came about, and "the auto-pilot armed and fired it" is a
    different answer from "I picked this level". Both used to record plain `armed`."""
    from types import SimpleNamespace

    assert cond._armed_source(SimpleNamespace(source="hybrid")) == "armed_hybrid"
    assert cond._armed_source(SimpleNamespace(source="manual")) == "armed_manual"
    # Anything unexpected keeps the generic label rather than inventing one.
    assert cond._armed_source(SimpleNamespace(source="scanner")) == "armed"
    assert cond._armed_source(SimpleNamespace(source="")) == "armed"


def test_opened_arm_is_attributed_to_the_hybrid(db_session, monkeypatch):
    from app.models.db import TradeProposalRecord

    s, opened = _fire_ready(db_session, monkeypatch, confidence=0.70, min_arm_conf=0.62)
    cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    rec = db_session.get(TradeProposalRecord, s.result_proposal_id)
    assert opened == ["DE30m"]
    assert rec.source == "armed_hybrid"       # not plain "armed", and not "hybrid"


def test_manually_armed_trade_is_attributed_to_you(db_session, monkeypatch):
    from app.models.db import TradeProposalRecord

    s, _ = _fire_ready(db_session, monkeypatch, confidence=0.70, min_arm_conf=0.62)
    s.source = "manual"
    s.auto_execute = False                     # Mode A: queues instead of opening
    db_session.commit()

    cond.check_conditional_setups(db_session)
    db_session.refresh(s)
    rec = db_session.get(TradeProposalRecord, s.result_proposal_id)
    assert rec.source == "armed_manual"


# --- stage 2 is a ZONE + a CONFIRMING close, not a limit fill -------------------------------------

def test_zone_scales_with_the_setup_risk():
    """A band, not a line: price rarely returns to an exact price, so a limit on one mostly misses."""
    assert cond._retest_zone(100.0, 98.0) == 2.0 * cond._RETEST_ZONE_FRAC
    assert cond._retest_zone(100.0, None) > 0          # no stop -> small fallback band
    assert cond._retest_zone(100.0, 100.0) > 0         # degenerate stop -> fallback, never zero


def test_touching_the_zone_alone_does_not_confirm():
    """THE POINT. A limit would have filled here. Price came back but has NOT closed back above the
    level, so the retest may still be failing — commit nothing yet."""
    bars = [SimpleNamespace(close=99.5, low=99.2, high=100.4)]
    ok, why = cond._retest_confirmed(bars, LEVEL, "long", 1.0)
    assert ok is False
    assert "not yet holding" in why


def test_zone_touch_plus_close_back_above_confirms():
    bars = [
        SimpleNamespace(close=100.6, low=100.4, high=100.8),   # broke away
        SimpleNamespace(close=99.9, low=99.6, high=100.7),     # dipped INTO the zone
        SimpleNamespace(close=100.4, low=99.9, high=100.5),    # closed back above -> defended
    ]
    ok, _ = cond._retest_confirmed(bars, LEVEL, "long", 1.0)
    assert ok is True


def test_no_zone_touch_means_no_entry():
    """Broke and ran without coming back — the trade is missed, by design."""
    bars = [SimpleNamespace(close=104.0, low=103.5, high=104.5)] * 3
    ok, why = cond._retest_confirmed(bars, LEVEL, "long", 1.0)
    assert ok is False and "hasn't come back" in why


def test_short_confirmation_is_mirrored():
    bars = [
        SimpleNamespace(close=99.4, low=99.2, high=99.6),      # broke down
        SimpleNamespace(close=100.1, low=99.5, high=100.4),    # popped INTO the zone
        SimpleNamespace(close=99.6, low=99.4, high=100.2),     # closed back below -> defended
    ]
    ok, _ = cond._retest_confirmed(bars, LEVEL, "short", 1.0)
    assert ok is True
    # Still above the level on the close -> not confirmed.
    weak = [SimpleNamespace(close=100.3, low=99.6, high=100.5)]
    assert cond._retest_confirmed(weak, LEVEL, "short", 1.0)[0] is False


def test_failed_retest_never_opens_the_trade(db_session, monkeypatch):
    """The scenario that motivated this: price retests, then keeps going against the arm. A limit
    would already be filled and losing; here nothing opened."""
    s = _arm(db_session, break_confirmed_at=NOW - timedelta(minutes=30))
    _market(monkeypatch,
            closes=[100.8, 100.2, 99.7],          # into the zone, then closing BELOW the level
            lows=[100.5, 99.8, 99.3],
            highs=[101.0, 100.6, 100.1])
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 0


def test_confirmed_retest_opens_and_sizes_off_the_real_price(db_session, monkeypatch):
    """Waiting for the confirming close means the fill is above the level — sizing must use THAT,
    not the stale limit price, or the risk to the stop is understated."""
    s = _arm(db_session, break_confirmed_at=NOW - timedelta(minutes=30))
    _market(monkeypatch,
            closes=[100.9, 100.05, 100.45],
            lows=[100.6, 99.85, 100.0],           # bar 2 dipped into the zone
            highs=[101.1, 100.5, 100.6])
    seen: dict = {}
    monkeypatch.setattr(cond, "_fire",
                        lambda session, setup, ref: (seen.update(ref=ref), 1)[1])
    out = cond.check_conditional_setups(db_session)
    assert out["triggered"] == 1
    assert seen["ref"] == 100.45                  # the confirming close, not trigger_price
