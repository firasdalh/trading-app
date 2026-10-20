"""State-changing API calls must carry the desk's own header (cross-site request protection)."""
from __future__ import annotations


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)          # no `with`: the lifespan (scheduler, broker reconcile) never starts


def test_a_cross_site_post_without_the_header_is_refused(db_session):
    """What a malicious page in another tab can send: a body-less POST, no custom headers."""
    r = _client().post("/api/execution/flatten")
    assert r.status_code == 403
    r = _client().post("/api/kill-switch/false")
    assert r.status_code == 403


def test_reads_are_unaffected(db_session):
    r = _client().get("/health")
    assert r.status_code == 200


def test_the_apps_own_requests_pass_the_guard(db_session):
    r = _client().post("/api/kill-switch/false", headers={"X-Desk": "1"})
    assert r.status_code != 403


def test_a_standby_backend_is_read_only(db_session, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "SCHEDULER_STANDBY", True)
    r = _client().post("/api/kill-switch/false", headers={"X-Desk": "1"})
    assert r.status_code == 503 and "STANDBY" in r.json()["detail"]
    assert _client().get("/health").status_code == 200


def test_a_rebound_hostname_is_refused(db_session):
    r = _client().get("/health", headers={"Host": "attacker.example:8001"})
    assert r.status_code == 400
