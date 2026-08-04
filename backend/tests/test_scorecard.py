"""Per-symbol scorecard — the system grading its own closed trades.

The point of this module is that it PREDICTS NOTHING. Entry filters guess from history and four of
them in a row measured better in-sample and worse out-of-sample; this just counts what already
happened. So the tests are about one question: is the verdict honest about how much evidence there
actually is?

Classification is on EXPECTANCY IN R, never win rate — a symbol winning 35% with 3R winners is
excellent, one winning 60% with 0.3R winners bleeds. Win rate is reported, not judged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.state import get_or_create_risk_config
from app.models.db import Position, WatchItem
from app.models.enums import PositionStatus
from app.risk.scorecard import (
    DISABLE,
    LEARNING,
    PROVEN,
    WATCHING,
    WEAK,
    apply_scorecard,
    build_scorecard,
)

NOW = datetime.now(timezone.utc)


def _trade(session, symbol, r, *, risk=100.0, ac="index", days_ago=1):
    """One closed trade with a realised R-multiple of ``r``."""
    session.add(Position(
        symbol=symbol, asset_class=ac, direction="long", qty=0.1, entry_price=100.0,
        stop_loss=99.0, take_profit=103.0, status=PositionStatus.CLOSED.value,
        last_price=100.0, realized_pnl=r * risk, risk_amount=risk,
        opened_at=NOW - timedelta(days=days_ago, hours=1),
        closed_at=NOW - timedelta(days=days_ago)))


def _many(session, symbol, r, n, **kw):
    for i in range(n):
        _trade(session, symbol, r, days_ago=i + 1, **kw)
    session.commit()


def _score(session, symbol, **kw):
    card = build_scorecard(session, **kw)
    return next(s for s in card.scores if s.symbol == symbol)


# --- not enough evidence -------------------------------------------------------------------

def test_too_few_trades_is_learning(db_session):
    _many(db_session, "DE30m", -1.0, 10)          # badly losing, but only 10 trades
    s = _score(db_session, "DE30m", min_trades=30)
    assert s.verdict == LEARNING
    assert "30 trades needed" in s.reason


def test_min_trades_threshold_is_respected(db_session):
    _many(db_session, "DE30m", -1.0, 25)
    assert _score(db_session, "DE30m", min_trades=30).verdict == LEARNING
    assert _score(db_session, "DE30m", min_trades=20).verdict == DISABLE


# --- clear verdicts ------------------------------------------------------------------------

def test_consistently_losing_symbol_is_condemned(db_session):
    _many(db_session, "ETHUSDm", -0.8, 35, ac="crypto")
    s = _score(db_session, "ETHUSDm", min_trades=30)
    assert s.verdict == DISABLE and s.significant is True
    assert "lost 0.80R" in s.reason and "35 trades" in s.reason


def test_consistently_winning_symbol_is_proven(db_session):
    _many(db_session, "JP225m", 0.8, 35)
    s = _score(db_session, "JP225m", min_trades=30)
    assert s.verdict == PROVEN and s.significant is True


def test_noisy_breakeven_symbol_is_not_condemned(db_session):
    """Big swings averaging ~0 must NOT be called broken — that's the false positive to avoid."""
    for i in range(40):
        _trade(db_session, "HK50m", 2.0 if i % 2 else -2.0, days_ago=i + 1)
    db_session.commit()
    s = _score(db_session, "HK50m", min_trades=30)
    assert s.verdict == WATCHING and s.significant is False


def test_mild_loser_is_weak_not_disabled(db_session):
    """Losing, but inside what luck could explain -> warn, don't condemn."""
    for i in range(40):
        _trade(db_session, "XAUUSDm", [-2.0, 1.9, -1.0, 1.0][i % 4], days_ago=i + 1, ac="metal")
    db_session.commit()
    s = _score(db_session, "XAUUSDm", min_trades=30)
    assert s.verdict in (WEAK, WATCHING)
    assert s.significant is False


# --- the measure ---------------------------------------------------------------------------

