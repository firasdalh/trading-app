# Pre-registration: opposing order block in the trade's path (written 2026-10-19, BEFORE the holdout ran)

## Why this exists
The finding below was discovered **post-hoc**, while analysing the `ob_target` variant. A pattern found
after looking is how the September session filter fooled this project (p=0.0017 in-sample, inverted on
a larger sample). So the test is fixed here, in writing, before a single holdout trade is priced.

## In-sample observation (33 symbols × 6,500 1h bars, faithful harness, true per-bar spread)
Baseline gated trend trades WITH an unmitigated opposing order block (strength ≥ 55) whose near edge
sits 1.5R–4R from entry: **n=85, −0.355R**. WITHOUT: **n=1,152, +0.149R**. Gap +0.504R, worse in all
4 folds, permutation p=0.0072.

## Hypothesis H1
On instruments never used in any analysis, gated trend trades from the **unchanged baseline engine**
that have such a block in their path have **lower** expectancy than those that do not.

## Holdout (fixed now — not to be edited after results)
The 16 unseen G10 FX crosses: AUDCADm, CADCHFm, EURAUDm, EURCADm, EURCHFm, EURGBPm, EURNZDm, GBPAUDm,
GBPCADm, GBPCHFm, GBPNZDm, NZDCADm, NZDCHFm, NZDJPYm, NZDUSDm, USDCADm.
Same harness, same 6,500 bars, same live filter set, same Hybrid gate (trend conf > 0.67).

## Exact condition (identical to the in-sample one)
At the decision bar, on the entry-timeframe window the engine sees: `find_order_blocks` →
`opposing_block(obs, direction, entry, min_strength=55)` → its near edge is 1.5R–4R from entry,
where R = |entry − stop|.

## Decision rule
- **SUPPORTED** if the gap (without − with) is positive AND one-sided permutation p < 0.05 on the holdout.
- **REJECTED** otherwise — including a positive gap with p ≥ 0.05. No re-running with other
  parameters, other symbols or other windows to rescue it.

## Known caveat, stated in advance
FX crosses are partly correlated with the FX pairs in the original 33 (EURAUD relates to EURUSD and
AUDUSD), so this holdout is not fully independent. A pass is strong evidence; it is not proof.

---

## RESULT (appended 2026-10-19 after the holdout ran — nothing above was edited)

Validation first: `ob_path_test.py` re-derived the in-sample numbers exactly (n=85, −0.355R vs n=1,152,
+0.149R, gap +0.504R), so the test applied to the holdout is the one registered above.

| holdout: 16 unseen G10 crosses | n | expectancy |
|---|---|---|
| WITH an opposing block 1.5R–4R ahead | 29 | −0.461R |
| WITHOUT | 538 | −0.213R |
| gap | | +0.248R, one-sided permutation **p = 0.243** |

**Verdict under the pre-registered rule: REJECTED.** The direction replicated (trades with a block in the
path were still worse) but the gap halved and the test is far from significant. Not re-run with other
parameters, symbols or windows.

Honest limitation, stated without using it as a rescue: only 29 holdout trades met the condition, so the
test had low power for an effect of this size (standard error ≈ 0.22R). "Rejected" here means "not
confirmed on independent data", not "shown to be false".

## The more important thing the holdout exposed
The unchanged baseline engine LOSES on the holdout: gated trend **−0.226R** (n=567, PF 0.74), negative in
all four folds, and negative even before costs (gross −0.104R). Inside the original 33 the picture is
consistent with that: forex was already ~zero (+0.010R, n=374), and **metals (+0.417R, n=275) supply about
80% of the book's net R**. Being independently verified at the time of writing — see the strategy review
notes before relying on it.
