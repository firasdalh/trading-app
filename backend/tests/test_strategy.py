"""Custom strategies (app/agents/strategy.py): the conditions read closed bars the right way round,
the rule groups combine as OR-of-ANDs, exits are judged against the trade, and the engine only ever
trades a paper account, once per bar, through the Risk Manager and the executor."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.agents.strategy as st
from app.agents.indicators import adx_series
from app.models.db import Position, StrategyConfig, TradeProposalRecord
from app.models.enums import PositionStatus, ProposalStatus, RiskDecisionType
from app.models.schemas import Candle, OHLCVSeries, RiskDecision

NOW = datetime(2026, 11, 2, 12, 0, 30, tzinfo=timezone.utc)


def _candles(closes: list[float], tf: str = "1m", end: datetime = NOW) -> list[Candle]:
    """Closed bars ending one bar before ``end`` (so none is still forming)."""
    secs = st.TF_SECONDS[tf]
    start = end - timedelta(seconds=secs * (len(closes) + 1))
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) + 0.05, min(prev, c) - 0.05
        out.append(Candle(ts=start + timedelta(seconds=secs * i), open=prev, high=hi, low=lo, close=c, volume=100))
        prev = c
    return out


def _bars(closes, tf="1m"):
    return st.Bars(tf, _candles(closes, tf))


RISING = [100 + 0.3 * i for i in range(80)]
FALLING = [130 - 0.3 * i for i in range(80)]
CROSS_UP = [100.0] * 50 + [101.0, 103.0, 106.0]


def _cond(type_, tf="1m", **params):
    return {"type": type_, "tf": tf, "params": params}


def test_closed_only_drops_the_forming_bar():
    bars = _candles([1, 2, 3])
    forming = bars + [Candle(ts=NOW - timedelta(seconds=20), open=3, high=3, low=3, close=3, volume=1)]
    assert len(st.closed_only(forming, "1m", NOW)) == 3
    assert len(st.closed_only(bars, "1m", NOW)) == 3


def test_ema_cross_up_is_a_buy_not_a_sell():
    b = _bars(CROSS_UP)
    p = st.clean_params(st.CATALOG["ema_cross"], {"fast": 3, "slow": 8, "within": 3})
    ok, detail = st._c_ema_cross(p, 1, b, st.Ctx(now=NOW))
    assert ok is True and "ago" in detail
    assert st._c_ema_cross(p, -1, b, st.Ctx(now=NOW))[0] is False
    # a cross further back than "within" no longer counts
    p1 = st.clean_params(st.CATALOG["ema_cross"], {"fast": 3, "slow": 8, "within": 1})
    assert st._c_ema_cross(p1, 1, b, st.Ctx(now=NOW))[0] is False


def test_supertrend_rsi_and_trend_read_a_rising_market_as_a_buy():
    b = _bars(RISING)
    ctx = st.Ctx(now=NOW)
    sp = st.clean_params(st.CATALOG["supertrend"], {})
    assert st._c_supertrend(sp, 1, b, ctx)[0] is True and st._c_supertrend(sp, -1, b, ctx)[0] is False
    assert st._c_trend({}, 1, b, ctx)[0] is True and st._c_trend({}, -1, b, ctx)[0] is False
    # A zigzag with an upward drift, ending on an up bar: the RSI turns up on the last bar. (A series
    # that only ever rises has an RSI stuck at 100, which can't rise.)
    zig = _bars([100 + 0.3 * i + (0.5 if i % 2 else -0.5) for i in range(80)])
    rp = st.clean_params(st.CATALOG["rsi_rising"], {"period": 7, "bars": 1, "limit": 100})
    assert st._c_rsi_rising(rp, 1, zig, ctx)[0] is True and st._c_rsi_rising(rp, -1, zig, ctx)[0] is False


def test_rsi_series_matches_the_single_value_rsi():
    from app.agents.indicators import rsi

    closes = [100 + ((i * 7) % 11) - 5 + 0.1 * i for i in range(60)]
    assert abs(st._rsi_series(closes, 14)[-1] - rsi(closes, 14)) < 0.01


def test_groups_are_or_of_ands():
    data = {"1m": _bars(RISING)}
    ctx = st.Ctx(now=NOW, base_tf="1m")
    true_c = _cond("trend")                                # rising: the trend is up
    false_c = _cond("ema_above", fast=50, slow=20)          # ...so EMA 50 is NOT above EMA 20
    assert st.evaluate([[true_c, false_c]], 1, data, ctx, "long")["ok"] is False
    ev = st.evaluate([[true_c, false_c], [true_c]], 1, data, ctx, "long")
    assert ev["ok"] is True and ev["group"] == 1


def test_exit_rules_are_judged_against_the_trade():
    """'SuperTrend turns against the trade' fires for a long in a falling market, not a rising one."""
    ctx = st.Ctx(now=NOW, base_tf="1m")
    rule = [[_cond("supertrend")]]
    assert st.evaluate(rule, 1, {"1m": _bars(FALLING)}, ctx, "exit")["ok"] is True     # long, market down
    assert st.evaluate(rule, 1, {"1m": _bars(RISING)}, ctx, "exit")["ok"] is False     # long, market up
    assert st.evaluate(rule, -1, {"1m": _bars(RISING)}, ctx, "exit")["ok"] is True     # short, market up


def test_two_closes_beyond_ema_against_a_long():
    closes = RISING[:-2] + [RISING[-3] - 4, RISING[-3] - 5]
    ev = st.evaluate([[_cond("price_ema", period=10, bars=2)]], 1, {"1m": _bars(closes)},
                     st.Ctx(now=NOW, base_tf="1m"), "exit")
    assert ev["ok"] is True


def test_a_condition_without_candles_never_holds():
    ev = st.evaluate([[_cond("trend", tf="5m")]], 1, {"1m": _bars(RISING)}, st.Ctx(now=NOW), "long")
    assert ev["ok"] is False and ev["groups"][0]["conditions"][0]["ok"] is None


# ----------------------------------------------------------------- the engine, with a fake broker

class _Quote:
    def __init__(self, price):
        self.price = price


class _Broker:
    name = "fake-strategy"

    def __init__(self, closes, paper=True):
        self.closes, self._paper = closes, paper

    @property
    def is_paper(self):
        return self._paper

    def get_ohlcv(self, symbol, timeframe="1m", limit=200):
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=_candles(self.closes, timeframe))

    def scalp_spec(self, symbol):
        last = self.closes[-1]
        return {"bid": last - 0.05, "ask": last + 0.05, "spread": 0.1, "digits": 2, "stops_level": 0.0}

    def get_quote(self, symbol):
        return _Quote(self.closes[-1])

    def market_open(self, symbol):
        return True


@pytest.fixture
def engine(db_session, monkeypatch):
    from app.data import ohlcv_cache

    ohlcv_cache._cache.clear()
    st._BAR_CACHE.clear()
    st._LAST_LOOK.clear()
    calls = {"assess": [], "exec": [], "close": []}
    holder = {"broker": _Broker(RISING)}
    monkeypatch.setattr("app.brokers.registry.get_broker_for", lambda ac, bm: holder["broker"])
    monkeypatch.setattr(st, "NOW_FOR_TESTS", NOW, raising=False)

    def fake_assess(session, proposal, **kw):
        calls["assess"].append((proposal, kw))
        return RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol=proposal.symbol,
                            approved_qty=0.1, risk_amount=5.0)

    def fake_exec(session, record):
        calls["exec"].append(record)
        record.status = ProposalStatus.EXECUTED.value
        session.add(Position(symbol=record.symbol, asset_class=record.asset_class, direction=record.direction,
                             qty=record.approved_qty, entry_price=record.entry, stop_loss=record.stop_loss,
                             take_profit=record.take_profit, status=PositionStatus.OPEN.value,
                             source=record.source, opened_at=NOW, risk_amount=5.0, initial_risk_amount=5.0))
        session.commit()

    def fake_close(session, pos, broker, price, reason):
        calls["close"].append((pos.id, reason))
        pos.status, pos.closed_at, pos.realized_pnl = PositionStatus.CLOSED.value, NOW, 1.0
        return True

    monkeypatch.setattr("app.risk.service.assess", fake_assess)
    monkeypatch.setattr("app.execution.executor.execute_proposal", fake_exec)
    monkeypatch.setattr("app.execution.monitor._close_position", fake_close)
    monkeypatch.setattr("app.execution.executor.ExecutionBlocked", RuntimeError, raising=False)
    return calls, holder


def _strategy(session, **kw) -> StrategyConfig:
    base = dict(name="test", enabled=True, pairs=[{"symbol": "BTCUSDm", "asset_class": "crypto"}], timeframe="1m",
                sides="both", entry_long=[[_cond("trend")]], entry_short=[], mirror_short=True,
                exit_rules=[[_cond("supertrend")]], risk_pct=0.005, stop_atr=1.5, target_r=2.0, max_trades_day=10,
                cooldown_minutes=0, loss_cooldown_minutes=5)
    base.update(kw)
    cfg = StrategyConfig(**base)
    session.add(cfg)
    session.commit()
    return cfg


def test_a_live_account_is_never_traded_with_its_live_switch_off(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(RISING, paper=False)
    cfg = _strategy(db_session)                                 # the switch is off by default
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and calls["exec"] == []
    assert "live account" in out[0]["note"] and "switch is off" in out[0]["note"]


def test_an_entry_goes_through_the_risk_manager_with_its_stop_and_target(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert out[0].get("opened") == "long"
    prop, kw = calls["assess"][0]
    # scalp mode's cooldowns: no per-pair wait, a 5-min pause after a loss in the same direction
    assert kw == {"override_risk_fraction": 0.005, "override_cooldown_minutes": 0, "override_loss_cooldown_minutes": 5,
                  "override_risk_ceiling": 0.06}
    assert prop.stop_loss < prop.entry < prop.take_profit
    assert abs((prop.take_profit - prop.entry) - 2 * (prop.entry - prop.stop_loss)) < 0.02
    rec = calls["exec"][0]
    assert rec.source == f"strategy:{cfg.id}" and rec.reasoning["strategy_id"] == cfg.id


def test_one_try_per_bar(db_session, engine):
    calls, holder = engine
    cfg = _strategy(db_session, exit_rules=[])
    st.run_strategy(db_session, cfg, now=NOW)
    # the trade it opened is closed elsewhere; the same bar does not re-enter
    for p in db_session.query(Position).all():
        p.status = PositionStatus.CLOSED.value
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert len(calls["assess"]) == 1


def test_the_close_rules_close_its_trade_and_leave_a_manual_one_alone(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)
    cfg = _strategy(db_session, entry_long=[[_cond("rsi_level", period=14, level=99)]])
    mine = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                    stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                    opened_at=NOW - timedelta(hours=3))
    manual = Position(symbol="BTCUSDm", asset_class="crypto", direction="short", qty=0.1, entry_price=110,
                      stop_loss=120, status=PositionStatus.OPEN.value, source="manual",
                      opened_at=NOW - timedelta(hours=3))
    db_session.add_all([mine, manual])
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert [c[0] for c in calls["close"]] == [mine.id]       # SuperTrend red: against its long
    assert "SuperTrend" in calls["close"][0][1]


def test_manual_trades_are_closed_only_when_asked(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)
    manual = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                      stop_loss=100, status=PositionStatus.OPEN.value, source="manual",
                      opened_at=NOW - timedelta(hours=3))
    db_session.add(manual)
    db_session.commit()
    cfg = _strategy(db_session, entry_long=[[_cond("rsi_level", period=14, level=99)]])
    st.run_strategy(db_session, cfg, now=NOW)
    assert calls["close"] == []
    cfg.manage_manual = True
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))
    assert [c[0] for c in calls["close"]] == [manual.id]


def test_an_exit_waits_for_a_bar_closed_after_the_entry(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)
    cfg = _strategy(db_session, entry_long=[[_cond("rsi_level", period=14, level=99)]])
    fresh = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                     stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                     opened_at=NOW - timedelta(seconds=10))     # after the last closed 1m bar
    db_session.add(fresh)
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert calls["close"] == []


def test_the_daily_cap_stops_new_entries(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, max_trades_day=1)
    db_session.add(Position(symbol="ETHUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=1,
                            status=PositionStatus.CLOSED.value, source=f"strategy:{cfg.id}", opened_at=NOW))
    db_session.commit()
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and "daily cap" in out[0]["note"]


def test_a_refused_order_is_not_left_for_approval(db_session, engine, monkeypatch):
    calls, _ = engine

    def blocked(session, record):
        raise RuntimeError("inside a high-impact news window - automated entry not opened")

    monkeypatch.setattr("app.execution.executor.execute_proposal", blocked)
    cfg = _strategy(db_session)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert "news" in out[0]["blocked"]
    rec = db_session.query(TradeProposalRecord).one()
    assert rec.status == ProposalStatus.EXPIRED.value


# ----------------------------------------------------------------- the board's API

def test_the_board_validates_and_saves_stopped(db_session):
    from fastapi import HTTPException

    from app.api.strategy_routes import RunIn, StrategyIn, create_strategy, run_strategy

    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[{"type": "no_such", "tf": "1m"}]])
    with pytest.raises(ValidationError):
        StrategyIn(exit_rules=[[{"type": "spread", "tf": "1m"}]])          # a filter is entry-only
    req = StrategyIn(name="EMA scalp", timeframe="10s",
                     entry_long=[[{"type": "ema_cross", "tf": "10s", "params": {"fast": 10, "slow": 9999}}], []],
                     exit_rules=[[{"type": "supertrend", "tf": "1m"}]])
    assert req.entry_long[0][0].params["slow"] == 400                     # clamped into bounds
    assert len(req.entry_long) == 1                                        # the empty group dropped
    view = create_strategy(req, session=db_session)
    assert view.enabled is False
    with pytest.raises(HTTPException):
        run_strategy(view.id, RunIn(on=True), session=db_session)          # no pair yet


def test_a_sell_sentence_reads_the_mirrored_number():
    c = {"type": "rsi_rising", "tf": "1m", "params": {"period": 7, "bars": 1, "limit": 80}}
    assert "under 80" in st.label(c, "long")
    assert "over 20" in st.label(c, "short") and "100" not in st.label(c, "short")


# ----------------------------------------------------------------- the clock: synchronised to closes

def test_closed_bars_are_read_once_per_bar(db_session):
    st._BAR_CACHE.clear()
    calls = []

    class _Counting(_Broker):
        def get_ohlcv(self, symbol, timeframe="1m", limit=200):
            calls.append(timeframe)
            return super().get_ohlcv(symbol, timeframe, limit)

    from app.data import ohlcv_cache

    ohlcv_cache._cache.clear()
    b = _Counting(RISING)
    t0 = datetime(2026, 11, 2, 12, 0, 2, tzinfo=timezone.utc)         # 2 s after a 5s / 1m close
    st.load_bars(b, "BTCUSDm", {"5s", "1m"}, t0)
    st.load_bars(b, "BTCUSDm", {"5s", "1m"}, t0 + timedelta(seconds=2))   # same bars: no new read
    assert sorted(calls) == ["1m", "5s"]
    st.load_bars(b, "BTCUSDm", {"5s", "1m"}, t0 + timedelta(seconds=5))   # a new 5s bar closed
    assert sorted(calls) == ["1m", "5s", "5s"]


def test_a_pair_is_looked_at_a_second_after_each_close_and_on_the_heartbeat(db_session, monkeypatch):
    st._LAST_LOOK.clear()
    looks = []
    monkeypatch.setattr(st, "run_strategy", lambda session, cfg, now=None, pairs=None: looks.append(now) or [])
    _strategy(db_session, timeframe="1m", entry_long=[[_cond("trend", tf="1m")]], exit_rules=[])
    t0 = datetime(2026, 11, 2, 12, 0, 1, 500000, tzinfo=timezone.utc)   # 1.5 s after a 1m close
    for dt in (0, 1, 2, 3):                                                # the same minute: one look
        st.strategy_tick(db_session, now=t0 + timedelta(seconds=dt))
    assert len(looks) == 1
    st.strategy_tick(db_session, now=t0 + timedelta(seconds=16))           # the heartbeat
    assert len(looks) == 2
    st.strategy_tick(db_session, now=t0 + timedelta(seconds=60))           # the next 1m close (+1.5 s)
    assert len(looks) == 3
    st.strategy_tick(db_session, now=t0 + timedelta(seconds=60.2))
    assert len(looks) == 3


# ----------------------------------------------------------------- its own cooldowns

def test_a_strategys_own_cooldowns_reach_the_risk_manager_and_the_executor(db_session, engine):
    from app.risk.service import record_cooldowns

    calls, _ = engine
    cfg = _strategy(db_session, cooldown_minutes=12, loss_cooldown_minutes=45)
    st.run_strategy(db_session, cfg, now=NOW)
    _, kw = calls["assess"][0]
    assert kw["override_cooldown_minutes"] == 12 and kw["override_loss_cooldown_minutes"] == 45
    rec = calls["exec"][0]
    assert rec.reasoning["cooldowns"] == {"pair": 12, "loss": 45}
    assert record_cooldowns(rec) == {"override_cooldown_minutes": 12, "override_loss_cooldown_minutes": 45}



def test_a_pair_cools_down_after_any_close_and_says_so(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, cooldown_minutes=10)
    # a WIN on the pair 3 minutes ago, and the user's own trade, not the strategy's: any close counts
    done = Position(symbol="BTCUSDm", asset_class="crypto", direction="short", qty=0.1, entry_price=101.0,
                    status=PositionStatus.CLOSED.value, source="manual", opened_at=NOW - timedelta(minutes=9),
                    closed_at=NOW - timedelta(minutes=3), realized_pnl=4.0)
    db_session.add(done)
    db_session.commit()
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and out[0]["note"] == "cooldown after the last close on this pair - 7 min left"
    # ten minutes after that close the pair is free again, and the order still goes through the Risk Manager
    done.closed_at = NOW - timedelta(minutes=10, seconds=1)
    db_session.commit()
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert out[0].get("opened") == "long" and calls["assess"][0][1]["override_cooldown_minutes"] == 10


def test_no_wait_set_means_no_cooldown(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, cooldown_minutes=0)
    db_session.add(Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=99.0,
                            status=PositionStatus.CLOSED.value, source=f"strategy:{cfg.id}",
                            opened_at=NOW - timedelta(minutes=2), closed_at=NOW - timedelta(seconds=30),
                            realized_pnl=-3.0))
    db_session.commit()
    assert st._cooldown_left(db_session, cfg, "BTCUSDm", NOW) == 0


def test_the_board_sees_until_when_a_pair_waits(db_session):
    """A pair waiting out its cooldown shows as waiting on the widget and the board, not "GO" (asked for
    2026-11-03): the view says until when, for the pairs still waiting only."""
    from app.api.strategy_routes import _view

    cfg = _strategy(db_session, cooldown_minutes=10, pairs=[{"symbol": "BTCUSDm", "asset_class": "crypto"},
                                                            {"symbol": "USOILm", "asset_class": "energy"}])
    assert st.cooldown_until(db_session, cfg, "USOILm") is None and _view(db_session, cfg).cooldown_until == {}
    closed = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.add(Position(symbol="USOILm", asset_class="energy", direction="long", qty=0.21, entry_price=95.87,
                            status=PositionStatus.CLOSED.value, source=f"strategy:{cfg.id}",
                            opened_at=closed - timedelta(minutes=6), closed_at=closed, realized_pnl=-25.2))
    db_session.commit()
    until = st.cooldown_until(db_session, cfg, "USOILm")
    assert until == closed + timedelta(minutes=10)
    assert st._cooldown_left(db_session, cfg, "USOILm", closed + timedelta(minutes=3)) == 7
    assert _view(db_session, cfg).cooldown_until == {"USOILm": until.isoformat()}
    cfg.cooldown_minutes = 0                       # no wait set: nothing to wait for
    db_session.commit()
    assert _view(db_session, cfg).cooldown_until == {}


def test_the_cooldowns_never_go_under_the_confirmed_floors(db_session):
    from app.api.strategy_routes import StrategyIn
    from app.risk.service import strategy_cooldowns

    with pytest.raises(ValidationError):
        StrategyIn(loss_cooldown_minutes=0)          # under the 5-min pause the user confirmed
    with pytest.raises(ValidationError):
        StrategyIn(cooldown_minutes=-1)
    assert StrategyIn().cooldown_minutes == 0 and StrategyIn().loss_cooldown_minutes == 5
    # and the risk layer clamps whatever reaches it (an old row, a hand-edited one)
    assert strategy_cooldowns(0, 0) == {"override_cooldown_minutes": 0, "override_loss_cooldown_minutes": 5}
    assert strategy_cooldowns(None, None) == {"override_cooldown_minutes": 0, "override_loss_cooldown_minutes": 5}
    assert strategy_cooldowns(30, 180) == {"override_cooldown_minutes": 30, "override_loss_cooldown_minutes": 180}


# ----------------------------------------------------------------- room to higher-timeframe levels and zones

def _hl(closes, tf="1h", end=NOW, wick=0.2):
    """Doji bars with a fixed wick, so each swing high / low is exactly its close +/- the wick."""
    secs = st.TF_SECONDS[tf]
    start = end - timedelta(seconds=secs * (len(closes) + 1))
    return [Candle(ts=start + timedelta(seconds=secs * i), open=c, high=c + wick, low=c - wick, close=c, volume=100)
            for i, c in enumerate(closes)]


# Swing highs 110.0 and 110.1 (a double top: one level, two touches), swing lows 100.0 and 107.3.
LEVELS = [104, 105, 106, 107, 108, 109, 109.8, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100.2, 101, 102,
          103, 104, 105, 106, 107, 108, 109, 109.9, 109, 108, 107.5, 108, 108.4, 108.6, 108.8]


def _room(s, bid=108.8, ask=108.9, risk=1.0, **params):
    p = st.clean_params(st.CATALOG["sr_room"], params)
    return st._c_sr_room(p, s, st.Bars("1h", _hl(LEVELS)), st.Ctx(now=NOW, bid=bid, ask=ask, risk=risk))


def test_room_to_a_tested_resistance_blocks_a_buy_right_under_it():
    ok, detail = _room(1, room=1.5, touches=2)
    assert ok is False and detail == "resistance 110 (2 touches) 1.1R above"
    assert _room(1, room=1.0, touches=2)[0] is True                  # 1.1R of room is enough for 1R
    ok, detail = _room(1, room=1.5, touches=3)                        # no level turned price 3 times
    assert ok is True and "no resistance" in detail
    assert _room(1, ask=None, room=1.5)[0] is None                    # no quote: never a hold


def test_a_sell_reads_the_support_under_it():
    ok, detail = _room(-1, room=1.5, touches=1)                       # 108.8 -> 107.3 = 1.5R: just enough
    assert ok is True and detail == "support 107.3 (1 touch) 1.5R below"
    assert _room(-1, room=2.0, touches=1)[0] is False
    assert _room(-1, room=2.0, touches=2)[0] is True                  # neither low was tested twice


def _supply():
    """The order-block tests' sharp fall: a supply zone over the price (tests/test_order_blocks.py)."""
    from tests.test_order_blocks import C, chop

    cs = chop(55, 100.0, 1.0)
    cs.append(C(55, 100.0, 102.0, 99.8, 101.8))           # the last up candle before the fall
    px = 101.8
    for i in range(56, 65):
        o = px
        px -= 1.6
        cs.append(C(i, o, o + 0.05, px - 0.3, px, 3000.0))
    return st.Bars("1h", cs)


