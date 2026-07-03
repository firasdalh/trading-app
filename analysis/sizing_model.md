# Task 14 — Position sizing: flat vs confidence-scaled (decision doc)

**Decision document — nothing is implemented.** Recommendation for your review.

## The question
Should position size be **flat** (same % risk every trade) or **scaled by the confidence score**
(bigger size on higher-confidence setups)?

## The prerequisite for confidence-scaling to help
Sizing up on confidence only improves risk-adjusted return if confidence is a **calibrated,
monotonic** predictor of edge — i.e. higher confidence ⇒ reliably higher expectancy. If it isn't,
scaling just puts more money on a noisy number and, because drawdown scales with risk (Task 6),
*amplifies* the damage when the score is wrong.

## What the data says (170 backtested trades, realistic R)
Correlation between confidence and realized R = **0.04 (essentially zero)**. By bucket:

| confidence | trades | win% | expectancy R |
|---|---|---|---|
| 0–50% | 14 | 43% | +0.445 |
| 50–60% | 23 | 22% | **−0.293** |
| 60–70% | 24 | 25% | **−0.101** |
| 70–80% | 41 | 34% | +0.182 |
| 80–100% | 68 | 40% | **+0.376** |

The relationship is **non-monotonic and noisy**: the middle band (50–70%) is a net *loser*, the top
band (80%+) is the best, and the tiny 0–50% bucket is high but on 14 trades (noise). There is a weak
"top bucket is better" signal, but **no smooth confidence→edge curve** to size along.

## Analysis
- **Confidence-scaled sizing (size ∝ confidence):** would size the 50–70% band *up* (mid-size) even
  though it's the negative band, and relies on a score with ~0 linear correlation to outcome. It
  fails the prerequisite. Added calibration/complexity risk with no demonstrated payoff — and Task 6
  shows the downside (bigger size → deeper drawdown) is real and asymmetric.
- **Flat sizing:** robust to the miscalibration we actually observe; the safe band (0.25–0.5%/trade,
  Task 6) already controls drawdown. Simple, testable, no new failure mode.
- **The real signal in this data is SELECTION, not scaling:** the 50–70% band is a drag and the 80%+
  band carries the edge. That argues for a **confidence FLOOR** (skip / de-emphasize 50–70%), which
  is a *filter/threshold* decision — validate it separately (small samples; don't overfit these
  buckets) — NOT a sizing model.

## Recommendation
1. **Keep FLAT risk-per-trade** — do not implement confidence-scaled sizing. It isn't justified by
   the evidence and adds calibration risk to the money path.
2. **Start at 0.5%/trade** (Task 6 safe band), flat.
3. **Separately consider** raising the effective confidence floor (the Hybrid `min_confidence` and/or
   a funnel gate) to trim the negative 50–70% band — but only after the divergence review / more
   trades confirm the bucket pattern isn't sample noise. That's a filter change to validate, not a
   sizing change.
4. **Re-check** this once confidence is re-calibrated on live paper data (`/api/journal/calibration`):
   if a genuine monotonic confidence→edge curve emerges with a real sample, revisit scaling then.

_Inputs: Task 6 (`drawdown_simulation.md`), Task 8 (`execution_realism.md`, realistic R), and the
per-confidence-bucket expectancy above. No code changed._
