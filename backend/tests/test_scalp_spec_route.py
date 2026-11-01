"""GET /api/market/scalp-spec: the scalp ticket's live lot economics (read-only)."""
from __future__ import annotations


class _Broker:
    name = "stub"

    def __init__(self, spec):
        self._spec = spec

    def scalp_spec(self, symbol):
        return self._spec


def _get(monkeypatch, spec):
    from fastapi.testclient import TestClient

    import app.api.market_routes as routes
    from app.main import app

    monkeypatch.setattr(routes, "_broker_for", lambda asset_class, session: _Broker(spec))
    return TestClient(app).get("/api/market/scalp-spec", params={"symbol": "EURUSDm", "asset_class": "forex"})


def test_returns_the_brokers_spec(db_session, monkeypatch):
    r = _get(monkeypatch, {"symbol": "EURUSDm", "bid": 1.1, "ask": 1.1002, "account_leverage": 2000})
    assert r.status_code == 200
    assert r.json()["account_leverage"] == 2000


def test_a_broker_that_cannot_price_the_symbol_is_a_503(db_session, monkeypatch):
    r = _get(monkeypatch, None)
    assert r.status_code == 503
    assert "EURUSDm" in r.json()["detail"]
