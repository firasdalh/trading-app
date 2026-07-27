"""Repeatability probe for the AI classifiers at the deterministic engine's gates 5 & 6.

The reviewer's open question: the momentum + price-action classifiers run at temp-0 with a fixed
seed, but that's *best-effort* — are they actually reproducible run-to-run? This runs the SAME fixed
snapshot through each classifier N times (clearing the module cache each pass so every call really
hits the model) and reports how often the CATEGORY flips and how much the confidence moves.

Interpreting the result:
  • flip_rate 0.0 across passes  -> stable; the "temp-0 + seed" pinning holds; leave gates 5/6 on AI.
  • flip_rate > ~0.1             -> the label is not reproducible; replace those forks with a
                                    deterministic rule (MACD slope + HTF alignment; distance-to-level
                                    + wick ratio) or raise the confidence floor.

Cost: it makes live model calls, so it SPENDS TOKENS — ``main`` only runs when an LLM key is
configured, and does nothing (no spend) otherwise. The summary math is pure and unit-tested offline.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace


def summarize(categories: list[str], confidences: list[float]) -> dict:
    """Pure summary of one classifier's N identical-input passes. No I/O — unit-tested offline."""
    n = len(categories)
    if n == 0:
        return {"n": 0, "mode": None, "flip_rate": None, "distinct": [],
                "conf_min": None, "conf_max": None, "conf_spread": None}
    mode, _ = Counter(categories).most_common(1)[0]
    flips = sum(1 for c in categories if c != mode)
    return {
        "n": n,
        "mode": mode,
        "flip_rate": round(flips / n, 3),
        "distinct": sorted(set(categories)),
        "conf_min": round(min(confidences), 3) if confidences else None,
        "conf_max": round(max(confidences), 3) if confidences else None,
        "conf_spread": round(max(confidences) - min(confidences), 3) if confidences else None,
    }


@dataclass
class ScenarioResult:
    scenario: str
    classifier: str
    categories: list[str] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        return summarize(self.categories, self.confidences)


def _tf(timeframe: str, trend: str, indicators: dict) -> SimpleNamespace:
    return SimpleNamespace(timeframe=timeframe, trend=trend, indicators=indicators)


# --- Fixed, representative ambiguous-fork snapshots (the whole point is IDENTICAL input each pass) ---

def _momentum_scenarios() -> list[dict]:
    # A long whose entry-TF MACD is rolling over while the 4h still supports it — the classic
    # healthy_pullback / weak / reversal fork.
    pullback_ind = {"macd_hist": -0.15, "macd_hist_prev": -0.30, "atr14": 1.0, "rsi14": 45,
                    "rsi14_prev": 48, "adx": 24, "plus_di": 23, "minus_di": 17,
                    "ema20": 100.0, "last_close": 100.4}
    pullback_tech = SimpleNamespace(timeframes=[
        _tf("1h", "up", pullback_ind),
        _tf("4h", "up", {"macd_hist": 0.25}),
    ])
    reversal_ind = {"macd_hist": -0.6, "macd_hist_prev": -0.2, "atr14": 1.0, "rsi14": 41,
                    "rsi14_prev": 52, "adx": 19, "plus_di": 16, "minus_di": 24,
                    "ema20": 100.0, "last_close": 98.8}
    reversal_tech = SimpleNamespace(timeframes=[
        _tf("1h", "up", reversal_ind),
        _tf("4h", "down", {"macd_hist": -0.3}),
    ])
    return [
        {"scenario": "long pullback (4h still up)", "symbol": "EURUSDm", "direction": "long",
         "tf": "1h", "ind": pullback_ind, "technical": pullback_tech},
        {"scenario": "long, entry-TF rolling over (4h flipped down)", "symbol": "EURUSDm",
         "direction": "long", "tf": "1h", "ind": reversal_ind, "technical": reversal_tech},
    ]


