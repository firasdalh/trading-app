"""Task 6 — Monte Carlo drawdown / ruin across risk-per-trade levels.

Bootstraps the system's ACTUAL backtested R-multiple distribution (from analysis/entries.json) into
many random trade sequences, at a range of risk-per-trade values, and reports for each level:
  - max-drawdown distribution (median / 95th percentile),
  - probability of ACCOUNT RUIN, and
  - probability of breaching the daily-loss circuit breaker at least once.

RUIN THRESHOLD: default = a 50% peak-to-trough drawdown (equity <= 0.50 x its running peak). This is
a common "account is effectively dead" line; change RUIN_DD below if you define ruin differently
(e.g. 0.30 for a 30% prop-firm-style limit). We also report P(DD>=20%) and P(DD>=30%) so you can read
off other thresholds without re-running.

Standalone: reads only the cached dataset (no broker). Run: PYTHONPATH=backend python analysis/drawdown_simulation.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
ENTRIES = HERE / "entries.json"
OUT = HERE / "drawdown_simulation.md"

RISK_LEVELS = [0.0025, 0.005, 0.01, 0.015, 0.02]   # 0.25% .. 2% risk per trade
MAX_DAILY_LOSS = 0.03      # RISK.md daily-loss breaker default (3% of equity)
RUIN_DD = 0.50             # ruin = a 50% peak-to-trough drawdown
HORIZON = 250              # trades per simulated path (~a multi-month active-trading run)
PATHS = 20000
SEED = 7


def _trades_per_day(entries: list[dict]) -> float:
    days = set()
    for e in entries:
        t = e.get("time")
        if t:
            days.add(t[:10])
    n_days = max(1, len(days))
    return max(1.0, round(len(entries) / n_days, 1))


def main():
    entries = json.loads(ENTRIES.read_text())
    R = np.array([e["r"] for e in entries if e.get("r") is not None], dtype=float)
    n = len(R)
    if n < 20:
        OUT.write_text(f"# Drawdown simulation\n\nNot enough trades ({n}).\n", encoding="utf-8"); return

    win_rate = float(np.mean(R > 0))
    exp_r = float(np.mean(R))
    tpd = _trades_per_day(entries)
    days_per_path = HORIZON // int(round(tpd)) if tpd >= 1 else HORIZON
    rng = np.random.default_rng(SEED)

    lines = ["# Task 6 — Drawdown & ruin Monte Carlo", ""]
    lines.append(f"Bootstrapped from **{n} backtested trades** — win rate **{win_rate*100:.1f}%**, "
                 f"expectancy **{exp_r:+.3f}R/trade**, avg **{tpd:.1f} trades/day**. "
                 f"{PATHS:,} paths x {HORIZON} trades each. "
                 f"Ruin = {int(RUIN_DD*100)}% peak-to-trough drawdown. Daily breaker = {int(MAX_DAILY_LOSS*100)}% "
                 "day loss.")
    if exp_r <= 0:
        lines.append("")
        lines.append("> **The bootstrapped edge is non-positive (expectancy <= 0R).** With a negative "
                     "expectancy, drawdown and ruin grow with BOTH the risk level and the horizon — "
                     "position sizing cannot rescue a losing distribution. Read the table as *how fast* "
                     "each risk level fails, not as a safe-sizing menu; the real fix is the edge "
                     "(filters / entry timing), not the size. This mirrors the funnel-vs-filtered "
                     "finding: the raw deterministic signal across all symbols is a thin/negative "
                     "distribution; the live edge comes from the AI-review + armed sub-strategies.")

    lines += ["", "## Results by risk-per-trade", "",
              "| risk/trade | median maxDD | 95th-pct maxDD | P(DD>=20%) | P(DD>=30%) | "
              f"P(ruin {int(RUIN_DD*100)}%) | P(daily-breaker) |",
              "|---|---|---|---|---|---|---|"]

    flags = []
    for risk in RISK_LEVELS:
        draws = rng.choice(R, size=(PATHS, HORIZON), replace=True)
        step = 1.0 + risk * draws
        step = np.clip(step, 1e-6, None)              # equity can't go below ~0 in one trade
        equity = np.cumprod(step, axis=1)
        peak = np.maximum.accumulate(equity, axis=1)
        dd = 1.0 - equity / peak
        max_dd = dd.max(axis=1)
        p_ruin = float(np.mean(max_dd >= RUIN_DD))
        p_dd20 = float(np.mean(max_dd >= 0.20))
        p_dd30 = float(np.mean(max_dd >= 0.30))
        med_dd = float(np.median(max_dd))
        p95_dd = float(np.percentile(max_dd, 95))

        # Daily-breaker breach: group trades into days of ~tpd, arithmetic day P&L in equity fractions.
        d = int(round(tpd))
        usable = (HORIZON // d) * d
        day_pnl = (risk * draws[:, :usable]).reshape(PATHS, usable // d, d).sum(axis=2)
        p_breaker = float(np.mean(np.any(day_pnl <= -MAX_DAILY_LOSS, axis=1)))

        lines.append(f"| {risk*100:.2f}% | {med_dd*100:.0f}% | {p95_dd*100:.0f}% | {p_dd20*100:.0f}% "
                     f"| {p_dd30*100:.0f}% | {p_ruin*100:.1f}% | {p_breaker*100:.0f}% |")
        if p_ruin >= 0.05 or p95_dd >= 0.40:
            flags.append((risk, p_ruin, p95_dd))

    lines += ["", "## Flags", ""]
    if flags:
        for risk, pr, p95 in flags:
            lines.append(f"- **{risk*100:.2f}%/trade looks statistically unsafe**: P(ruin) "
                         f"{pr*100:.1f}%, 95th-pct drawdown {p95*100:.0f}%.")
    else:
        lines.append("- No risk level breached the unsafe thresholds (P(ruin) >= 5% or 95th-pct DD "
                     ">= 40%) over this horizon.")
    lines += ["",
              "## Notes / assumptions",
              f"- Compounding: each trade risks the given fraction of CURRENT equity; P&L = risk x equity x R.",
              f"- Bootstrap assumes trades are i.i.d. draws from the historical R distribution — it "
              "ignores serial correlation (streaks/regime clustering), so REAL drawdowns are usually "
              "somewhat worse than shown.",
              f"- Daily-breaker probability groups trades into days of ~{int(round(tpd))} and uses an "
              "arithmetic day loss; it approximates the realized+floating breaker (Task 4), which trips "
              "intraday.",
              f"- Horizon = {HORIZON} trades; ruin/breaker probabilities scale with horizon.",
              "- Position-sizing defaults were NOT changed by this script — it only reports.",
              ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} (win={win_rate*100:.1f}%, exp={exp_r:+.3f}R, tpd={tpd})")


if __name__ == "__main__":
    main()