def test_a_buy_stays_out_of_supply_and_away_from_it():
    b = _supply()
    zone = next(z for z in b.zones() if z.kind == "bearish")
    p = st.clean_params(st.CATALOG["zone_room"], {"room": 1.0, "strength": 0})

    def read(ask, s=1, params=p):
        return st._c_zone_room(params, s, b, st.Ctx(now=NOW, bid=ask - 0.1, ask=ask, risk=1.0))

    ok, detail = read(zone.mid)
    assert ok is False and detail.startswith("inside supply")
    ok, detail = read(zone.bottom - 0.5)
    assert ok is False and detail.endswith("0.5R above")
    assert read(zone.bottom - 2.0)[0] is True
    strict = st.clean_params(st.CATALOG["zone_room"], {"room": 1.0, "strength": 100})
    assert zone.strength < 100 and read(zone.mid, params=strict) == (True, "no supply zone above")
    assert read(zone.bottom - 0.5, s=-1)[0] is True                   # a sell isn't held back by supply


class _LevelBroker(_Broker):
    """1m candles RISING to 123.7; on 1h a double top at 124.0 / 124.1, just over the price."""

    def get_ohlcv(self, symbol, timeframe="1m", limit=200):
        if timeframe == "1h":
            return OHLCVSeries(symbol=symbol, timeframe="1h", candles=_hl([c + 14 for c in LEVELS]))
        return super().get_ohlcv(symbol, timeframe, limit)


