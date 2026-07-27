"""Classifier repeatability harness — the pure summary math + the mocked run loop (no tokens)."""
from __future__ import annotations

import app.backtest.classifier_repeatability as rep


def test_summarize_stable_run():
    s = rep.summarize(["healthy_pullback"] * 5, [0.7, 0.7, 0.71, 0.7, 0.69])
    assert s["n"] == 5
    assert s["flip_rate"] == 0.0
    assert s["mode"] == "healthy_pullback"
    assert s["distinct"] == ["healthy_pullback"]
    assert s["conf_spread"] == round(0.71 - 0.69, 3)


def test_summarize_drift_run():
    s = rep.summarize(["healthy_pullback", "healthy_pullback", "weak_momentum",
                       "healthy_pullback", "probable_reversal"], [0.6] * 5)
    assert s["flip_rate"] == round(2 / 5, 3)  # 2 of 5 differ from the mode
    assert s["mode"] == "healthy_pullback"
    assert len(s["distinct"]) == 3


def test_summarize_empty():
    s = rep.summarize([], [])
    assert s["n"] == 0 and s["flip_rate"] is None and s["mode"] is None


def test_run_loop_uses_mocked_classifiers(monkeypatch):
    """The run loop clears the cache and collects each pass — exercised with a stable mock (no LLM)."""
    import app.agents.momentum_read as mr
    import app.agents.priceaction_read as pr

    class _M:
        category = "healthy_pullback"
        confidence = 0.72

    class _P:
        category = "likely_reject"
        confidence = 0.66

    monkeypatch.setattr(mr, "interpret_momentum", lambda *a, **k: _M())
    monkeypatch.setattr(pr, "interpret_price_action", lambda *a, **k: _P())
    results = rep.run_repeatability(passes=4)
    assert len(results) == 4  # 2 momentum + 2 price-action scenarios
    for r in results:
        assert r.summary["n"] == 4
        assert r.summary["flip_rate"] == 0.0  # a deterministic mock never flips


def test_report_flags_unstable(monkeypatch):
    import app.agents.momentum_read as mr
    import app.agents.priceaction_read as pr

    seq = iter(["healthy_pullback", "weak_momentum", "probable_reversal", "healthy_pullback"] * 4)

    class _M:
        def __init__(self):
            self.category = next(seq)
            self.confidence = 0.6

    monkeypatch.setattr(mr, "interpret_momentum", lambda *a, **k: _M())
    monkeypatch.setattr(pr, "interpret_price_action", lambda *a, **k: None)  # PA returns nothing
    text = rep.format_report(rep.run_repeatability(passes=4))
    assert "UNSTABLE" in text or "DRIFT" in text
