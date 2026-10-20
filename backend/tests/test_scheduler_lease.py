"""Only one backend may run the scheduler (a second one would double-fire arms and the Hybrid)."""
from __future__ import annotations


def test_a_second_backend_gets_no_lease(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_LEASE_PORT", 8798)   # not the live app's port
    first = main._take_scheduler_lease()
    assert first is not None and main.SCHEDULER_STANDBY is False
    try:
        second = main._take_scheduler_lease()
        assert second is None and main.SCHEDULER_STANDBY is True
    finally:
        first.close()
        monkeypatch.setattr(main, "SCHEDULER_STANDBY", False)
    again = main._take_scheduler_lease()             # freed as soon as the holder closes / dies
    assert again is not None
    again.close()
