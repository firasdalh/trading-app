"""The strategy activity feed (the board's and the desk's): the engine's own events, plus the closes of its
trades that never went through it - at the stop, at the target, or by hand - with how each one ended."""
from datetime import datetime, timedelta, timezone

from app.api.strategy_routes import all_activity, strategy_activity
from app.models.db import AgentRun, Position, StrategyConfig
from app.models.enums import PositionStatus

T = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


def _cfg(session, name):
    cfg = StrategyConfig(name=name, enabled=True, pairs=[{"symbol": "BTCUSDm", "asset_class": "crypto"}],
                         timeframe="1m", sides="both", entry_long=[], entry_short=[], mirror_short=True,
                         exit_rules=[], risk_pct=0.005, stop_atr=1.5, target_r=2.0, max_trades_day=10)
    session.add(cfg)
    session.commit()
    return cfg


def _closed(session, cfg, symbol, direction, at, exit_price, stop, target, pnl):
    p = Position(symbol=symbol, asset_class="metal" if symbol.startswith("XAU") else "crypto", direction=direction,
                 qty=0.1, entry_price=100.0, stop_loss=stop, take_profit=target, last_price=exit_price,
                 realized_pnl=pnl, status=PositionStatus.CLOSED.value, source=f"strategy:{cfg.id}",
                 opened_at=at - timedelta(minutes=5), closed_at=at)
    session.add(p)
    session.commit()
    return p


def _event(session, cfg, symbol, event, at, **detail):
    session.add(AgentRun(agent="strategy", symbol=symbol, event=event, created_at=at,
                         detail={"strategy_id": cfg.id, "name": cfg.name, **detail}))
    session.commit()


def test_the_feed_adds_the_closes_the_engine_did_not_make(db_session):
    a, b = _cfg(db_session, "A"), _cfg(db_session, "B")
    _event(db_session, a, "BTCUSDm", "opened", T, side="long", lots=0.1, rules=["EMA 10 above EMA 50"])
    # closed by its own rule: the engine logged it, and the trade closed a second later - listed once
    _event(db_session, a, "BTCUSDm", "closed", T + timedelta(minutes=3), side="long", pnl=4.0, rules=["SuperTrend turns"])
    _closed(db_session, a, "BTCUSDm", "long", T + timedelta(minutes=3, seconds=1), 100.4, 99.0, 102.0, 4.0)
    stop = _closed(db_session, a, "XAUUSDm", "long", T + timedelta(minutes=4), 98.9, 99.0, 102.0, -11.0)    # past its stop
    target = _closed(db_session, a, "ETHUSDm", "short", T + timedelta(minutes=5), 98.0, 101.0, 98.0, 20.0)   # its target
    hand = _closed(db_session, b, "USOILm", "short", T + timedelta(minutes=6), 99.5, 101.0, 98.0, 5.0)        # by hand

    feed = all_activity(limit=40, session=db_session)
    assert [(e["event"], e["symbol"]) for e in feed] == [
        ("closed", "USOILm"), ("closed", "ETHUSDm"), ("closed", "XAUUSDm"), ("closed", "BTCUSDm"), ("opened", "BTCUSDm")]
    by = {e["symbol"]: e["detail"].get("by") for e in feed if e["event"] == "closed"}
    assert by == {"USOILm": "other", "ETHUSDm": "target", "XAUUSDm": "stop", "BTCUSDm": None}
    assert sorted(e["id"] for e in feed if e["detail"].get("by")) == sorted([-stop.id, -target.id, -hand.id])
    hit = next(e for e in feed if e["symbol"] == "XAUUSDm")
    assert hit["asset_class"] == "metal" and hit["detail"]["pnl"] == -11.0 and hit["detail"]["name"] == "A"
    assert next(e for e in feed if e["symbol"] == "USOILm")["detail"]["name"] == "B"
    assert feed[-1]["asset_class"] == "crypto"                     # an engine event: from the strategy's pairs
    # one strategy's feed: its own events and trades only; the limit holds
    assert [e["symbol"] for e in strategy_activity(a.id, limit=40, session=db_session)] == [
        "ETHUSDm", "XAUUSDm", "BTCUSDm", "BTCUSDm"]
    assert len(all_activity(limit=2, session=db_session)) == 2