def _priceaction_scenarios() -> list[dict]:
    reject_ind = {"last_close": 99.5, "atr14": 1.0, "macd_hist": 0.05, "macd_hist_prev": 0.08,
                  "rsi14": 62, "rsi14_prev": 65, "adx": 20, "plus_di": 21, "minus_di": 19,
                  "chan_r2": 0.7, "chan_pos": 0.9, "vol_trend": -1, "structure": 1, "choch": 0}
    break_ind = {"last_close": 99.7, "atr14": 1.0, "macd_hist": 0.35, "macd_hist_prev": 0.20,
                 "rsi14": 66, "rsi14_prev": 61, "adx": 31, "plus_di": 28, "minus_di": 14,
                 "chan_r2": 0.85, "chan_pos": 1.02, "vol_trend": 1, "structure": 1, "choch": 0}
    return [
        {"scenario": "long into resistance, momentum fading", "symbol": "EURUSDm", "direction": "long",
         "tf": "1h", "level": 100.0, "ind": reject_ind},
        {"scenario": "long pressing through resistance", "symbol": "EURUSDm", "direction": "long",
         "tf": "1h", "level": 100.0, "ind": break_ind},
    ]


def run_repeatability(passes: int = 5) -> list[ScenarioResult]:
    """Call each classifier ``passes`` times on identical input (SPENDS TOKENS). The module cache is
    cleared before every call so each pass is a real model round-trip, not a cached echo."""
    import app.agents.momentum_read as mr
    import app.agents.priceaction_read as pr

    out: list[ScenarioResult] = []
    for scn in _momentum_scenarios():
        res = ScenarioResult(scenario=scn["scenario"], classifier="momentum")
        for _ in range(passes):
            mr._CACHE.clear()
            r = mr.interpret_momentum(scn["symbol"], scn["direction"], scn["ind"],
                                      scn["technical"], scn["tf"])
            if r is not None:
                res.categories.append(r.category)
                res.confidences.append(round(float(r.confidence), 3))
        out.append(res)
    for scn in _priceaction_scenarios():
        res = ScenarioResult(scenario=scn["scenario"], classifier="price-action")
        for _ in range(passes):
            pr._CACHE.clear()
            r = pr.interpret_price_action(scn["symbol"], scn["direction"], scn["level"],
                                          scn["ind"], scn["tf"])
            if r is not None:
                res.categories.append(r.category)
                res.confidences.append(round(float(r.confidence), 3))
        out.append(res)
    return out


def format_report(results: list[ScenarioResult]) -> str:
    lines = ["=" * 82,
             "AI CLASSIFIER REPEATABILITY — identical input, N passes (gates 5 & 6)",
             "=" * 82,
             "flip_rate 0.00 = the label never changed (reproducible). > ~0.10 = drifting.",
             "-" * 82]
    worst = 0.0
    for r in results:
        s = r.summary
        if s["n"] == 0:
            lines.append(f"[{r.classifier}] {r.scenario}: no result (LLM returned None every pass)")
            continue
        worst = max(worst, s["flip_rate"] or 0.0)
        cats = ", ".join(f"{c}×{n}" for c, n in Counter(r.categories).most_common())
        lines.append(f"[{r.classifier}] {r.scenario}")
        lines.append(f"    passes={s['n']}  flip_rate={s['flip_rate']:.2f}  mode={s['mode']}")
        lines.append(f"    categories: {cats}")
        lines.append(f"    confidence: {s['conf_min']}–{s['conf_max']} (spread {s['conf_spread']})")
    lines.append("-" * 82)
    verdict = ("STABLE — pinning holds, gates 5/6 can stay on AI" if worst == 0.0
               else "MINOR DRIFT — watch it / raise the confidence floor" if worst <= 0.1
               else "UNSTABLE — replace these forks with a deterministic rule")
    lines.append(f"Worst flip_rate: {worst:.2f}  ->  {verdict}")
    lines.append("=" * 82)
    return "\n".join(lines)


def main() -> None:
    from app.agents.llm import llm_available

    passes = 5
    if not llm_available():
        print("No LLM configured (no API key found) — nothing was called, no tokens spent.")
        print("Set your OpenAI key in Settings → AI model, then re-run:")
        print("    python -m app.backtest.classifier_repeatability")
        print(f"With a key it makes ~{4 * passes} live classifier calls to measure run-to-run drift.")
        return
    print(f"Running {passes}× identical-input repeatability (momentum + price-action)… "
          "this makes live model calls.")
    print(format_report(run_repeatability(passes=passes)))


if __name__ == "__main__":
    main()