def test_the_engine_waits_when_a_1h_resistance_is_closer_than_the_room(db_session, engine):
    calls, holder = engine
    holder["broker"] = _LevelBroker(RISING)
    cfg = _strategy(db_session, entry_long=[[_cond("trend"), _cond("sr_room", tf="1h", room=1.5, touches=2)]],
                    sides="long", exit_rules=[])
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and out[0]["note"] == "watching - no entry rule holds"
    # 0.25 to the level against a 0.6 stop is about 0.4R: with less room asked for, it buys.
    cfg.entry_long = [[_cond("trend"), _cond("sr_room", tf="1h", room=0.3, touches=2)]]
    db_session.commit()
    st._BAR_CACHE.clear()
    out = st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))
    assert out[0].get("opened") == "long"
    prop, _ = calls["assess"][0]
    base = st.Bars("1m", st.closed_only(_candles(RISING), "1m", NOW))
    one_r = st._stop_distance(db_session, cfg.stop_atr, base, holder["broker"].scalp_spec("BTCUSDm"))
    assert abs((prop.entry - prop.stop_loss) - one_r) < 0.011          # the R it measured is the order's


def test_the_level_filters_read_only_their_timeframes():
    from app.api.strategy_routes import StrategyIn

    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[{"type": "sr_room", "tf": "5m"}]])
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[[{"type": "zone_room", "tf": "10s"}]])
    with pytest.raises(ValidationError):
        StrategyIn(exit_rules=[[{"type": "sr_room", "tf": "1h"}]])      # an entry filter only
    req = StrategyIn(entry_long=[[{"type": "sr_room", "tf": "4h", "params": {"room": 99}}]], stop_atr=2.0)
    assert req.entry_long[0][0].params == {"room": 10, "touches": 2}
    assert req.rules().stop_atr == 2.0
    cat = {c["type"]: c for c in st.catalog()}
    assert cat["sr_room"]["tfs"] == ["1h", "4h", "1d"] and cat["sr_room"]["default_tf"] == "1h"
    assert cat["zone_room"]["default_tf"] == "1h" and cat["ema_cross"]["tfs"] is None
    assert "within 1.5R above" in st.label({"type": "sr_room", "tf": "1h", "params": {}}, "long")
    assert "support" in st.label({"type": "sr_room", "tf": "1d", "params": {}}, "short")


def test_zones_read_further_back_than_the_other_rules(db_session):
    from app.data import ohlcv_cache

    r = st.Rules(timeframe="1m", entry_long=[[_cond("trend"), _cond("zone_room", tf="1h")]])
    assert r.sizes() == {"1m": st._BARS, "1h": st._ZONE_BARS}
    st._BAR_CACHE.clear()
    ohlcv_cache._cache.clear()
    asked = []

    class _Sized(_Broker):
        def get_ohlcv(self, symbol, timeframe="1m", limit=200):
            asked.append((timeframe, limit))
            return super().get_ohlcv(symbol, timeframe, limit)

    b = _Sized(RISING)
    st.load_bars(b, "BTCUSDm", r.tfs(), NOW, r.sizes())
    assert sorted(asked) == [("1h", st._ZONE_BARS), ("1m", st._BARS)]
    st.load_bars(b, "BTCUSDm", {"1h"}, NOW)              # a shorter read is served from the longer one
    assert len(asked) == 2


# ----------------------------------------------------------------- swing structure (HH / HL)

# Swings of 2 bars: lows 100, 102 (HL), 104 (HL); highs 106, 110 (HH). The newest swing is the 104 low,
# confirmed by the 2 bars after it: a higher high, then a higher low.
UPTURN = [110, 108, 106, 104, 102, 100, 102, 104, 106, 104, 102, 104, 106, 108, 110, 108, 106, 104, 106, 108]


def _structure(s, closes=UPTURN, trade=None, **params):
    p = st.clean_params(st.CATALOG["structure"], {"swing": 2, **params})
    return st._c_structure(p, s, st.Bars("1m", _hl(closes, "1m")), st.Ctx(now=NOW, trade=trade))


def test_structure_reads_a_higher_high_then_a_higher_low():
    ok, detail = _structure(1)
    assert ok is True and detail.startswith("HH 110.2 · HL 103.8") and "confirmed 0 bars ago" in detail
    assert _structure(-1)[0] is False
    # the same swings with a newer high: HH and HL, but the HL is no longer the newest swing
    assert _structure(1, closes=UPTURN + [110, 112, 114, 113, 112])[0] is False
    assert _structure(1, within=1)[0] is True and _structure(1, closes=UPTURN + [109, 110], within=1)[0] is False


def test_a_short_closes_at_the_higher_low_made_after_its_entry():
    # Judged for the other side, as evaluate does for a close rule on a short.
    before = Position(symbol="BTCUSDm", direction="short", opened_at=NOW - timedelta(minutes=10))
    after = Position(symbol="BTCUSDm", direction="short", opened_at=NOW - timedelta(seconds=20))
    assert _structure(1, trade=before)[0] is True
    ok, detail = _structure(1, trade=after)
    assert ok is False and "before the entry" in detail
    ev = st.evaluate([[_cond("structure", swing=2)]], -1, {"1m": st.Bars("1m", _hl(UPTURN, "1m"))},
                     st.Ctx(now=NOW, trade=before), "exit")
    assert ev["ok"] is True and "a short closes at the higher low" in ev["groups"][0]["conditions"][0]["label"]


# ----------------------------------------------------------------- the candle agreeing (not just its colour)

def _bar(o, h, l, c, i=0):
    return Candle(ts=NOW - timedelta(minutes=3 - i), open=o, high=h, low=l, close=c, volume=100)


def _clean(s, *bars, **params):
    p = st.clean_params(st.CATALOG["clean_candle"], params)
    return st._c_clean_candle(p, s, st.Bars("1m", list(bars)), st.Ctx(now=NOW))


def test_clean_candle_wants_a_real_body_the_trades_way():
    strong = _bar(100, 101.1, 99.95, 101)                       # green, body 87% of the range
    ok, detail = _clean(1, strong)
    assert ok is True and detail.startswith("green, body 87%")
    assert _clean(-1, strong) == (False, "green candle")
    assert _clean(1, _bar(100, 100.5, 99.5, 100.02))[1] == "doji (body 2% of the range)"
    assert _clean(1, _bar(100, 100.6, 99.6, 100.25))[1] == "spinning top (body 25% of the range)"


