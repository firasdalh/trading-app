"""Custom strategies (app/agents/strategy.py): the conditions read closed bars the right way round,
the rule groups combine as OR-of-ANDs, exits are judged against the trade, and the engine only ever
trades a paper account, once per bar, through the Risk Manager and the executor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import app.agents.strategy as st
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


def test_a_live_account_is_never_traded(db_session, engine):
    calls, holder = engine
    holder["broker"] = _Broker(RISING, paper=False)
    cfg = _strategy(db_session)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert calls["assess"] == [] and calls["exec"] == []
    assert "live account" in out[0]["note"]


def test_an_entry_goes_through_the_risk_manager_with_its_stop_and_target(db_session, engine):
    calls, _ = engine
    cfg = _strategy(db_session)
    out = st.run_strategy(db_session, cfg, now=NOW)
    assert out[0].get("opened") == "long"
    prop, kw = calls["assess"][0]
    # scalp mode's cooldowns: no per-pair wait, a 5-min pause after a loss in the same direction
    assert kw == {"override_risk_fraction": 0.005, "override_cooldown_minutes": 0, "override_loss_cooldown_minutes": 5}
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
