"""A mapped real broker that cannot connect must never be replaced by the simulator.

The live failure (full review 2026-10-20, reproduced): with the PC rebooted and the backend started
before the MT5 terminal, the registry silently served the SIM broker for every asset class. One
monitor pass then priced the REAL open positions on the simulator's synthetic quotes and closed both
DB rows (EURUSD as 'target' +$9.96, XAUUSD as 'stop' -$39), and the daily-loss baseline became the
simulator's $100,000 - so the 10% breaker would only trip at a $10,000 loss on a $1,290 account.
"""
from __future__ import annotations

import pytest

from app.models.db import Position
from app.models.enums import AssetClass, OrderStatus, PositionStatus

_ALL_MT5 = {k: "mt5" for k in ("stock", "crypto", "forex", "metal", "energy", "index")}


@pytest.fixture
def mt5_down(db_session, monkeypatch):
    from app.brokers import registry
    from app.brokers.base import BrokerError
    from app.core.state import get_or_create_settings

    def boom():
        raise BrokerError("MT5 initialize failed: terminal not running")

    monkeypatch.setattr(registry, "Mt5BrokerAdapter", boom)
    registry.reset_registry()
    s = get_or_create_settings(db_session)
    s.broker_map = dict(_ALL_MT5)
    db_session.commit()
    yield db_session
    registry.reset_registry()


def test_a_broker_that_cannot_connect_is_unavailable_not_the_simulator(mt5_down):
    from app.brokers.base import BrokerError
    from app.brokers.registry import UnavailableBroker, get_broker_for

    b = get_broker_for(AssetClass.FOREX, _ALL_MT5)
    assert isinstance(b, UnavailableBroker)
    assert b.name == "mt5"
    with pytest.raises(BrokerError):
        b.get_quote("EURUSDm")
    with pytest.raises(BrokerError):
        b.get_open_positions()
    assert b.close_position("EURUSDm").status == OrderStatus.ERROR
    assert b.market_open("EURUSDm") is False
    assert b.can_open("EURUSDm", "long")[0] is False


def test_monitor_never_closes_real_positions_while_the_broker_is_down(mt5_down):
    from app.core.state import get_or_create_daily_state
    from app.execution.monitor import monitor_positions

    rows = [
        Position(symbol="EURUSDm", asset_class="forex", direction="long", qty=0.10, entry_price=1.0800,
                 stop_loss=1.0750, take_profit=1.0900, status=PositionStatus.OPEN.value, risk_amount=38.0),
        Position(symbol="XAUUSDm", asset_class="metal", direction="long", qty=0.01, entry_price=4000.0,
                 stop_loss=3985.0, take_profit=4045.0, status=PositionStatus.OPEN.value, risk_amount=15.0),
    ]
    mt5_down.add_all(rows)
    mt5_down.commit()
    monitor_positions(mt5_down)
    for r in rows:
        mt5_down.refresh(r)
        assert r.status == PositionStatus.OPEN.value
        assert r.realized_pnl in (None, 0, 0.0)
    d = get_or_create_daily_state(mt5_down)
    assert d.starting_equity != 100000


def test_equity_is_unknown_not_the_simulators_balance(mt5_down):
    from app.risk.service import current_equity

    assert current_equity(mt5_down) is None


def test_the_real_broker_is_used_again_once_it_connects(mt5_down, monkeypatch):
    from app.brokers import registry

    registry.get_broker("mt5")                                  # fails -> stand-in, remembered
    sentinel = object.__new__(registry.SimPaperBroker)
    monkeypatch.setattr(registry, "Mt5BrokerAdapter", lambda: sentinel)
    assert isinstance(registry.get_broker("mt5"), registry.UnavailableBroker)   # inside the backoff
    monkeypatch.setattr(registry, "_RETRY_SECONDS", 0.0)
    assert registry.get_broker("mt5") is sentinel                               # retried and cached