def test_a_shooting_star_is_against_a_buy_and_a_hammer_against_a_sell():
    star = _bar(100, 101.2, 99.98, 100.4)                       # green, upper wick 2x the body
    assert _clean(1, star) == (False, "shooting star (wick 2.0x the body)")
    hammer = _bar(100.6, 100.62, 99.4, 100.2)                   # red, lower wick 2x the body
    assert _clean(-1, hammer) == (False, "hammer (wick 2.0x the body)")
    assert _clean(1, hammer)[1] == "red candle"
    assert _clean(1, star, wick=2.5)[0] is True                 # a looser wick allowance lets it through


def test_clean_candle_reads_every_candle_asked_for():
    doji, strong = _bar(100, 100.5, 99.5, 100.02, 0), _bar(100, 101.1, 99.95, 101, 1)
    assert _clean(1, doji, strong, bars=1)[0] is True
    assert _clean(1, doji, strong, bars=2)[1].startswith("doji")


def test_a_strong_candle_against_a_long_closes_it():
    red = _bar(101, 101.05, 99.9, 100)                          # a real red body, no lower wick to speak of
    ev = st.evaluate([[_cond("clean_candle")]], 1, {"1m": st.Bars("1m", [red])}, st.Ctx(now=NOW), "exit")
    assert ev["ok"] is True


def test_a_group_takes_up_to_sixteen_conditions():
    # 10, then 12 on 2026-11-03 for the ATR and spread filters, then 16 the same day: the user's BUY
    # group was full again when the stochastic-with-room rule came.
    from app.api.strategy_routes import StrategyIn, strategy_catalog

    sixteen = [{"type": "trend", "tf": "1m"} for _ in range(16)]
    assert len(StrategyIn(entry_long=[sixteen]).entry_long[0]) == 16
    with pytest.raises(ValidationError):
        StrategyIn(entry_long=[sixteen + [{"type": "trend", "tf": "1m"}]])
    assert strategy_catalog()["max_conditions"] == 16


# ----------------------------------------------------------------- a SuperTrend close rule is a TURN

def test_a_supertrend_close_rule_needs_a_turn_after_the_entry():
    closes = RISING[:60] + [RISING[59] - 0.8 * i for i in range(1, 31)]      # up, then a fall that flips it red
    bars = st.Bars("1m", _candles(closes))
    d = bars.st_dir(10, 2.7)
    assert d[-1] == -1
    k = len(d) - 1
    while d[k - 1] == -1:
        k -= 1                                                              # the first red bar
    p = st.clean_params(st.CATALOG["supertrend"], {"period": 10, "mult": 2.7, "fresh": 0})

    def read(opened_at):
        pos = Position(symbol="BTCUSDm", direction="long", opened_at=opened_at)
        return st._c_supertrend(p, -1, bars, st.Ctx(now=NOW, trade=pos))    # judged against the long

    flip_known = bars.c[k].ts + timedelta(minutes=1)
    assert read(bars.c[k - 5].ts) == (True, "red")                          # green at the entry, red now: a turn
    ok, detail = read(flip_known + timedelta(seconds=30))                   # red already when it was opened
    assert ok is False and "before the entry - not a turn" in detail
    assert st._c_supertrend(p, -1, bars, st.Ctx(now=NOW)) == (True, "red")  # no trade: the plain read


def test_the_board_reads_close_rules_with_the_open_trade(db_session, engine):
    _, holder = engine
    holder["broker"] = _Broker(FALLING)                                     # SuperTrend red throughout
    rules = st.Rules(timeframe="1m", entry_long=[[_cond("trend")]], exit_rules=[[_cond("supertrend")]])
    plain = st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW)
    assert plain["exit_long"]["ok"] is True                                 # no trade: SuperTrend is red
    db_session.add(Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110.0,
                            status=PositionStatus.OPEN.value, source="manual", opened_at=NOW - timedelta(seconds=90)))
    db_session.commit()
    st._BAR_CACHE.clear()
    held = st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW)
    cond = held["exit_long"]["groups"][0]["conditions"][0]
    assert held["exit_long"]["ok"] is False and "before the entry" in cond["detail"]




def test_supertrend_turn_is_confirmed_by_candles_of_its_colour():
    fall = [RISING[59] - 0.8 * i for i in range(1, 13)]                    # red candles: the flip, then more
    closes = RISING[:60] + fall
    bars = st.Bars("1m", _candles(closes))
    d = bars.st_dir(10, 2.7)
    k = len(d) - 1
    while d[k - 1] == -1:
        k -= 1
    after = len(d) - 1 - k                                                  # candles after the red flip
    assert after >= 2
    pos = Position(symbol="BTCUSDm", direction="long", opened_at=bars.c[k - 5].ts)   # opened while green

    def read(hold, series=bars, fresh=0, trade=pos):
        p = st.clean_params(st.CATALOG["supertrend"], {"period": 10, "mult": 2.7, "fresh": fresh, "hold": hold})
        return st._c_supertrend(p, -1, series, st.Ctx(now=NOW, trade=trade))   # judged against the long

    assert read(0) == (True, "red")                                         # 0: close on the turn
    ok, detail = read(2)
    assert ok is True and detail.endswith("confirmed by 2 candles going its way")
    ok, detail = read(after + 1)
    assert ok is False and "waiting for" in detail
    # A fake flip: a green candle after the turn, the line still red - no close on 1 confirming candle,
    # though "close on the turn" (0) still closes.
    bounce = st.Bars("1m", _candles(closes + [fall[-1] + 0.3]))
    assert bounce.st_dir(10, 2.7)[-1] == -1
    ok, detail = read(1, series=bounce)
    assert ok is False and "not confirmed: 1 of the last 1 candle came back up" in detail
    assert read(0, series=bounce)[0] is True
    # ...and when the fall resumes with red candles while the line is still red, that confirms it.
    resumed = st.Bars("1m", _candles(closes + [fall[-1] + 0.3, fall[-1] - 0.5, fall[-1] - 1.2]))
    assert resumed.st_dir(10, 2.7)[-1] == -1
    assert read(2, series=resumed)[0] is True
    # A green candle that still closed LOWER than the one before (it opened lower): the price kept going
    # down, which confirms the flip too.
    last = bars.c[-1]
    gap = Candle(ts=last.ts + timedelta(minutes=1), open=last.close - 1.0, high=last.close - 0.5,
                 low=last.close - 1.1, close=last.close - 0.6, volume=100)
    lower = st.Bars("1m", list(bars.c) + [gap])
    assert lower.st_dir(10, 2.7)[-1] == -1
    assert read(1, series=lower)[0] is True
    # As an entry rule (no trade): a green flip confirmed by green candles reads the same, mirrored.
    assert read(2, trade=None)[0] is True
    assert "confirmed by 2 candle(s) after the flip" in st.label(
        {"type": "supertrend", "tf": "1m", "params": {"hold": 2}}, "exit")



def test_structure_waits_for_confirming_candles():
    # UPTURN ends on the higher low's confirming bar; one more candle either keeps going up or comes back.
    assert _structure(1, hold=1)[1].endswith("waiting for 1 confirming candle")
    ok, detail = _structure(1, closes=UPTURN + [109], hold=1)               # a higher close: going up
    assert ok is True and detail.endswith("+ 1 candle going its way")
    ok, detail = _structure(1, closes=UPTURN + [107], hold=1)               # a lower close: came back down
    assert ok is False and "not confirmed: 1 of the last 1 candle came back down" in detail
    assert _structure(1, closes=UPTURN + [107])[0] is True                   # 0: acts at once, as before
    assert "confirmed by 2 candle(s) going its way" in st.label(
        {"type": "structure", "tf": "1m", "params": {"hold": 2}}, "exit")


# ----------------------------------------------------------------- ADX: strength, and is it building?

CHOP = [100 + math.sin(i * 1.3) for i in range(60)]
TREND = CHOP + [CHOP[-1] + 0.3 * (i + 1) for i in range(25)]      # ADX(14) climbs to 46


def _sideways(n):
    """The trend, then n bars going sideways at its top: ADX lags, then turns down."""
    return TREND + [TREND[-1] + 0.6 * math.sin(i * 1.3) for i in range(1, n + 1)]


def _adx(closes, s=1, **params):
    return st._c_adx(st.clean_params(st.CATALOG["adx"], params), s, _bars(closes), None)


def test_adx_rising_wants_the_strength_building():
    assert _adx(TREND, rising=1)[0] is True
    ok, detail = _adx(_sideways(9), rising=1)                          # the trend stalled: ADX turned down
    assert ok is False and "not rising" in detail
    assert _adx(_sideways(9))[0] is True                               # without it: ADX 49 is still over 20


def test_adx_rising_means_on_each_bar_not_over_the_span():
    line = adx_series(_candles(_sideways(4)), 14)["adx"]
    assert line[-1] > line[-4] and line[-1] < line[-2]                 # up over 3 bars, but down on the last
    ok, detail = _adx(_sideways(4), rising=3)
    assert ok is False and "down on 1 of the last 3 bars" in detail
    assert _adx(_sideways(6), rising=1)[0] is True                     # up on the last 2 bars, down before
    assert _adx(_sideways(6), rising=3)[0] is False


def test_adx_rises_in_a_falling_market_too():
    down = CHOP + [CHOP[-1] - 0.3 * (i + 1) for i in range(25)]
    assert _adx(down, s=-1, rising=2)[0] is True                       # a sell: -DI leading, strength building
    assert _adx(down, s=1, rising=2)[0] is False


def test_a_saved_adx_rule_reads_as_before():
    # Rules saved before "rising" existed read it as 0: not needed.
    assert st.clean_params(st.CATALOG["adx"], {"period": 22, "level": 20})["rising"] == 0
    assert "rising on each of the last 1 bar(s)" in st.label(_cond("adx", period=22, level=20, rising=1), "long")


# ----------------------------------------------------------------- ATR: is the market moving enough?

def _walk(steps, start=100.0):
    out, px = [], start
    for d in steps:
        px += d
        out.append(px)
    return out


_QUIET = [0.02 if i % 2 else -0.02 for i in range(70)]
_BUSY = [1.0 if i % 2 else -1.0 for i in range(10)]
AWAKE = _walk(_QUIET + _BUSY)                              # quiet, then ten wide bars
COOLING = _walk(_QUIET[:60] + _BUSY + _QUIET[:30])         # the busy spell behind it, quiet again
JUST_WOKE = COOLING + _walk([5.0], COOLING[-1])            # one wide bar after the quiet


def _atr(closes, s=1, **params):
    return st._c_atr(st.clean_params(st.CATALOG["atr"], params), s, _bars(closes), None)


