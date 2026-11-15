"""The journal's signal review (2026-11-08): a watch-only signal replayed as the trade it would have been, and an
edit recording what it changed."""
from datetime import datetime, timedelta, timezone

from app.agents import signal_review as sr

T0 = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)


def step(m: float, hi: float, lo: float, close: float | None = None, spread: float = 0.0):
    return (T0 + timedelta(minutes=m), hi, lo, close if close is not None else (hi + lo) / 2, spread)


def run(side, path, entry=100.0, stop=None, target=None, now=None):
    stop = stop if stop is not None else (entry - side * 10)
    target = target if target is not None else (entry + side * 8)
    return sr.simulate(side, entry, stop, target, path, T0 + timedelta(hours=24), now or T0 + timedelta(hours=30))


# --- the replay ------------------------------------------------------------------------------------------

def test_a_buy_that_reaches_its_target_first_wins_its_r():
    out = run(1, [step(1, 103, 97), step(2, 108.5, 104)])
    assert out["result"] == "win" and out["r"] == 0.8 and out["exit_price"] == 108
    assert out["mae_r"] == 0.3        # 3 against it on the way, of a 10 risk


def test_a_buy_that_reaches_its_stop_first_loses_one_r():
    out = run(1, [step(1, 103, 95), step(2, 101, 89.5), step(3, 120, 100)])
    assert out["result"] == "loss" and out["r"] == -1.0 and out["exit_price"] == 90
    assert out["mfe_r"] == 0.3 and not out["same_bar"]


def test_both_levels_in_one_candle_count_as_the_stop():
    out = run(1, [step(1, 109, 89)])
    assert out["result"] == "loss" and out["same_bar"] is True


def test_a_sell_leaves_on_the_ask_so_the_spread_can_stop_it():
    # The bid only reaches 109.5 - under the stop at 110 - but the ask (bid + 1) is past it.
    out = run(-1, [step(1, 109.5, 101, spread=1.0)])
    assert out["result"] == "loss"
    # And its target needs the ASK down to 92: a bid at 91.5 is not enough with a 1.0 spread...
    assert run(-1, [step(1, 100, 91.5, spread=1.0)])["result"] != "win"
    # ...a bid at 90.9 is.
    assert run(-1, [step(1, 100, 90.9, spread=1.0)])["result"] == "win"


def test_still_open_before_the_horizon_and_judged_where_price_is_after_it():
    path = [step(1, 103, 98, 102), step(60, 104, 99, 104)]
    open_ = run(1, path, now=T0 + timedelta(hours=2))
    assert open_["result"] == "open" and open_["r"] == 0.4 and open_["exit_at"] is None
    late = run(1, path + [step(24 * 60 + 5, 107, 103, 106)], now=T0 + timedelta(hours=26))
    assert late["result"] == "timeout" and late["r"] == 0.4      # the step past the horizon is not read


def test_no_distance_to_the_stop_is_not_judged():
    assert sr.simulate(1, 100, 100, 108, [step(1, 101, 99)], T0 + timedelta(hours=24), T0)["result"] == "unknown"


# --- what an edit changed ------------------------------------------------------------------------------------

def snap(**over):
    base = {"name": "S", "pairs": [{"symbol": "BTCUSDm"}], "timeframe": "30s", "sides": "both", "mirror_short": True,
            "entry_long": [[{"type": "adx", "tf": "1m", "params": {"period": 14, "level": 22, "rising": 0}},
                            {"type": "spread", "tf": "30s", "params": {"max_pct": 25}}]],
            "entry_short": [], "exit_rules": [], "risk_pct": 0.05, "stop_atr": 4.5, "target_r": 0.8,
            "max_trades_day": 180, "manage_manual": False, "cooldown_minutes": 15, "loss_cooldown_minutes": 30,
            "stop": None, "hours": None, "daily_target": None, "off_groups": {}, "live": True}
    base.update(over)
    return base


def test_an_edit_says_which_conditions_and_settings_changed():
    before = snap()
    after = snap(entry_long=[[{"type": "spread", "tf": "30s", "params": {"max_pct": 25}}]],
                 cooldown_minutes=10, stop_atr=4.0, target_r=1.0,
                 pairs=[{"symbol": "BTCUSDm"}, {"symbol": "ETHUSDm"}], off_groups={"entry_long": [0]})
    lines = sr.describe_changes(before, after)
    assert any(x.startswith("BUY group 1: − ADX 14 is at least 22") for x in lines), lines
    assert "BUY group 1: switched off (watch only)" in lines
    assert "wait after a close: 15 min → 10 min" in lines
    assert "stop: 4.5× ATR → 4× ATR" in lines
    assert "target: 0.8R → 1R" in lines
    assert "+ pairs: ETHUSDm" in lines
    # The spread rule did not change, so it is not listed.
    assert not any("spread is at most" in x for x in lines)


def test_an_edit_that_changed_nothing_lists_nothing():
    assert sr.describe_changes(snap(), snap()) == []


def test_crypto_around_the_clock_reads_in_the_change_list():
    window = {"start": "05:00", "end": "23:31", "tz": "Asia/Dubai"}
    lines = sr.describe_changes(snap(hours=window), snap(hours={**window, "crypto_24h": True}))
    assert lines == ["trading hours: 05:00-23:31 Asia/Dubai → 05:00-23:31 Asia/Dubai, crypto 24/7"]


def test_trading_sessions_read_in_the_change_list():
    window = {"start": "05:00", "end": "23:31", "tz": "Asia/Dubai"}
    sessions = {"sessions": ["london", "between"], "tz": "Asia/Dubai", "crypto_24h": True}
    assert sr.describe_changes(snap(hours=window), snap(hours=sessions)) == [
        "trading hours: 05:00-23:31 Asia/Dubai → sessions: London + Between sessions, crypto 24/7"]


