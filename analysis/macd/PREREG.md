# Pre-registration: two entry conditions found in-sample (written 2026-10-29, BEFORE the holdout ran)

## Why this exists
The in-sample search (`evaluate.py`, about 50 variants) covered:
- the MACD-histogram gate and the MACD confidence points;
- a MACD-cross bonus or condition;
- a supply/demand bonus or condition;
- last week's high/low as a bonus or condition.

It produced two conditions that improved the trades the live engine takes. Both were picked out of that search, so their in-sample p-values (about 0.03) overstate them. They are fixed here, in writing, before a single holdout trade is priced.

## In-sample result
Data: 33 symbols, 2025-04-30 → 2026-10-28, faithful harness, the live engine (trend-only ON, live filter set, AI off). The Hybrid bar is confidence > 0.68.

| Hybrid-eligible decisions | with | without | gap | one-sided p |
|---|---|---|---|---|
| H1 MACD cross with the trade within 6 bars | +0.255R (516) | −0.013R (2,194) | +0.268R | 0.030 |
| H2 supply/demand bounce within 3 bars | +0.230R (440) | +0.001R (2,270) | +0.230R | 0.035 |

Both are much weaker in the last 30% of the period than in the first 70%:
- H1 "with": +0.040R in the last 30% vs +0.423R in the first 70%.
- H2 "with": +0.066R vs +0.325R.

In both halves they still beat the decisions without them.

## Hypotheses
**H1.** Take the Hybrid-eligible decisions of the unchanged live engine. Those with a MACD signal-line cross in the trade's direction within the last 6 1h bars have higher expectancy than those without. A cross is the histogram changing sign; bar 0 is the decision bar; the histogram must still be on the trade's side.

**H2.** Among the same decisions, the ones with a supply/demand bounce have higher expectancy than those without. A bounce means both of these:
- Within the last 3 1h bars, price touched a zone on the trade's side. Zones are the app's 1h order blocks: demand under a long, supply over a short. The zone must already have been confirmed at that bar.
- The decision bar closes back out of the zone on the trade's side.

## Holdout (fixed now)
20 symbols, 7,000 1h bars each, fetched 2026-10-29 by `fetch_holdout.py`. None of them was used to develop or tune any rule in this repo.
- **14 G10 crosses:** CADCHFm, EURAUDm, EURCHFm, EURGBPm, EURNZDm, GBPAUDm, GBPCADm, GBPCHFm, GBPNZDm, NZDCADm, NZDCHFm, NZDJPYm, NZDUSDm, USDCADm. They served once as the holdout of the order-block test (2026-10-19), which was a different hypothesis.
- **Five metals:** XALUSDm, XCUUSDm, XNIUSDm, XPBUSDm, XPDUSDm.
- **DXYm.**

Everything else is unchanged: the harness (`replay.py`), the engine and filters, and the Hybrid bar.

## Test (`holdout_test.py`)
For each hypothesis, gap = mean net R with the condition − mean net R without it. It is computed over the holdout's Hybrid-eligible live decisions: every bar, path-independent, R net of spread.

The one-sided p is the share of 5,000 bootstrap resamples, each drawing whole symbol-weeks with seed 11, whose gap is ≤ 0.

## Decision rule
- **SUPPORTED:** gap > 0 and p < 0.05. That makes it a candidate for a live change, which stays the user's decision.
- **REJECTED** otherwise. There will be no re-running with other windows, symbols or periods.
- **Inconclusive, counted as not supported:** either group has fewer than 50 decisions.

## Known caveats, stated in advance
- **Not fully independent:** the FX crosses are correlated with the in-sample FX pairs.
- **The engine already lost on these crosses:** on the earlier holdout it made −0.226R a trade. So this test asks whether the condition picks better trades (the gap), not whether the engine is profitable on these symbols.

---

## RESULT
(appended after the holdout ran; nothing above is edited)

Validation first: `holdout_test.py` reproduced the in-sample numbers exactly (H1 path: 232 trades, +53.3R; H2 path: 236 trades, +55.2R). The test applied to the holdout is the one registered above.

Holdout: 20 symbols, 1,647 Hybrid-eligible live decisions, mean −0.248R.

| hypothesis | with | without | gap | one-sided p | verdict |
|---|---|---|---|---|---|
| H1 MACD cross within 6 bars | −0.161R (297) | −0.267R (1,350) | +0.106R | 0.249 | **REJECTED** |
| H2 supply/demand bounce within 3 bars | −0.434R (234) | −0.217R (1,413) | **−0.217R** | 0.885 | **REJECTED** |

- **H1:** the direction held, but the gap shrank to less than half its in-sample size and is far from significant. In the first 70% of the holdout it was the wrong way round: "with" made −0.291R, "without" −0.258R.
- **H2:** it reversed. Trades right after a zone bounce were worse.

Neither is re-run with other windows, symbols or periods.

### What else the holdout showed (not part of the test)
- **The MACD-histogram gate the engine already uses holds up on the holdout.**
  - Without it: −227.6R instead of −145.0R, worse in all four folds.
  - The trades it blocks: −0.366R each (95% CI −0.54 to −0.17), vs −0.248R for the trades it lets through.
- **The unchanged engine loses on these 20 symbols:** 548 trades, −0.265R a trade, −145R in total.
  - G10 crosses: −0.187R (377 trades).
  - Base metals and palladium: −0.526R (142 trades).
  - DXY: +0.010R (29 trades).
- **This matches the 2026-10-19 holdout:** the in-sample edge is instrument-specific, not general.

### Post-hoc note, 2026-10-29 (after the verdicts; it does not change them)
The harness charged each trade its spread, but it did not apply the live risk manager's spread gate. Live refuses a trade when the spread is above 15% of the stop distance. Re-running the same numbers with that gate:

| | in-sample (33) | holdout (20) |
|---|---|---|
| live engine, Hybrid path | +79.3R (740 trades, +0.107R) | −19.0R (319 trades, −0.060R) |
| without the MACD-histogram gate | +11.4R | −53.2R |
| trades the MACD-histogram gate blocks | −0.136R each | −0.149R each |
| H1 gap | +0.320R, p = 0.016 | **+0.346R, p = 0.070** |
| H2 gap | +0.074R, p = 0.28 | −0.177R, p = 0.80 |

- **The MACD-histogram gate:** the conclusion is unchanged.
- **H2:** fails either way.
- **H1:** looks stronger on the holdout once the gate is applied, but it still misses p < 0.05. Changing the test after seeing the result would be exactly the rescue the rule forbids, so H1 stays **rejected**. It is the one idea worth re-testing on data that does not exist yet: the next 3–6 months of live and replayed trades.