def test_atr_lets_an_awake_market_through_and_stops_a_quiet_one():
    ok, detail = _atr(AWAKE, bars=3)
    assert ok is True and detail.startswith("ATR ") and "x its 50-bar average" in detail
    ok, detail = _atr(COOLING)
    assert ok is False and "0.58x its 50-bar average" in detail
    assert _atr(COOLING, mult=0.5)[0] is True                  # the bar is the user's to set
    assert _atr(AWAKE[:40])[0] is None                         # under 14 + 50 bars: can't tell


def test_atr_on_each_of_the_last_bars_lets_no_single_spike_through():
    assert _atr(JUST_WOKE, bars=1)[0] is True
    ok, detail = _atr(JUST_WOKE, bars=2)
    assert ok is False and detail.endswith("under 1x on 1 of the last 2 bars")


def test_atr_reads_the_same_for_a_buy_and_a_sell():
    assert _atr(AWAKE, s=1) == _atr(AWAKE, s=-1) and _atr(COOLING, s=1) == _atr(COOLING, s=-1)
    spec = st.CATALOG["atr"]
    assert spec.use == "entry" and spec.per_side is False and spec.group == "Filters"
    assert st.label(_cond("atr", bars=2), "long").startswith(
        "ATR 14 is at least 1.0x its 50-bar average on each of the last 2 bar(s)")


# ----------------------------------------------------------------- a stop that follows the SuperTrend

ST_STOP = {"mode": "supertrend", "tf": "1m", "period": 10, "mult": 2.7, "buffer": 0.5}


def _stop_at(closes=RISING, side=1, stop=None):
    return st.st_stop(stop or ST_STOP, {"1m": _bars(closes)}, side)


def _held(db_session, cfg, stop_loss, direction="long", source=None):
    pos = Position(symbol="BTCUSDm", asset_class="crypto", direction=direction, qty=0.1, entry_price=110.0,
                   stop_loss=stop_loss, status=PositionStatus.OPEN.value, source=source or f"strategy:{cfg.id}",
                   opened_at=NOW - timedelta(minutes=30), risk_amount=5.0, initial_risk_amount=5.0)
    db_session.add(pos)
    db_session.commit()
    return pos


def _moves(holder, status="submitted"):
    from app.models.enums import OrderStatus
    from app.models.schemas import OrderResult

    moves = []

    def set_sl_tp(symbol, stop_loss=None, take_profit=None, direction=None):
        moves.append((symbol, stop_loss, take_profit, direction))
        return OrderResult(status=OrderStatus(status), error=None if status == "submitted" else "Invalid stops")
    holder["broker"].set_sl_tp = set_sl_tp
    return moves


def test_the_supertrend_stop_sits_buffer_atr_beyond_the_line():
    bars = _bars(RISING)
    line, a = bars.st(10, 2.7)["line"][-1], bars.atr(10)[-1]
    want = _stop_at()
    assert want["line"] == line and want["level"] == pytest.approx(line - 0.5 * a)
    assert _stop_at(stop={**ST_STOP, "buffer": 0})["level"] == line           # 0 = exactly on the line
    assert _stop_at(side=-1) is None                                          # green: nothing above a sell
    assert _stop_at(FALLING, side=-1)["level"] > _stop_at(FALLING, side=-1)["line"]   # a sell's sits over it
    assert _stop_at(RISING[:8]) is None                                       # too few bars to read it


def test_a_supertrend_stop_strategy_opens_with_its_stop_under_the_line(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, stop=ST_STOP, target_r=2.0)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert out[0].get("opened") == "long"
    prop = calls["assess"][0][0]
    assert prop.stop_loss == pytest.approx(_stop_at()["level"], abs=0.006)     # 2 digits on this pair
    assert prop.take_profit == pytest.approx(prop.entry + 2.0 * (prop.entry - prop.stop_loss), abs=0.011)
    assert st.Rules.of(cfg).tfs() == {"1m"}


def test_no_entry_when_the_supertrend_is_against_the_trade(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)
    cfg = _strategy(db_session, stop=ST_STOP, sides="long", entry_long=[[_cond("spread", max_pct=200)]])
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and "against a long - no line to put the stop on" in out[0]["skipped"]


def test_the_stop_follows_the_line_only_toward_the_trade(db_session, engine):
    from app.models.db import AgentRun

    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, stop=ST_STOP, exit_rules=[])
    pos = _held(db_session, cfg, stop_loss=100.0)
    st.run_strategy(db_session, cfg, now=NOW)
    level = round(_stop_at()["level"], 2)
    assert moves == [("BTCUSDm", level, None, "long")]                        # the target is left as it is
    db_session.refresh(pos)
    assert pos.stop_loss == level
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "stop_moved")).one()
    assert ev.detail["frm"] == 100.0 and ev.detail["to"] == level and ev.detail["tf"] == "1m"
    st.run_strategy(db_session, cfg, now=NOW)                                 # the same line: nothing to do
    pos.stop_loss = level + 1.0                                               # tighter than the line already
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert len(moves) == 1


def test_the_stop_is_never_put_at_or_past_the_price(db_session, engine):
    _, holder = engine
    moves = _moves(holder)
    holder["broker"].scalp_spec = lambda symbol: {"bid": 120.0, "ask": 120.1, "spread": 0.1, "digits": 2,
                                                  "stops_level": 0.0}
    cfg = _strategy(db_session, stop={**ST_STOP, "buffer": 0}, exit_rules=[])
    _held(db_session, cfg, stop_loss=100.0)
    assert _stop_at(stop={**ST_STOP, "buffer": 0})["level"] > 120.0           # the line is over the bid now
    st.run_strategy(db_session, cfg, now=NOW)
    assert moves == []


def test_with_close_my_own_trades_on_the_users_stop_follows_the_line_too(db_session, engine):
    # Asked 2026-11-03: the user's CADJPY trade kept its stop while strategy 1 managed it.
    from app.models.db import AgentRun

    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, stop=ST_STOP, exit_rules=[], manage_manual=True)
    pos = _held(db_session, cfg, stop_loss=100.0, source="manual")
    out = st.run_strategy(db_session, cfg, now=NOW)
    level = round(_stop_at()["level"], 2)
    assert moves == [("BTCUSDm", level, None, "long")]                        # its target is left as it is
    db_session.refresh(pos)
    assert pos.stop_loss == level
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "stop_moved")).one()
    assert ev.detail["yours"] is True
    assert "your trade on this pair - its close rules and SuperTrend stop are on it" in out[0]["note"]
    pos.stop_loss = level + 1.0                                               # the user's own, tighter: kept
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert len(moves) == 1


def test_with_close_my_own_trades_off_the_users_stop_is_left_alone(db_session, engine):
    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, stop=ST_STOP, exit_rules=[])
    pos = _held(db_session, cfg, stop_loss=100.0, source="manual")
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert moves == [] and "you have a trade open on this pair - no new entry" in out[0]["note"]
    db_session.refresh(pos)
    assert pos.stop_loss == 100.0


def test_another_bots_trade_is_never_trailed(db_session, engine):
    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, stop=ST_STOP, exit_rules=[], manage_manual=True)
    _held(db_session, cfg, stop_loss=100.0, source="hybrid")                  # not the user's hand, not its own
    st.run_strategy(db_session, cfg, now=NOW)
    assert moves == []


def test_two_trades_on_one_side_keep_their_stops(db_session, engine):
    # Its own trade beside one of the user's on the same side: the broker would move both stops - neither.
    _, holder = engine
    moves = _moves(holder)
    cfg = _strategy(db_session, stop=ST_STOP, exit_rules=[], manage_manual=True)
    _held(db_session, cfg, stop_loss=100.0, source="manual")
    _held(db_session, cfg, stop_loss=100.0)
    st.run_strategy(db_session, cfg, now=NOW)
    assert moves == []


def test_a_refused_stop_move_leaves_the_stop_and_says_so(db_session, engine):
    from app.models.db import AgentRun

    _, holder = engine
    _moves(holder, status="error")
    cfg = _strategy(db_session, stop=ST_STOP, exit_rules=[])
    pos = _held(db_session, cfg, stop_loss=100.0)
    st.run_strategy(db_session, cfg, now=NOW)
    db_session.refresh(pos)
    assert pos.stop_loss == 100.0
    ev = db_session.scalars(select(AgentRun).where(AgentRun.event == "stop_move_failed")).one()
    assert ev.detail["reason"] == "Invalid stops"


def test_the_board_saves_a_supertrend_stop_and_reads_it(db_session, engine):
    from app.api.strategy_routes import StrategyIn, create_strategy, update_strategy

    for bad in ({"tf": "7m"}, {"buffer": -1}, {"buffer": 6}, {"period": 2}, {"mult": 0.1}):
        with pytest.raises(ValidationError):
            StrategyIn(stop=bad)
    view = create_strategy(StrategyIn(stop={"tf": "5m", "buffer": 1.0}), session=db_session)
    assert view.stop == {"mode": "supertrend", "tf": "5m", "period": 10, "mult": 2.7, "buffer": 1.0}
    assert "5m" in StrategyIn(stop={"tf": "5m"}).rules().tfs()                # read at each 5m close
    assert update_strategy(view.id, StrategyIn(stop=None), session=db_session).stop is None
    out = st.check_now(db_session, st.Rules(entry_long=[[_cond("trend")]], stop=ST_STOP), "BTCUSDm", "crypto",
                       now=NOW)
    assert out["stop"]["short"] is None and out["stop"]["long"]["level"] < out["stop"]["long"]["line"]
    assert "stop" not in st.check_now(db_session, st.Rules(entry_long=[[_cond("trend")]]), "BTCUSDm", "crypto",
                                      now=NOW)


# ----------------------------------------------------------------- trading hours

def _hrs(start, end, tz="UTC"):
    return {"start": start, "end": end, "tz": tz}


def _at(h, m=0, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=timezone.utc)


def test_trading_hours_read_the_strategys_own_clock():
    dubai = _hrs("09:00", "12:00", "Asia/Dubai")                    # UTC+4, no DST
    assert st.hours_now(dubai, _at(5, 0))["open"] is True             # 09:00 there
    assert st.hours_now(dubai, _at(7, 59, 59))["open"] is True        # 11:59:59
    shut = st.hours_now(dubai, _at(8, 0))                             # 12:00: the end is not inside
    assert shut["open"] is False and shut["now"] == "12:00" and shut["next_s"] == 21 * 3600
    early = st.hours_now(dubai, _at(0, 1, 30))                        # 04:01:30, five hours early
    assert early["open"] is False and early["next_s"] == 4 * 3600 + 58 * 60 + 30
    assert st.hours_now(dubai, _at(6, 0))["next_s"] == 2 * 3600       # 10:00: open until 12:00
    assert st.hours_now(None, _at(6, 0)) is None and st.hours_now({}, _at(6, 0)) is None