def test_hour_picks_read_in_the_change_list():
    sessions = {"sessions": ["london", "between"], "tz": "Asia/Dubai"}
    hours = {"hours_utc": [8, 9, 10, 11, 12, 16, 23], "tz": "Asia/Dubai"}
    assert sr.describe_changes(snap(hours=sessions), snap(hours=hours)) == [
        "trading hours: sessions: London + Between sessions → hours 08:00-13:00, 16:00-17:00, 23:00-00:00 UTC"]


def test_a_new_group_is_one_line():
    after = snap(entry_long=snap()["entry_long"] + [[{"type": "spread", "tf": "1m", "params": {"max_pct": 20}}]])
    assert sr.describe_changes(snap(), after) == ["BUY group 2 added (1 conditions)"]


# --- the review, end to end ------------------------------------------------------------------------------------

class _Series:
    def __init__(self, candles):
        self.candles = candles


class _Broker:
    """No MT5: the review falls back to the latest 1m candles."""
    def __init__(self, candles):
        self._c = candles

    def get_ohlcv(self, symbol, tf, limit):
        return _Series(self._c)

    def spread(self, symbol):
        return 0.0


def test_review_replays_logged_levels_and_lists_the_edits(db_session):
    from app.models.db import AgentRun, StrategyConfig
    from app.models.schemas import Candle

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    at = now - timedelta(hours=3)
    cfg = StrategyConfig(name="S", pairs=[{"symbol": "BTCUSDm", "asset_class": "crypto"}], timeframe="1m",
                         stop_atr=4.5, target_r=0.8)
    db_session.add(cfg)
    db_session.flush()
    db_session.add(AgentRun(agent="strategy", event="edited", created_at=at - timedelta(minutes=30),
                            detail={"strategy_id": cfg.id, "name": "S", "running": True,
                                    "changes": ["wait after a close: 15 min → 10 min"]}))
    # A sell at 100 with its stop at 110 and target at 92, logged - then price falls to 91.
    db_session.add(AgentRun(agent="strategy", event="signal", symbol="BTCUSDm", created_at=at,
                            detail={"strategy_id": cfg.id, "name": "S", "side": "short", "group": 1, "watch": True,
                                    "price": 100.0, "stop": 110.0, "target": 92.0, "rules": ["r1"]}))
    db_session.flush()
    candles = [Candle(ts=at + timedelta(minutes=m), open=100, high=101, low=99 - m, close=99.5 - m, volume=1)
               for m in range(1, 12)]
    out = sr.review(db_session, 30, now=now, broker_for=lambda ac: _Broker(candles))
    assert [e["changes"] for e in out["edits"]] == [["wait after a close: 15 min → 10 min"]]
    (s,) = out["signals"]
    assert s["levels"] == "logged" and s["result"] == "win" and s["r"] == 0.8 and s["side"] == "short"
    assert s["rules"] == ["r1"]
    # A final outcome is kept: a second read does not ask the broker again.
    out2 = sr.review(db_session, 30, now=now, broker_for=lambda ac: (_ for _ in ()).throw(AssertionError("asked")))
    assert out2["signals"][0]["result"] == "win"
    sr._FINAL.clear()


def test_a_signal_without_a_price_is_listed_but_not_judged(db_session):
    from app.models.db import AgentRun

    db_session.add(AgentRun(agent="strategy", event="signal", symbol="BTCUSDm",
                            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                            detail={"strategy_id": 99, "side": "long", "group": 1}))
    db_session.flush()
    (s,) = sr.review(db_session, 30, broker_for=lambda ac: _Broker([]))["signals"]
    assert s["result"] == "unknown" and s["levels"] == "none"


# --- the two producers: an edit through the route, a signal's levels ----------------------------------

def test_editing_a_strategy_records_what_changed(db_session):
    from sqlalchemy import select

    from app.api.strategy_routes import StrategyIn, create_strategy, update_strategy
    from app.models.db import AgentRun

    rule = {"type": "adx", "tf": "1m", "params": {"period": 14, "level": 22, "rising": 0}}
    view = create_strategy(StrategyIn(name="S", entry_long=[[rule]], cooldown_minutes=15), session=db_session)
    update_strategy(view.id, StrategyIn(name="S", entry_long=[[{**rule, "params": {**rule["params"], "level": 25}}]],
                                        cooldown_minutes=10), session=db_session)
    (row,) = db_session.scalars(select(AgentRun).where(AgentRun.event == "edited")).all()
    changes = row.detail["changes"]
    assert "wait after a close: 15 min → 10 min" in changes
    assert any(c.startswith("BUY group 1: + ADX 14 is at least 25") for c in changes)
    assert any(c.startswith("BUY group 1: − ADX 14 is at least 22") for c in changes)


def test_a_watch_signal_logs_the_stop_and_target_the_strategy_would_place(db_session, monkeypatch):
    import app.agents.strategy as st
    from app.models.db import StrategyConfig

    cfg = StrategyConfig(name="S", target_r=0.8, stop_atr=4.5)
    monkeypatch.setattr(st, "_side_risk", lambda *a, **k: 10.0)
    assert st._signal_levels(db_session, cfg, st.Rules(), {}, None, 1, 100.0) == {"stop": 90.0, "risk": 10.0,
                                                                                    "target": 108.0}
    assert st._signal_levels(db_session, cfg, st.Rules(), {}, None, -1, 100.0)["stop"] == 110.0
    monkeypatch.setattr(st, "_side_risk", lambda *a, **k: None)
    assert st._signal_levels(db_session, cfg, st.Rules(), {}, None, 1, 100.0) == {}