def test_low_win_rate_with_big_winners_is_not_condemned(db_session):
    """35% win rate at +3R is a GOOD symbol. Judging on win rate would wrongly condemn it.

    Note it lands on WATCHING rather than PROVEN: with winners this large the swing between trades
    is wide, so 40 trades genuinely isn't enough to rule out luck. That's the intended behaviour —
    the verdict tracks how much evidence there is, not how nice the average looks."""
    for i in range(40):
        _trade(db_session, "USOILm", 3.0 if i % 3 == 0 else -1.0, days_ago=i + 1, ac="energy")
    db_session.commit()
    s = _score(db_session, "USOILm", min_trades=30)
    assert s.win_rate < 40                        # looks bad by win rate
    assert s.expectancy_r > 0                     # ...but it makes money
    assert s.verdict != DISABLE                   # ...so it is never condemned


def test_high_win_rate_with_tiny_winners_is_condemned(db_session):
    """70% wins at +0.2R against -1R losers is a slow bleed. Win rate would call it great."""
    for i in range(40):
        _trade(db_session, "AUDNZDm", 0.2 if i % 10 < 7 else -1.0, days_ago=i + 1, ac="forex")
    db_session.commit()
    s = _score(db_session, "AUDNZDm", min_trades=30)
    assert s.win_rate > 60                        # looks great by win rate
    assert s.expectancy_r < 0
    assert s.verdict == DISABLE                   # ...but it loses money


def test_trades_without_recorded_risk_cannot_be_scored(db_session):
    for i in range(35):
        db_session.add(Position(
            symbol="ZZZm", asset_class="index", direction="long", qty=0.1, entry_price=100.0,
            status=PositionStatus.CLOSED.value, realized_pnl=-10.0, risk_amount=None,
            closed_at=NOW - timedelta(days=i + 1)))
    db_session.commit()
    s = _score(db_session, "ZZZm", min_trades=30)
    assert s.verdict == LEARNING and "no risk recorded" in s.reason


# --- warn vs act ---------------------------------------------------------------------------

def test_warn_only_by_default(db_session):
    _many(db_session, "ETHUSDm", -0.8, 35, ac="crypto")
    db_session.add(WatchItem(symbol="ETHUSDm", asset_class="crypto", enabled=True))
    db_session.commit()

    card = build_scorecard(db_session, min_trades=30, auto_disable=False)
    assert [s.symbol for s in card.to_disable] == ["ETHUSDm"]     # the warning is raised
    assert apply_scorecard(db_session, card) == []                # ...but nothing is switched off
    assert db_session.scalars(select_watch(db_session, "ETHUSDm")).one().enabled is True


def test_auto_disable_switches_the_symbol_off(db_session):
    _many(db_session, "ETHUSDm", -0.8, 35, ac="crypto")
    db_session.add(WatchItem(symbol="ETHUSDm", asset_class="crypto", enabled=True))
    db_session.commit()

    card = build_scorecard(db_session, min_trades=30, auto_disable=True)
    assert apply_scorecard(db_session, card) == ["ETHUSDm"]
    assert db_session.scalars(select_watch(db_session, "ETHUSDm")).one().enabled is False


def test_auto_disable_leaves_good_symbols_alone(db_session):
    _many(db_session, "JP225m", 0.8, 35)
    db_session.add(WatchItem(symbol="JP225m", asset_class="index", enabled=True))
    db_session.commit()
    card = build_scorecard(db_session, min_trades=30, auto_disable=True)
    assert apply_scorecard(db_session, card) == []


def select_watch(session, symbol):
    from sqlalchemy import select
    return select(WatchItem).where(WatchItem.symbol == symbol)


# --- config ---------------------------------------------------------------------------------

def test_defaults_are_warn_only_at_30(db_session):
    cfg = get_or_create_risk_config(db_session)
    assert cfg.scorecard_min_trades == 30
    assert cfg.scorecard_auto_disable is False


def test_route_returns_warnings(db_session):
    from app.api.journal_routes import scorecard as route

    _many(db_session, "ETHUSDm", -0.8, 35, ac="crypto")
    out = route(days=None, apply=False, session=db_session)
    assert out.min_trades == 30 and out.auto_disable is False
    assert any("ETHUSDm" in w for w in out.warnings)


def test_route_apply_is_a_noop_while_warn_only(db_session):
    from app.api.journal_routes import scorecard as route

    _many(db_session, "ETHUSDm", -0.8, 35, ac="crypto")
    db_session.add(WatchItem(symbol="ETHUSDm", asset_class="crypto", enabled=True))
    db_session.commit()
    route(days=None, apply=True, session=db_session)               # apply, but auto-disable is off
    assert db_session.scalars(select_watch(db_session, "ETHUSDm")).one().enabled is True
