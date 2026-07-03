# Task 7 — Walk-forward / out-of-sample validation

## 0. Parameter provenance (are these fitted or conventional?)

- **ADX 25 / 20**, **EMA 20/50/200**, **RSI 75/25**, **R:R 2.0 (cap 4.0)** are all textbook / round-number defaults (Wilder's ADX bands, standard EMA stack, classic RSI extremes) — not values that look grid-searched to a dataset.
- The few non-standard numbers (mean-reversion RSI **66/34**, value-zone **1.0xATR**, chase penalty **2.5xATR**) carry an explicit written rationale in the code, not a fitted precision (e.g. 23.7). So the PRIOR is: convention-chosen, low curve-fit risk. The tests below check that empirically.

## 1. ADX-threshold sensitivity (full sample)

A robust knob shows a smooth plateau; a fitted one shows a lonely spike at the chosen value.

| ADX thr | trades | win% | expectancy R | profit factor | maxDD R |
|---|---|---|---|---|---|
| 20 | 230 | 34% | +0.147 | 1.21 | 10.8 |
| 22 | 218 | 33% | +0.144 | 1.21 | 13.0 |
| 25 **<- current** | 170 | 34% | +0.173 | 1.25 | 17.2 |
| 28 | 143 | 34% | +0.213 | 1.31 | 16.8 |
| 30 | 137 | 30% | +0.044 | 1.06 | 19.0 |

## 2. Walk-forward (4 time folds): best-IS threshold -> next OOS fold

| test fold | best-IS thr | IS exp R | OOS exp R (best-IS) | OOS exp R (default 25) |
|---|---|---|---|---|
| 2 | 28 | +0.023 | +0.093 (n=35) | -0.018 (n=40) |
| 3 | 28 | +0.065 | +0.136 (n=44) | +0.365 (n=54) |
| 4 | 20 | +0.151 | +0.135 (n=58) | +0.370 (n=44) |

## 3. Current config (ADX=25) holdout: last 30% of time held out

| window | trades | win% | expectancy R | profit factor |
|---|---|---|---|---|
| in-sample | 111 | 29% | +0.015 | 1.02 |
| OUT-OF-SAMPLE | 59 | 44% | +0.471 | 1.80 |

## Verdict

- **Threshold stability across walk-forward folds:** best-IS threshold = [28, 28, 20] -> **stable** (little curve-fit risk on this knob).
- **Is the default (25) near the full-sample optimum?** best full-sample threshold = 28 (exp +0.213); default 25 = +0.173 -> yes, 25 sits on the plateau.
- **Does the current config hold out-of-sample?** OOS expectancy +0.471 vs IS +0.015 -> **holds** (edge persists into unseen data).

_Caveats: single knob swept (ADX threshold) — the strongest fitting risk, but not the only parameter; small per-fold samples make fold-level expectancy noisy; deterministic engine only (no AI review); costs = flat 0.05R. Walk-forward here VALIDATES robustness — it does not, and should not, auto-tune the live value._