def test_trading_hours_can_run_past_midnight():
    night = _hrs("22:00", "02:00")
    assert [st.hours_now(night, _at(h))["open"] for h in (21, 22, 23, 0, 1, 2, 3)] == \
        [False, True, True, True, True, False, False]
    nine_to_midnight = _hrs("09:00", "00:00")
    assert st.hours_now(nine_to_midnight, _at(23, 59))["open"] is True
    assert st.hours_now(nine_to_midnight, _at(0, 0))["open"] is False
    assert st.hours_now(nine_to_midnight, _at(8, 59))["open"] is False
    assert st.hours_now(nine_to_midnight, _at(9, 0))["open"] is True


def test_outside_its_hours_it_opens_nothing_and_says_so(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, hours=_hrs("09:00", "12:00"))       # NOW is 12:00:30 UTC
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and out[0]["note"] == "outside its hours - no new trades until 09:00"
    cfg.hours = _hrs("09:00", "12:01")
    db_session.commit()
    out = st.run_strategy(db_session, cfg, now=NOW)                   # the same bar, now inside
    assert out[0].get("opened") == "long" and len(calls["assess"]) == 1


def test_outside_its_hours_its_close_rules_still_run(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)
    cfg = _strategy(db_session, hours=_hrs("09:00", "12:00"))
    mine = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                    stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                    opened_at=NOW - timedelta(hours=3))
    db_session.add(mine)
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert [c[0] for c in calls["close"]] == [mine.id]


def test_the_board_reads_the_drafts_hours(db_session, engine):
    rules = st.Rules(timeframe="1m", entry_long=[[_cond("trend")]], hours=_hrs("13:00", "17:00"))
    out = st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW)
    assert out["hours"]["open"] is False and out["hours"]["next_s"] == 59 * 60 + 30
    assert out["long"]["ok"] is True                # the rules still read; only the engine's entries wait
    assert st.check_now(db_session, st.Rules(timeframe="1m"), "BTCUSDm", "crypto", now=NOW)["hours"] is None


def test_the_board_saves_trading_hours(db_session):
    from app.api.strategy_routes import StrategyIn, create_strategy, update_strategy

    for bad in ({"start": "9:00", "end": "12:00"},                   # 24-hour HH:MM only
                {"start": "24:00", "end": "12:00"},
                {"start": "09:00", "end": "09:00"},                  # no window: that is "all day"
                {"start": "09:00", "end": "12:00", "tz": "Mars/Olympus"}):
        with pytest.raises(ValidationError):
            StrategyIn(hours=bad)
    view = create_strategy(StrategyIn(hours={"start": "09:00", "end": "12:00", "tz": "Asia/Dubai"}), session=db_session)
    assert view.hours == {"start": "09:00", "end": "12:00", "tz": "Asia/Dubai"}
    assert view.hours_now["start"] == "09:00" and isinstance(view.hours_now["open"], bool)
    view = update_strategy(view.id, StrategyIn(hours=None), session=db_session)
    assert view.hours is None and view.hours_now is None
    assert StrategyIn(hours={"start": "22:00", "end": "02:00"}).rules().hours == {"start": "22:00", "end": "02:00",
                                                                                   "tz": None}


# ----------------------------------------------------------------- the daily target

def _won(session, cfg, pnl, closed, symbol="ETHUSDm", source=None):
    """A trade closed at ``closed`` with ``pnl`` - the strategy's own unless ``source`` says otherwise."""
    p = Position(symbol=symbol, asset_class="crypto", direction="long", qty=0.1, entry_price=1.0,
                 status=PositionStatus.CLOSED.value, source=source or f"strategy:{cfg.id}",
                 opened_at=closed - timedelta(minutes=5), closed_at=closed, realized_pnl=pnl)
    session.add(p)
    session.commit()
    return p


def _target_rows(session):
    from app.models.db import AgentRun

    return session.scalars(select(AgentRun).where(AgentRun.event == "target_reached")).all()


def test_the_daily_target_stops_new_entries_for_the_day(db_session, engine):
    """Asked for 2026-11-03: "if today we made $300 ... we stop trading for today"."""
    calls, _ = engine
    cfg = _strategy(db_session, daily_target=300.0)
    _won(db_session, cfg, 180.0, NOW - timedelta(hours=2))
    assert st.today_result(db_session, cfg, NOW) == {"pnl": 180.0, "trades": 1, "hit_at": None}   # not there yet
    _won(db_session, cfg, 120.0, NOW - timedelta(hours=1))
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == []
    assert out[0]["note"] == "daily target reached - +$300.00 today, target $300.00: no new trades until 00:00 UTC"
    st.run_strategy(db_session, cfg, now=NOW + timedelta(seconds=10))
    rows = _target_rows(db_session)                                    # the feed says so once, not each pass
    assert len(rows) == 1 and rows[0].detail["pnl"] == 300.0 and rows[0].detail["target"] == 300.0
    assert rows[0].detail["strategy_id"] == cfg.id and rows[0].detail["trades"] == 2
    assert rows[0].detail["at"] == (NOW - timedelta(hours=1)).isoformat() and rows[0].symbol is None


def test_once_reached_the_target_holds_for_the_day(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, daily_target=300.0)
    _won(db_session, cfg, 350.0, NOW - timedelta(hours=3))
    _won(db_session, cfg, -100.0, NOW - timedelta(hours=1))           # a trade still open then, closed at a loss
    day = st.today_result(db_session, cfg, NOW)
    assert day["pnl"] == 250.0 and day["hit_at"] == NOW - timedelta(hours=3)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and "daily target reached - +$250.00 today" in out[0]["note"]


def test_the_target_counts_only_its_own_trades_closed_today(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, daily_target=300.0)
    _won(db_session, cfg, 500.0, NOW.replace(hour=0, minute=0, second=0) - timedelta(seconds=1))   # yesterday
    _won(db_session, cfg, 500.0, NOW - timedelta(hours=1), source="manual")                          # the user's
    _won(db_session, cfg, 500.0, NOW - timedelta(hours=1), source="strategy:999")                    # another one's
    gone = _won(db_session, cfg, 500.0, NOW - timedelta(hours=1))
    gone.realized_pnl = None              # closed at the broker, its figure not in yet: not counted until it is
    db_session.commit()
    assert st.today_result(db_session, cfg, NOW) == {"pnl": 0.0, "trades": 0, "hit_at": None}
    assert st.run_strategy(db_session, cfg, now=NOW)[0].get("opened") == "long"


def test_a_raised_target_trades_again_and_no_target_never_stops(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, daily_target=300.0)
    _won(db_session, cfg, 149.9, NOW - timedelta(hours=2))
    _won(db_session, cfg, 150.1, NOW - timedelta(hours=1))           # exactly $300 - no float shortfall
    assert st.today_result(db_session, cfg, NOW)["hit_at"] == NOW - timedelta(hours=1)
    assert "daily target" in st.run_strategy(db_session, cfg, now=NOW)[0]["note"]
    cfg.daily_target = 500.0
    db_session.commit()
    assert st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))[0].get("opened") == "long"
    cfg2 = _strategy(db_session, daily_target=None, pairs=[{"symbol": "XAUUSDm", "asset_class": "metal"}])
    _won(db_session, cfg2, 10_000.0, NOW - timedelta(hours=1))
    assert st.today_result(db_session, cfg2, NOW)["hit_at"] is None
    assert st.run_strategy(db_session, cfg2, now=NOW)[0].get("opened") == "long"


def test_after_the_target_its_close_rules_still_run(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)
    cfg = _strategy(db_session, daily_target=300.0)
    _won(db_session, cfg, 400.0, NOW - timedelta(hours=2))
    mine = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                    stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                    opened_at=NOW - timedelta(hours=3))
    db_session.add(mine)
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert [c[0] for c in calls["close"]] == [mine.id]


def test_the_board_saves_a_daily_target_and_shows_the_day(db_session):
    from app.api.strategy_routes import StrategyIn, create_strategy, update_strategy

    for bad in (0, -50, 2_000_000):
        with pytest.raises(ValidationError):
            StrategyIn(daily_target=bad)
    view = create_strategy(StrategyIn(daily_target=400), session=db_session)
    assert view.daily_target == 400 and view.pnl_today == 0.0 and view.target_hit is None
    cfg = db_session.get(StrategyConfig, view.id)
    closed = datetime.now(timezone.utc).replace(microsecond=0)
    if closed.hour == 0 and closed.minute < 10:        # just after midnight UTC: keep both trades in today
        closed += timedelta(minutes=10)
    _won(db_session, cfg, 250.0, closed - timedelta(minutes=10))
    _won(db_session, cfg, 160.0, closed - timedelta(minutes=5))
    view = update_strategy(view.id, StrategyIn(daily_target=400), session=db_session)
    assert view.pnl_today == 410.0 and view.target_hit == {"at": (closed - timedelta(minutes=5)).isoformat(), "pnl": 410.0}
    view = update_strategy(view.id, StrategyIn(daily_target=None), session=db_session)
    assert view.daily_target is None and view.target_hit is None and view.pnl_today == 410.0


# ----------------------------------------------------------------- stochastic: our way, with room

class _Stoch:
    """Bars whose stochastic reads ``k`` / ``d`` on the last bar."""

    def __init__(self, k, d):
        self.k, self.d = k, d

    def stoch(self, k, smooth=3, d=3):
        return [None, self.k], [None, self.d]


def _st_room(s, k, d, zone=80):
    return st._c_stoch_room(st.clean_params(st.CATALOG["stoch_room"], {"zone": zone}), s, _Stoch(k, d), st.Ctx(now=NOW))


def test_stochastic_with_room_wants_the_fast_line_our_way_and_out_of_the_danger_zone():
    """Asked for 2026-11-03: the fast line above the slow one, and not in the dangerous zone - room to go up.
    No cross needed, and none from the low zone."""
    assert _st_room(1, 55, 48) == (True, "%K 55 above %D 48, 25 below the overbought zone (80)")
    assert _st_room(1, 45, 52)[0] is False                           # the fast line under the slow one
    ok, why = _st_room(1, 84, 79)                                     # our way, but overbought
    assert ok is False and why.endswith("in the overbought zone")
    assert _st_room(1, 80, 70)[0] is False                            # on the line counts as in the zone
    assert _st_room(1, 50, 50)[0] is False                            # level: not leading either way
    # A sell is the mirror: %K under %D and over 20.
    assert _st_room(-1, 45, 52) == (True, "%K 45 under %D 52, 25 above the oversold zone (20)")
    assert _st_room(-1, 15, 22)[0] is False and _st_room(-1, 55, 48)[0] is False
    assert _st_room(1, 72, 60, zone=70)[0] is False and _st_room(1, 72, 60, zone=75)[0] is True


