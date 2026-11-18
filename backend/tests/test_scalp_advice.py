"""The advisor's read of a scalp (2026-11-17, the user: "the advisor gives me nothing useful, and it compares with the
DAY while we scalp on a small timeframe"): a trade not planned on 1h or above is read from its own numbers - the
odds from the strategy's own past trades at the same R and age (the learner keeps each closed trade's path), what
holding paid from there, how long such trades still took, and the risks that matter for a few minutes (no stop,
news within 45 min, the market closing, a spread eating the room to the stop). No trend, no daily chart. A trade
planned on 1h or above keeps the swing read."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.agents.learning as L
import app.agents.position_advisor as advisor
import app.agents.scalp_advice as SA
from app.data.providers import CalendarEvent
from app.models.db import Position, StrategyTradeFact, TradeProposalRecord
from app.models.schemas import PositionView
from tests.test_learning import NOW as L_NOW
from tests.test_learning import _HistBroker, _Mt5, _strategy


@pytest.fixture(autouse=True)
def _fresh():
    SA.clear_cache()
    advisor._reset_auto_state()
    yield
    SA.clear_cache()


class _Cal:
    def __init__(self, events):
        self._events = events

    def get_events(self, symbol, lookahead_hours=24, include_medium=False):
        return self._events


def _event(mins, importance="high"):
    return CalendarEvent(label="US: CPI", when=datetime.now(timezone.utc) + timedelta(minutes=mins),
                         importance=importance, country="US")


# --------------------------------------------------------------------------- the learner keeps each trade's path

def test_the_learner_keeps_where_each_closed_trade_stood_minute_by_minute(db_session):
    cfg = _strategy(db_session)
    t0 = (L_NOW - timedelta(hours=10)).replace(second=0)   # 1m candles start on the minute

    def price(t):                                       # +0.01 a minute
        return 100 + 0.01 * (t - t0).total_seconds() / 60

    broker = _HistBroker(_Mt5(t0, 10 * 60, price))
    o = (L_NOW - timedelta(hours=5)).replace(second=20, microsecond=0)
    for key, side in (("a", "long"), ("b", "short")):
        db_session.add(StrategyTradeFact(key=key, strategy_id=cfg.id, symbol="BTCUSDm", side=side, opened_at=o,
                                         closed_at=o + timedelta(minutes=33), pnl=10.0, r=0.3, entry=price(o),
                                         risk_dist=1.0, fp={"v": L.FP_VERSION}, whatif={"t0.9|s1": [0.9, 60]}))
    db_session.add(StrategyTradeFact(key="c", strategy_id=cfg.id, symbol="BTCUSDm", side="long", opened_at=o,
                                     closed_at=None, pnl=None, r=None, entry=price(o), risk_dist=1.0,
                                     fp={"v": L.FP_VERSION}))
    db_session.commit()
    done = L.fill_from_history(db_session, cfg, lambda ac: broker, L_NOW)
    assert done == 2                                       # the two closed ones; the open one waits
    rows = {r.key: r for r in db_session.query(StrategyTradeFact).all()}
    long_, short = rows["a"].path, rows["b"].path
    assert [m for m, _r in long_] == [1, 2, 3, 5, 7, 10, 15, 20, 25, 30]          # only while it was open
    # the close of the last 1m candle finished by then: (m - 1) minutes of +0.01 since 20 s into the entry's minute
    for m, r in long_:
        assert abs(r - 0.01 * (m - 1 - 1 / 3)) < 1e-3
    for (m, rl), (_m, rs) in zip(long_, short):
        assert abs(rs - (-rl - 0.02)) < 1e-3              # a sell buys back at the ask: it pays the spread
    assert rows["c"].path is None
    assert L.fill_from_history(db_session, cfg, lambda ac: broker, L_NOW) == 0     # each once
    assert all(f["path"] for f in L.facts_of(db_session, cfg.id) if f["r"] is not None)


def test_a_trade_closed_inside_its_first_minute_has_an_empty_path():
    bars = [SimpleNamespace(ts=datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc), close=101.0)]
    at = datetime(2026, 11, 17, 10, 0, 30, tzinfo=timezone.utc)
    assert L.trade_path(1, 100.0, 1.0, at, at + timedelta(seconds=40), bars, 0.0) == []
    assert L.trade_path(1, 100.0, 0.0, at, at + timedelta(minutes=5), bars, 0.0) is None


# --------------------------------------------------------------------------- the odds

def _moms(n_win, n_loss, r_then=0.4, minute=10, held=25.0):
    return ([([(1.0, 0.0), (float(minute), r_then)], 1.0, held)] * n_win
            + [([(1.0, 0.0), (float(minute), r_then)], -1.0, held)] * n_loss)


def test_the_odds_come_from_past_trades_at_the_same_r_and_age():
    h = SA.history_odds(_moms(30, 10), age_min=9, r_now=0.45)
    assert h["n"] == 40 and h["won"] == 30
    assert h["hold_edge"] == pytest.approx((30 * 0.6 + 10 * -1.4) / 40, abs=1e-3)
    assert SA.history_odds(_moms(30, 10), age_min=30, r_now=0.45) is None       # nobody stood there at 30 min
    assert SA.history_odds(_moms(30, 10), age_min=9, r_now=-0.2) is None        # ... nor at -0.2R
    assert SA.history_odds(_moms(12, 3), age_min=9, r_now=0.45) is None         # 15: too few to say
    near = SA.history_odds(_moms(30, 10, r_then=0.58), age_min=9, r_now=0.42)   # 0.16 away: the wider window
    assert near is not None and near["n"] == 40


def test_the_distances_alone_give_the_plain_odds():
    assert SA.plain_odds("long", 100.4, 99.0, 101.0) == pytest.approx(0.7)
    assert SA.plain_odds("short", 99.6, 101.0, 99.0) == pytest.approx(0.7)
    assert SA.plain_odds("long", 98.0, 99.0, 101.0) == 0.0                       # past the stop: clamped
    assert SA.plain_odds("long", 100.0, None, 101.0) is None and SA.plain_odds("long", 100.0, 99.0, None) is None


def test_how_long_trades_this_old_still_took():
    moms = [([(1.0, 0.0)], 1.0, float(h)) for h in range(5, 45)]
    assert SA.minutes_left(moms, 20) == pytest.approx(12.0)
    assert SA.minutes_left(moms, 40) is None                                   # 5 trades went that long


# --------------------------------------------------------------------------- the advice, end to end

def _open_trade(session, sid, *, tf="1m", age_min=10.0, direction="long", stop=99.0, tp=101.0, last=100.4,
                source=None):
    opened = datetime.now(timezone.utc) - timedelta(minutes=age_min)
    session.add(TradeProposalRecord(symbol="BTCUSDm", asset_class="crypto", timeframe=tf, direction=direction,
                                    entry=100.0, stop_loss=99.0 if direction == "long" else 101.0,
                                    take_profit=101.0 if direction == "long" else 99.0, status="executed",
                                    source=source or f"strategy:{sid}", created_at=opened - timedelta(seconds=2)))
    session.add(Position(symbol="BTCUSDm", asset_class="crypto", direction=direction, qty=0.1, entry_price=100.0,
                         stop_loss=stop, take_profit=tp, status="open", source=source or f"strategy:{sid}",
                         opened_at=opened))
    session.commit()
    return PositionView(id=1, symbol="BTCUSDm", asset_class="crypto", direction=direction, qty=0.1, entry_price=100.0,
                        stop_loss=stop, take_profit=tp, status="open", last_price=last, unrealized_pnl=4.0)


def _history(session, sid, n_win, n_loss, r_then=0.4, minute=10):
    t = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    for i in range(n_win + n_loss):
        o = t + timedelta(minutes=40 * i)
        session.add(StrategyTradeFact(key=f"h{i}", strategy_id=sid, symbol="BTCUSDm", side="long", opened_at=o,
                                      closed_at=o + timedelta(minutes=25), pnl=1.0, r=1.0 if i < n_win else -1.0,
                                      entry=100.0, risk_dist=1.0, path=[[1, 0.0], [minute, r_then]]))
    session.commit()


def _run(monkeypatch, session, live, events=(), broker=None):
    monkeypatch.setattr(advisor, "live_broker_positions", lambda s: live)
    monkeypatch.setattr(advisor, "get_calendar_provider", lambda: _Cal(list(events)))
    monkeypatch.setattr(SA, "broker_of", lambda s, p: broker)
    monkeypatch.setattr(advisor, "_position_context", lambda s, p: None)      # the swing read: no broker here
    return advisor.advise_positions(session)


def test_a_strategy_scalp_ahead_is_told_to_let_it_run_with_its_own_odds(db_session, monkeypatch):
    cfg = _strategy(db_session)
    _history(db_session, cfg.id, 30, 10)
    p = _open_trade(db_session, cfg.id)
    [a] = _run(monkeypatch, db_session, [p])
    assert a.style == "scalp" and a.thesis == "unknown" and a.severity == "info"
    assert a.headline == "BTCUSDm — ahead: let it run to the target"
    # 30 of 40 won (75%); the distances say 70%: the figure leans on them like 20 trades -> (30 + 14) / 60
    assert a.r_multiple == 0.4 and a.odds == pytest.approx(44 / 60, abs=1e-3) and a.odds_from == "history"
    assert a.odds_n == 40 and a.odds_plain == pytest.approx(0.7) and a.hold_edge == pytest.approx(0.1, abs=1e-3)
    assert "about 73%" in a.detail and "30 of 40" in a.detail and "distances alone say 70%" in a.detail
    assert "1d" not in a.detail and "daily" not in a.detail.lower() and "trend" not in a.detail.lower()
    assert a.minutes_open == pytest.approx(10.0, abs=0.1) and "Open 10 min" in a.detail
    assert a.minutes_left == pytest.approx(15.0, abs=0.2)          # each past trade took 25 min: 15 more from here
    assert "median 15 more min" in a.detail


def test_a_scalp_with_too_little_history_falls_back_to_the_distances(db_session, monkeypatch):
    cfg = _strategy(db_session)
    _history(db_session, cfg.id, 5, 5)
    p = _open_trade(db_session, cfg.id, last=99.9)
    [a] = _run(monkeypatch, db_session, [p])
    assert a.odds_from == "distance" and a.odds == pytest.approx(0.45) and a.odds_n is None
    assert a.headline == "BTCUSDm — on plan: nothing to do"
    assert "Not enough of this strategy's past trades" in a.detail


def test_behind_the_stop_is_the_plan_unless_history_clearly_says_close(db_session, monkeypatch):
    cfg = _strategy(db_session)
    _history(db_session, cfg.id, 10, 10, r_then=-0.4)
    p = _open_trade(db_session, cfg.id, last=99.6)
    [a] = _run(monkeypatch, db_session, [p])
    assert a.headline == "BTCUSDm — behind: the stop is the plan" and a.severity == "info"
    # trades that stood here almost all lost: closing at -0.4R saved 0.5R a trade, far from luck
    SA.clear_cache()
    db_session.query(StrategyTradeFact).delete()
    _history(db_session, cfg.id, 1, 39, r_then=-0.4)
    [b] = _run(monkeypatch, db_session, [p])
    assert b.severity == "warn" and b.headline == "BTCUSDm — trades like this did better closed here"
    assert "Closing at this point has saved" in b.detail and "a solid edge" in b.detail


def test_only_news_near_enough_to_matter_for_a_scalp_is_flagged(db_session, monkeypatch):
    cfg = _strategy(db_session)
    p = _open_trade(db_session, cfg.id)
    [far] = _run(monkeypatch, db_session, [p], events=[_event(70)])
    assert far.event_label is None and far.severity == "info"              # the swing read flags up to 90 min
    [near] = _run(monkeypatch, db_session, [p], events=[_event(20)])
    assert near.event_label == "US: CPI" and near.severity == "warn" and near.minutes_to_event in (19, 20)
    assert near.headline.startswith("BTCUSDm — high-impact news in about")
    [soft] = _run(monkeypatch, db_session, [p], events=[_event(30, "medium")])
    assert soft.event_label is None and soft.events_soon and "CPI" in soft.events_soon


def test_no_stop_is_danger(db_session, monkeypatch):
    cfg = _strategy(db_session)
    p = _open_trade(db_session, cfg.id, stop=None)
    [a] = _run(monkeypatch, db_session, [p], events=[_event(10)])
    assert a.severity == "danger" and a.headline == "BTCUSDm — no stop set" and "High-impact news" in a.detail


class _Broker:
    def __init__(self, spread=0.01, closes_in=None, kind="weekend"):
        self._spread, self._closes_in, self._kind = spread, closes_in, kind

    def spread(self, symbol):
        return self._spread


def test_the_market_closing_and_a_wide_spread_are_flagged(db_session, monkeypatch):
    cfg = _strategy(db_session)
    p = _open_trade(db_session, cfg.id, last=99.5)                       # 0.5 of room left to the stop
    monkeypatch.setattr(SA, "closes_soon", lambda b, s, now: (12, "weekend"))
    [a] = _run(monkeypatch, db_session, [p], broker=_Broker(spread=0.01))
    assert a.severity == "warn" and a.headline == "BTCUSDm — market closes in 12 min" and "the weekend" in a.detail
    monkeypatch.setattr(SA, "closes_soon", lambda b, s, now: None)
    [b] = _run(monkeypatch, db_session, [p], broker=_Broker(spread=0.2))   # 40% of the room
    assert b.severity == "warn" and b.headline == "BTCUSDm — the spread is wide right now" and "40%" in b.detail
    [c] = _run(monkeypatch, db_session, [p], broker=_Broker(spread=0.05))  # 10%: fine
    assert c.severity == "info"


def test_a_stop_locked_in_profit_is_not_a_spread_worry(db_session, monkeypatch):
    cfg = _strategy(db_session)
    p = _open_trade(db_session, cfg.id, stop=100.3, last=100.4)
    monkeypatch.setattr(SA, "closes_soon", lambda b, s, now: None)
    [a] = _run(monkeypatch, db_session, [p], broker=_Broker(spread=0.09))
    assert a.severity == "info"


def test_which_trades_get_the_scalp_read(db_session, monkeypatch):
    cfg = _strategy(db_session)
    p = _open_trade(db_session, cfg.id, tf="1h", source="hybrid")
    [swing] = _run(monkeypatch, db_session, [p])
    assert swing.style == "swing"                                        # planned on 1h: the old read, unchanged
    db_session.query(TradeProposalRecord).delete()
    db_session.query(Position).delete()
    db_session.commit()
    m = _open_trade(db_session, cfg.id, tf="manual", source="manual")   # a desk ticket
    assert advisor._trade_style(db_session, m, db_session.query(Position).one()) == "scalp"
    phone = PositionView(id=9, symbol="EURUSDm", asset_class="forex", direction="long", qty=1.0, entry_price=1.1,
                         status="open", last_price=1.1, unrealized_pnl=0.0)
    assert advisor._trade_style(db_session, phone, None) == "scalp"          # no plan at all
    assert advisor._trade_style(None, phone, None) == "swing"                # (no database: as before)


def test_an_old_plan_on_the_same_pair_is_not_this_trades(db_session, monkeypatch):
    cfg = _strategy(db_session)
    p = _open_trade(db_session, cfg.id, tf="1h", source="hybrid")
    row = db_session.query(Position).one()
    row.opened_at = datetime.now(timezone.utc) - timedelta(hours=3)      # the 1h plan is 2 h 50 min older than it
    db_session.commit()
    assert advisor._trade_style(db_session, p, row) == "scalp"
    [a] = _run(monkeypatch, db_session, [p])
    assert a.style == "scalp" and a.r_multiple is None                  # no plan: no R, the distances still answer
    assert a.odds_from == "distance" and a.headline == "BTCUSDm — stop and target set, nothing urgent"
