"""Hybrid activity dashboard: per-tick stats recording + the /api/hybrid/stats aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.settings_routes import hybrid_stats
from app.models.db import AgentRun, ConditionalSetup


def _tick(*, opened=None, **stats) -> AgentRun:
    return AgentRun(agent="hybrid", event="tick",
                    detail={"opened": opened, "reason": "x", "stats": stats})


def test_hybrid_stats_aggregates_today(db_session):
    now = datetime.now(timezone.utc)

    # Two real scans today: one opened a direct trade + AI confirm, one AI veto.
    db_session.add(_tick(reached_scan=True, scanned=5, candidates=2, skipped_low_conf=1,
                         ai_review="confirm", opened={"symbol": "EURUSDm", "direction": "long"}))
    db_session.add(_tick(reached_scan=True, scanned=4, candidates=1, skipped_low_conf=2,
                         ai_review="veto"))
    # Early bail (kill-switch / no room) — reached_scan False, so NOT counted as a scan.
    db_session.add(_tick(reached_scan=False))
    # A non-hybrid agent run must be ignored entirely.
    db_session.add(AgentRun(agent="scanner", event="tick",
                            detail={"stats": {"reached_scan": True, "candidates": 99}}))
    # Yesterday's hybrid run must fall outside the "today" window.
    stale = _tick(reached_scan=True, scanned=9, candidates=9, skipped_low_conf=9, ai_review="confirm")
    stale.created_at = now - timedelta(days=1)
    db_session.add(stale)

    # Hybrid-armed conditionals created today (one still armed, one triggered today).
    db_session.add(ConditionalSetup(symbol="XAUUSDm", asset_class="metals", timeframe="1h",
                                    direction="long", order_type="buy_stop", trigger_price=2000.0,
                                    status="armed", source="hybrid"))
    trig = ConditionalSetup(symbol="US30m", asset_class="indices", timeframe="1h",
                            direction="long", order_type="buy_stop", trigger_price=40000.0,
                            status="triggered", source="hybrid")
    trig.triggered_at = now
    db_session.add(trig)
    # A manually-armed setup must NOT count toward the Hybrid tally.
    db_session.add(ConditionalSetup(symbol="GBPUSDm", asset_class="forex", timeframe="1h",
                                    direction="short", order_type="sell_stop", trigger_price=1.2,
                                    status="armed", source="manual"))
    db_session.commit()

    out = hybrid_stats(db_session)
    assert out.scans == 2                 # only the two reached_scan hybrid ticks today
    assert out.candidates == 3            # 2 + 1
    assert out.skipped_low_conf == 3      # 1 + 2
    assert out.ai_confirmed == 1
    assert out.ai_rejected == 1
    assert out.direct_trades == 1         # the one tick with an 'opened'
    assert out.armed_setups == 2          # both hybrid conditionals created today (manual excluded)
    assert out.triggered_armed == 1       # the one with triggered_at today
    assert out.accept_rate == 0.5         # 1 confirm / (1 confirm + 1 veto)
    assert out.last_opened == "EURUSDm long"  # most recent hybrid run carrying an 'opened'


def test_hybrid_stats_empty(db_session):
    out = hybrid_stats(db_session)
    assert (out.scans, out.candidates, out.ai_confirmed, out.ai_rejected,
            out.direct_trades, out.armed_setups, out.triggered_armed, out.skipped_low_conf) == \
        (0, 0, 0, 0, 0, 0, 0, 0)


def test_run_hybrid_records_stats(db_session, monkeypatch):
    """A Hybrid tick writes a stats block into its AgentRun so the dashboard can total it."""
    import app.agents.conditional as cond
    import app.agents.hybrid as hybrid
    from app.models.db import WatchItem
    from app.models.enums import AssetClass, Direction, RiskDecisionType
    from app.models.schemas import RiskDecision, TradeProposal

    db_session.add(WatchItem(symbol="EURUSDm", asset_class="forex", timeframe="1h", enabled=True))
    cfg = hybrid.get_or_create_hybrid_config(db_session)
    cfg.enabled = True
    db_session.commit()

    # A directional setup that is risk-approved but BELOW the confidence bar → skipped_low_conf.
    prop = TradeProposal(symbol="EURUSDm", asset_class=AssetClass.FOREX, direction=Direction.LONG,
                         confidence=0.40)
    dec = RiskDecision(decision=RiskDecisionType.APPROVED, approved=True, reason="ok", symbol="EURUSDm")
    monkeypatch.setattr(hybrid, "preview_symbol", lambda *a, **k: (prop, dec))
    monkeypatch.setattr(hybrid, "live_broker_positions", lambda s: [])
    monkeypatch.setattr(hybrid, "kill_switch_active", lambda s: False)
    monkeypatch.setattr(cond, "live_broker_positions", lambda s: [])

    hybrid.run_hybrid(db_session)

    run = db_session.query(AgentRun).filter(AgentRun.agent == "hybrid").order_by(
        AgentRun.id.desc()).first()
    assert run is not None
    stats = (run.detail or {}).get("stats") or {}
    assert stats.get("reached_scan") is True
    assert stats.get("scanned") == 1
    assert stats.get("skipped_low_conf") == 1
    assert stats.get("candidates") == 0

    out = hybrid_stats(db_session)
    assert out.scans == 1 and out.skipped_low_conf == 1 and out.candidates == 0