def test_stochastic_with_room_reads_real_bars_and_the_board_shows_it():
    ok, why = st._c_stoch_room(st.clean_params(st.CATALOG["stoch_room"], {}), 1, _bars(RISING), st.Ctx(now=NOW))
    assert ok is False and "overbought" in why                     # a steady climb pins %K at the top
    assert st._c_stoch_room({"k": 5, "zone": 80}, 1, _bars([100.0, 101.0]), st.Ctx(now=NOW)) == (None, "not enough bars")
    c = {"type": "stoch_room", "tf": "1m", "params": {"k": 5, "zone": 80}}
    assert "under 80" in st.label(c, "long") and "over 20" in st.label(c, "short")
    spec = next(x for x in st.catalog() if x["type"] == "stoch_room")
    assert spec["use"] == "entry" and spec["group"] == "Momentum"


# ----------------------------------------------------------------- Timeframes agree: the strip as a rule

UP_THEN_DOWN = [100 + 0.3 * i for i in range(120)] + [136 - 0.6 * i for i in range(1, 61)]


def _align(tfs, need=0) -> dict:
    """A "Timeframes agree" rule's params: these timeframes ticked, every other one not."""
    return st.clean_params(st.CATALOG["tf_align"], {**{tf: int(tf in tfs) for tf in st._ALIGN_TFS}, "need": need})


def _tf_align(tfs, tf="1m", need=0) -> dict:
    return {"type": "tf_align", "tf": tf, "params": _align(tfs, need)}


def _turn(b) -> int:
    """The first bar of the down-run the series ends in."""
    i = len(b) - 1
    while st._lean(b, i - 1)["side"] == -1:
        i -= 1
    return i


def test_the_lean_is_the_strips_verdict():
    from app.agents.market_monitor import lean_of, readings

    up = {"trend": "up", "supertrend": "up", "macd_hist": 0.2, "rsi": 55.0, "adx": 20.0, "plus_di": 25.0,
          "minus_di": 20.0, "structure": "up", "choch_side": None, "volume": {"up_share": 0.6}}
    assert lean_of(up) == {"up": 7, "down": 0, "of": 7, "side": 1}
    # The strip's edges: RSI under 55, a DI gap under 5 or an ADX under 20, buyers under 60% are no side.
    edge = {**up, "rsi": 54.9, "plus_di": 24.9, "volume": {"up_share": 0.59}}
    assert readings(edge) == {"trend": 1, "supertrend": 1, "macd": 1, "rsi": 0, "adx": 0, "structure": 1, "flow": 0}
    assert readings({**up, "adx": 19.9})["adx"] == 0
    assert readings({**up, "choch_side": "down"})["structure"] == -1     # a change of character overrides
    # A lead of one is no lean: 3 up, 2 down reads ≈.
    mixed = {**up, "supertrend": "down", "macd_hist": -0.1, "rsi": 60.0, "adx": None, "volume": None}
    assert lean_of(mixed) == {"up": 3, "down": 2, "of": 7, "side": 0}
    assert lean_of(None) is None


def test_the_rule_reads_the_monitors_own_lean():
    from app.agents.market_monitor import lean_of, lean_read, read_timeframe

    for closes in (RISING, FALLING, UP_THEN_DOWN):
        cs = _candles(closes)
        assert lean_read(cs, "1m") == lean_of(read_timeframe(OHLCVSeries(symbol="X", timeframe="1m", candles=cs)))
    assert lean_read(_candles(RISING[:50]), "1m") is None                 # too little history, as the strip


def test_timeframes_agree_when_every_ticked_one_leans_the_trades_way():
    data = {"1m": _bars(RISING), "5m": _bars(RISING, "5m")}
    ctx = st.Ctx(now=NOW, bars=data)
    ok, why = st._c_tf_align(_align(["1m", "5m"]), 1, data["1m"], ctx)
    assert ok is True and why == "1m ▲5/7 · 5m ▲5/7 - 2 of 2 lean up"
    ok, why = st._c_tf_align(_align(["1m", "5m"]), -1, data["1m"], ctx)
    assert ok is False and why.endswith("0 of 2 lean down, needs all")


def test_how_many_of_the_ticked_timeframes_must_agree():
    data = {"1m": _bars(RISING), "5m": _bars(FALLING, "5m")}
    ctx = st.Ctx(now=NOW, bars=data)

    def holds(need, s):
        return st._c_tf_align(_align(["1m", "5m"], need), s, data["1m"], ctx)[0]

    assert holds(0, 1) is False and holds(0, -1) is False                 # all of them: they disagree
    assert holds(1, 1) is True and holds(1, -1) is True                   # any one of them
    assert holds(8, 1) is False                                           # more than ticked = all


def test_a_timeframe_it_cannot_read_holds_it_only_when_it_matters():
    data = {"1m": _bars(RISING)}
    ctx = st.Ctx(now=NOW, bars=data)
    assert st._c_tf_align(_align(["1m", "5m"]), 1, data["1m"], ctx) == (None, "1m ▲5/7 · 5m no read - can't read 5m yet")
    assert st._c_tf_align(_align(["1m", "5m"], need=1), 1, data["1m"], ctx)[0] is True
    assert st._c_tf_align(_align([]), 1, data["1m"], ctx) == (None, "no timeframe ticked")


def test_the_rule_reads_the_timeframes_it_ticks_not_its_own():
    cond = _tf_align(["1m", "5m"], tf="1h")
    rules = st.Rules(timeframe="10s", entry_long=[[cond]])
    assert rules.tfs() == {"10s", "1m", "5m"}
    assert rules.sizes() == {"10s": st._BARS, "1m": st._ALIGN_BARS, "5m": st._ALIGN_BARS}
    data = {"10s": _bars(RISING, "10s"), "1m": _bars(RISING), "5m": _bars(RISING, "5m")}
    ev = st.evaluate([[cond]], 1, data, st.Ctx(now=NOW, base_tf="10s"), "long")
    row = ev["groups"][0]["conditions"][0]
    assert ev["ok"] is True and row["tf"] == "1m·5m"
    assert row["label"] == "All of 1M · 5M lean up on the Timeframes strip"


def test_the_rule_reads_as_a_sentence_in_the_log():
    assert st.label(_tf_align(["10s", "30s", "1m", "5m"]), "short") == \
        "All of 10S · 30S · 1M · 5M lean down on the Timeframes strip"
    assert st.label(_tf_align(["1m", "5m", "15m"], need=2), "long") == \
        "At least 2 of 1M · 5M · 15M lean up on the Timeframes strip"
    assert st.label(_tf_align(["5m"]), "long") == "5M leans up on the Timeframes strip"
    assert st.label(_tf_align(["1m", "5m"]), "exit") == "All of 1M · 5M turned against the trade on the Timeframes strip"
    spec = next(x for x in st.catalog() if x["type"] == "tf_align")
    assert spec["multi_tf"] is True and spec["use"] == "both"
    assert [q["key"] for q in spec["params"] if q["default"]] == ["10s", "30s", "1m", "5m"]   # the fast four ticked


def test_a_timeframes_close_rule_needs_a_turn_after_the_entry():
    b = _bars(UP_THEN_DOWN)
    data, turn, p = {"1m": b}, _turn(_bars(UP_THEN_DOWN)), _align(["1m"])

    def read(opened_at):
        pos = Position(symbol="BTCUSDm", direction="long", opened_at=opened_at)
        return st._c_tf_align(p, -1, b, st.Ctx(now=NOW, trade=pos, bars=data))    # judged against the long

    assert read(b.c[turn - 5].ts)[0] is True                              # up at the entry, down now: a turn
    ok, why = read(b.c[turn].ts + timedelta(seconds=90))                  # down already when it was opened
    assert ok is False and why.endswith("down already when the trade opened, not a turn")
    assert st._c_tf_align(p, -1, b, st.Ctx(now=NOW, bars=data))[0] is True   # no trade: the plain read


def test_one_timeframe_against_at_the_entry_does_not_stop_the_rule_turning():
    """The 5m leaned against the long all along; the 1m turned after the entry: the rule as a whole turned."""
    b1, b5 = _bars(UP_THEN_DOWN), _bars([130 - 0.3 * i for i in range(200)], "5m")
    data, p = {"1m": b1, "5m": b5}, _align(["1m", "5m"])
    turn = _turn(_bars(UP_THEN_DOWN))

    def long_at(t):
        return st.Ctx(now=NOW, trade=Position(symbol="BTCUSDm", direction="long", opened_at=t), bars=data)

    assert st._c_tf_align(p, -1, b1, long_at(b1.c[turn - 5].ts))[0] is True
    assert st._c_tf_align(p, -1, b1, long_at(b1.c[turn].ts + timedelta(seconds=90)))[0] is False   # both already


def test_the_leans_of_closed_bars_are_read_once(db_session, engine, monkeypatch):
    import app.agents.market_monitor as mm

    _, holder = engine
    holder["broker"] = _Broker(UP_THEN_DOWN)
    reads = []
    monkeypatch.setattr(st, "lean_read", lambda cs, tf: reads.append(tf) or mm.lean_read(cs, tf))
    rules = st.Rules(timeframe="1m", exit_rules=[[_tf_align(["1m"])]])
    db_session.add(Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110.0,
                            status=PositionStatus.OPEN.value, source="manual", opened_at=NOW - timedelta(minutes=70)))
    db_session.commit()
    first = st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW)
    assert first["exit_long"]["ok"] is True and len(reads) > 1            # it looked back for the turn
    n = len(reads)
    st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW + timedelta(seconds=2))
    assert len(reads) == n                                                # the same bar: nothing read again
    st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW + timedelta(minutes=1))
    assert len(reads) - n <= 1                                            # a new bar: only it is read


def test_the_engine_opens_on_the_timeframes_and_closes_on_their_turn(db_session, engine):
    calls, holder = engine
    rule = [[_tf_align(["1m", "5m"])]]
    cfg = _strategy(db_session, entry_long=rule, exit_rules=rule)
    st.run_strategy(db_session, cfg, now=NOW)                            # both lean up: a buy
    assert len(calls["exec"]) == 1 and calls["exec"][0].direction == "long"
    assert "All of 1M · 5M lean up on the Timeframes strip" in calls["exec"][0].rationale

    db_session.query(Position).delete()
    holder["broker"] = _Broker(UP_THEN_DOWN)
    st._BAR_CACHE.clear()
    turn = _turn(_bars(UP_THEN_DOWN))
    held = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                    stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                    opened_at=_bars(UP_THEN_DOWN).c[turn - 5].ts)         # the 1m turned down after it
    db_session.add(held)
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))
    assert [c[0] for c in calls["close"]] == [held.id]
    assert "All of 1M · 5M turned against the trade on the Timeframes strip" in calls["close"][0][1]


