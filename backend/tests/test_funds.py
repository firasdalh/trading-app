"""The Withdraw page's money in/out record (2026-11-21): Exness's balance deals worded and totalled, read-only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.brokers.funds import build, kind_of
from app.brokers.mt5_adapter import Mt5BrokerAdapter

T0 = datetime(2026, 10, 28, 22, 46, 7, tzinfo=timezone.utc)
ACCT = {"currency": "USD", "balance": 2227.59, "equity": 2216.46, "margin": 322.93, "margin_free": 1893.53,
        "margin_level": 686.4, "open_positions": 1, "floating": -11.13}


def _op(ticket, sec, amount, comment, **kw):
    return {"ticket": ticket, "time": T0 + timedelta(seconds=sec), "amount": amount, "comment": comment, **kw}


def test_exness_comments_are_worded():
    assert kind_of("D-CC-USD-482229596167", 1008.67) == ("deposit", "card")
    assert kind_of("W-CC-USD-487377760263", -110) == ("withdrawal", "card")
    assert kind_of("W-ALLINT-USD-INT-1185362219013", -100) == ("transfer_out", "Exness account transfer")
    assert kind_of("D-ALLINT-USD-INT-1186231517189", 97.87) == ("transfer_in", "Exness account transfer")
    assert kind_of("D-NULL", 31.13) == ("protection", None)          # Exness putting a negative balance back to 0
    assert kind_of("DIV-HK50-29700", -2.17) == ("dividend", None)
    assert kind_of("W-USDTTRC20-123", -50) == ("withdrawal", "usdttrc20")   # a method it has no word for: its code
    assert kind_of("", 5, mt5_type=3) == ("credit", None)
    assert kind_of("correction", 1.5) == ("other", None)


def test_a_withdrawal_that_comes_straight_back_counts_as_neither():
    out = build([
        _op(1, 0, 1000, "D-CC-USD-1"),
        _op(2, 100, -110, "W-CC-USD-2"),
        _op(3, 113, 110, "D-CC-USD-3"),                 # back 13 s later
        _op(4, 200, -300, "W-CC-USD-4"),
        _op(5, 200 + 601, 300, "D-CC-USD-5"),           # same amount, but past the 10-minute window: a real deposit
        _op(6, 900, -50, "W-CC-USD-6"),
        _op(7, 905, 49.99, "D-CC-USD-7"),               # not the same amount
        _op(8, 1000, -100, "W-ALLINT-USD-INT-8"),
        _op(9, 1001, 100, "D-CC-USD-9"),                # not the same method
    ], ACCT)
    by = {o["ticket"]: o for o in out["ops"]}
    assert by[2]["returned"] and by[3]["returned"] and by[2]["pair"] == 3 and by[3]["back_after_s"] == 13
    assert not any(by[t]["returned"] for t in (1, 4, 5, 6, 7, 8, 9))
    tot = out["totals"]
    assert tot["deposits"] == 1000 + 300 + 49.99 + 100 and tot["withdrawals"] == 300 + 50
    assert tot["transfers_out"] == 100 and tot["returned_n"] == 1
    assert tot["money_in"] == 1449.99 and tot["money_out"] == 450 and tot["net_in"] == 999.99
    assert tot["withdrawals_n"] == 3 and tot["last_withdrawal"]["amount"] == -100
    assert [o["ticket"] for o in out["ops"]][:2] == [9, 8]            # newest first


def test_totals_trading_result_and_what_can_be_withdrawn():
    out = build([
        _op(1, 0, 5000, "D-CC-USD-1"),
        _op(2, 50, -1000, "W-CC-USD-2"),
        _op(3, 60, 21.5, "D-NULL"),
        _op(4, 70, -2.17, "DIV-HK50-29700"),
    ], ACCT)
    tot = out["totals"]
    assert tot["net_in"] == 4000 and tot["protection"] == 21.5 and tot["protection_n"] == 1
    assert tot["dividends"] == -2.17
    assert tot["trading_result"] == round(2227.59 - 4000, 2)          # what is there minus what was put in
    assert tot["trading_result_equity"] == round(2216.46 - 4000, 2)
    assert out["withdrawable"] == 1893.53 and out["balance"] == 2227.59 and out["open_positions"] == 1
    assert build([], {**ACCT, "margin_free": -5})["withdrawable"] == 0          # never below 0
    assert build([], ACCT)["totals"]["trading_result"] is None                   # no record, no result


def _deal(ticket, t, profit, symbol="", type_=2, comment="", swap=0.0, commission=0.0, fee=0.0):
    ts = int((T0 + timedelta(seconds=t)).timestamp())
    return SimpleNamespace(ticket=ticket, time=ts, time_msc=ts * 1000, profit=profit, symbol=symbol, type=type_,
                           comment=comment, swap=swap, commission=commission, fee=fee)


class _Mt5:
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1

    def __init__(self, deals, acct=True):
        self._deals, self._acct = deals, acct

    def account_info(self):
        return SimpleNamespace(currency="USD", balance=1150.0, equity=1140.0, margin=50.0, margin_free=1090.0,
                               margin_level=2280.0) if self._acct else None

    def history_deals_get(self, a, b):
        return self._deals


def _adapter(fake):
    a = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    a._mt5 = fake
    a._positions = lambda **k: [SimpleNamespace(profit=-12.0, swap=2.0)]
    return a


def test_mt5_reads_balance_deals_with_the_balance_after_each():
    deals = [
        _deal(1, 0, 1000.0, comment="D-CC-USD-1"),
        _deal(2, 10, 0.0, "XAUUSDm", 0, commission=-3.0),                 # trade in
        _deal(3, 20, 253.0, "XAUUSDm", 1, swap=-1.0, commission=-3.0),    # trade out
        _deal(4, 30, -100.0, comment="W-CC-USD-4"),
    ]
    out = _adapter(_Mt5(list(reversed(deals)))).get_funds()             # MT5's order doesn't matter
    assert [o["ticket"] for o in out["ops"]] == [4, 1]                  # trade deals left out
    by = {o["ticket"]: o for o in out["ops"]}
    assert by[1]["balance_after"] == 1000.0
    assert by[4]["balance_after"] == 1000.0 - 3 + 253 - 1 - 3 - 100     # trades count in the running balance
    assert out["totals"]["net_in"] == 900 and out["totals"]["trading_result"] == 250.0
    assert out["withdrawable"] == 1090.0 and out["open_positions"] == 1 and out["floating"] == -10.0


def test_mt5_unreadable_is_none():
    assert _adapter(_Mt5(None)).get_funds() is None
    assert _adapter(_Mt5([], acct=False)).get_funds() is None


class _Broker:
    name = "stub"

    def __init__(self, out):
        self._out = out

    def get_funds(self):
        return self._out


def _get(monkeypatch, out):
    from fastapi.testclient import TestClient

    import app.api.market_routes as routes
    from app.main import app

    monkeypatch.setattr(routes, "_broker_for", lambda asset_class, session: _Broker(out))
    return TestClient(app).get("/api/broker/funds")


def test_route_returns_the_record(db_session, monkeypatch):
    r = _get(monkeypatch, build([_op(1, 0, 500, "D-CC-USD-1")], ACCT))
    assert r.status_code == 200 and r.json()["totals"]["deposits"] == 500


def test_route_503_when_the_broker_cant_say(db_session, monkeypatch):
    r = _get(monkeypatch, None)
    assert r.status_code == 503 and "withdrawals" in r.json()["detail"]


def test_the_sim_broker_cannot_report_funds():
    from app.brokers.base import BrokerAdapter

    assert BrokerAdapter.get_funds(SimpleNamespace()) is None