def test_the_board_saves_a_timeframes_rule():
    from app.api.strategy_routes import StrategyIn

    req = StrategyIn(timeframe="10s", entry_long=[[{"type": "tf_align", "tf": "10s",
                                                    "params": {**_align(["10s", "1m"]), "need": 99}}]],
                     exit_rules=[[{"type": "tf_align", "tf": "10s", "params": _align(["10s"])}]])
    assert req.entry_long[0][0].params["need"] == len(st._ALIGN_TFS)      # clamped into bounds: at most every chip
    assert req.rules().tfs() == {"10s", "1m"}


def test_the_live_read_carries_each_timeframes_lean_for_the_board():
    """The board and the widget draw the rule the way the strip is drawn: one lean per ticked timeframe."""
    data = {"1m": _bars(RISING), "5m": _bars(FALLING, "5m")}
    ev = st.evaluate([[_tf_align(["1m", "5m"]), _cond("trend")]], 1, data, st.Ctx(now=NOW, base_tf="1m"), "long")
    rule, trend = ev["groups"][0]["conditions"]
    assert rule["leans"] == [{"tf": "1m", "up": 5, "down": 0, "of": 7, "side": 1},
                             {"tf": "5m", "up": 0, "down": 5, "of": 7, "side": -1}]
    assert "leans" not in trend and trend["tf"] == "1m"
    ev = st.evaluate([[_tf_align(["1m", "15m"])]], 1, data, st.Ctx(now=NOW, base_tf="1m"), "long")
    assert ev["groups"][0]["conditions"][0]["leans"][1] == {"tf": "15m", "side": None}      # no 15m candles


# ----------------------------------------------------------------- groups switched off on the board

ALL_ON = {"entry_long": [], "entry_short": [], "exit_rules": []}


def test_a_group_switched_off_is_read_but_never_fires():
    data, ctx = {"1m": _bars(RISING)}, st.Ctx(now=NOW, base_tf="1m")
    ev = st.evaluate([[_cond("trend")]], 1, data, ctx, "long", off={0})
    assert ev["ok"] is False
    assert ev["groups"][0]["ok"] is True and ev["groups"][0]["off"] is True   # it would hold: the board says so
    ev = st.evaluate([[_cond("trend")], [_cond("trend")]], 1, data, ctx, "long", off={0})
    assert ev["ok"] is True and ev["group"] == 1 and "off" not in ev["groups"][1]


def test_a_switched_off_buy_group_opens_nothing_and_its_mirror_neither(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session, off_groups={**ALL_ON, "entry_long": [0]})
    st.run_strategy(db_session, cfg, now=NOW)                          # rising: its trend group would buy
    assert calls["exec"] == []
    assert st.Rules.of(cfg).sell_off() == {0}                          # the mirrored SELL is off with it
    cfg.off_groups = ALL_ON
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW + timedelta(minutes=1))
    assert len(calls["exec"]) == 1


def test_a_sell_of_its_own_has_its_own_switches():
    rules = st.Rules(entry_long=[[_cond("trend")]], entry_short=[[_cond("trend")]], mirror_short=False,
                     off={"entry_long": [0], "entry_short": []})
    assert rules.buy_off() == {0} and rules.sell_off() == set()


def test_a_switched_off_close_rule_closes_nothing(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING)                                # SuperTrend red against a long
    cfg = _strategy(db_session, entry_long=[[_cond("rsi_level", period=14, level=99)]],
                    off_groups={**ALL_ON, "exit_rules": [0]})
    db_session.add(Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                            stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                            opened_at=NOW - timedelta(hours=3)))
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert calls["close"] == []


def test_the_board_reads_a_switched_off_group_and_marks_it(db_session, engine):
    rules = st.Rules(timeframe="1m", entry_long=[[_cond("trend")], [_cond("vwap")]], off={"entry_long": [0]})
    out = st.check_now(db_session, rules, "BTCUSDm", "crypto", now=NOW)
    first, second = out["long"]["groups"]
    assert first["off"] is True and first["ok"] is True and "off" not in second
    assert out["long"]["ok"] is second["ok"]
    assert out["short"]["groups"][0]["off"] is True                    # mirrored


def test_a_switched_off_group_stays_off_when_the_board_saves(db_session):
    from app.api.strategy_routes import StrategyIn, _view, create_strategy

    trend, vwap = {"type": "trend", "tf": "1m"}, {"type": "vwap", "tf": "1m"}
    req = StrategyIn(entry_long=[[trend], [], [vwap]], exit_rules=[[{"type": "supertrend", "tf": "1m"}]],
                     off_groups={"entry_long": [2, 7], "exit_rules": [0]})
    assert req.off_groups == {"entry_long": [1], "entry_short": [], "exit_rules": [0]}   # moved up with its group
    view = create_strategy(req, session=db_session)
    assert view.off_groups == {"entry_long": [1], "entry_short": [], "exit_rules": [0]}
    assert st.Rules.of(db_session.get(StrategyConfig, view.id)).buy_off() == {1}
    assert StrategyIn().off_groups == ALL_ON
    assert _view(db_session, _strategy(db_session)).off_groups == ALL_ON           # saved before the switches


# ----------------------------------------------------------------- the live account (RISK.md 2026-11-04)

def _confirm_live(session) -> None:
    """The user's typed live confirmation, given after this process started."""
    from app.core.state import get_or_create_settings

    s = get_or_create_settings(session)
    s.broker_env, s.live_confirmed_at = "live", datetime.now(timezone.utc)
    session.commit()


def test_with_its_live_switch_on_it_trades_a_live_account_as_a_demo(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(RISING, paper=False)
    _confirm_live(db_session)
    cfg = _strategy(db_session, live=True)
    st.run_strategy(db_session, cfg, now=NOW)
    assert len(calls["exec"]) == 1 and calls["exec"][0].direction == "long"
    assert calls["assess"][0][1]["override_risk_fraction"] == cfg.risk_pct      # sized as on the demo


def test_on_a_live_account_it_opens_only_while_live_trading_is_confirmed(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING, paper=False)            # its mirrored SELL holds
    cfg = _strategy(db_session, live=True)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["exec"] == [] and "confirm LIVE trading" in out[0]["note"]


def test_on_a_live_account_its_trades_are_managed_before_the_confirmation(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(FALLING, paper=False)            # SuperTrend red against its long
    cfg = _strategy(db_session, live=True, entry_long=[[_cond("rsi_level", period=14, level=99)]])
    mine = Position(symbol="BTCUSDm", asset_class="crypto", direction="long", qty=0.1, entry_price=110,
                    stop_loss=100, status=PositionStatus.OPEN.value, source=f"strategy:{cfg.id}",
                    opened_at=NOW - timedelta(hours=3))
    db_session.add(mine)
    db_session.commit()
    st.run_strategy(db_session, cfg, now=NOW)
    assert [c[0] for c in calls["close"]] == [mine.id]


def test_the_live_switch_is_saved_and_shown(db_session):
    from app.api.strategy_routes import StrategyIn, _view, create_strategy

    view = create_strategy(StrategyIn(live=True), session=db_session)
    assert view.live is True and db_session.get(StrategyConfig, view.id).live is True
    assert StrategyIn().live is False
    assert _view(db_session, _strategy(db_session)).live is False                  # off unless switched on


def test_3m_is_a_strategy_timeframe_and_an_unticked_timeframes_chip():
    """3m (2026-11-05): a base timeframe, a condition's timeframe, and a chip on "Timeframes agree" between
    the 1m and the 5m. The chip starts unticked, so a rule saved before it (no "3m" key) reads exactly as it did."""
    from app.api.strategy_routes import StrategyIn

    assert st.TF_SECONDS["3m"] == 180 and st.TIMEFRAMES.index("3m") == st.TIMEFRAMES.index("1m") + 1
    spec = next(x for x in st.catalog() if x["type"] == "tf_align")
    chips = [q for q in spec["params"] if q["kind"] == "tf"]
    assert [q["key"] for q in chips][3:6] == ["1m", "3m", "5m"]
    assert next(q for q in chips if q["key"] == "3m")["default"] == 0
    saved = {"type": "tf_align", "tf": "1m", "params": {"10s": 1, "30s": 1, "1m": 1, "5m": 1, "need": 0}}
    assert st._ticked(st.clean_params(st.CATALOG["tf_align"], saved["params"])) == ["10s", "30s", "1m", "5m"]
    assert st.label(saved, "long") == "All of 10S · 30S · 1M · 5M lean up on the Timeframes strip"
    req = StrategyIn(timeframe="3m", entry_long=[[{"type": "trend", "tf": "3m"}, _tf_align(["1m", "3m"])]])
    assert req.timeframe == "3m" and {"1m", "3m"} <= req.rules().tfs()

def test_5s_is_an_unticked_timeframes_chip():
    """The 5s (2026-11-06) is the first chip on "Timeframes agree", left of the 10s, as on the strip. It starts
    unticked, so a rule saved before it (no "5s" key) reads exactly as it did; ticked, it reads the 5s bars."""
    from app.api.strategy_routes import StrategyIn

    spec = next(x for x in st.catalog() if x["type"] == "tf_align")
    chips = [q for q in spec["params"] if q["kind"] == "tf"]
    assert [q["key"] for q in chips][:3] == ["5s", "10s", "30s"]
    assert next(q for q in chips if q["key"] == "5s")["default"] == 0
    assert next(q for q in spec["params"] if q["key"] == "need")["max"] == len(chips)
    saved = {"type": "tf_align", "tf": "1m", "params": {"10s": 1, "30s": 1, "1m": 1, "5m": 1, "need": 0}}
    assert st._ticked(st.clean_params(st.CATALOG["tf_align"], saved["params"])) == ["10s", "30s", "1m", "5m"]
    cond = _tf_align(["5s", "10s"], tf="5s")
    req = StrategyIn(timeframe="5s", entry_long=[[cond]])
    assert req.rules().tfs() == {"5s", "10s"}
    data = {"5s": _bars(RISING, "5s"), "10s": _bars(RISING, "10s")}
    ev = st.evaluate([[cond]], 1, data, st.Ctx(now=NOW, base_tf="5s"), "long")
    assert ev["ok"] is True
    assert ev["groups"][0]["conditions"][0]["label"] == "All of 5S · 10S lean up on the Timeframes strip"
