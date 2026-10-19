# Pre-registration: ranging fades lose (written 2026-10-20, BEFORE the holdout was examined)

## Origin — found post-hoc, never tested on unseen data
The 2026-10-14 enhancement research found the Hybrid's ranging-fade exception (`hybrid._MR_MIN_CONFIDENCE
= 0.60`, fades capped at conf 0.68 by `_MR_CONF_CAP`) admitted 73 fades worth −37.9R (−0.52R/trade) on the
original 33 symbols, negative in 5/5 quarters. It was flagged "found post-hoc → forward-test before
shipping" and never tested since.

## Hypothesis H_fade
Mean-reversion fades that pass the Hybrid gate (`strategy == "mean_reversion"` and `conf > 0.60`) LOSE
money on instruments never used in any analysis.

## Holdout (fixed in advance, already priced for an unrelated test)
`holdout/baseline.json` — the unchanged engine, live filter set, on the 16 never-used G10 pairs listed in
`ob_path_preregistration.md`. Net of the real spread of each trade's own entry bar.

## Decision rule
- **SUPPORTED** if the mean net R of gated fades is below 0 AND a one-sided bootstrap/permutation
  p < 0.05 against zero.
- **REJECTED** otherwise. No other symbol sets, windows or confidence bars to rescue it.
- Stated in advance: the holdout may contain very few gated fades. If n < 20 the result is reported as
  **INCONCLUSIVE (underpowered)** whatever its sign — it neither supports nor refutes.

## Validation step (in-sample, not the test)
The same code must first reproduce the Oct 14 figure on the original 33 (~73 fades, ~−0.5R) so the
holdout is tested with the definition that was found.

---

## RESULT (appended 2026-10-20 — nothing above was edited)

**Validation FAILED to reproduce the Oct 14 figure**, so the holdout cannot test the finding as it was found:

| sample | gated fades | mean | total | 95% CI |
|---|---|---|---|---|
| original 33 (validation, in-sample) | 37 | −0.096R | −3.6R | [−0.687, +0.542] |
| holdout 16 (the test) | 11 | −0.323R | −3.6R | [−1.226, +0.945] |

Not a boundary artefact: fade confidence takes only discrete values (0.50 / 0.55 / 0.60 / 0.65), and
`> 0.60` vs `>= 0.60` gives 37 vs 42 — neither is 73. The Oct 14 number came from an **all-bar scan**
(`scan_variants.py`, in that session's scratchpad, not in the repo); `bt.py` walks forward one trade at a
time with cooldowns, which is much closer to live. The Oct 14 notes already warned that the re-entry rule
"swings totals more than any candidate".

**Verdict: NOT CONFIRMED.** Under the live-like harness the lead shrinks from −0.52R to −0.10R/trade with
a CI spanning zero; the holdout is INCONCLUSIVE (n=11 < 20) by the rule set in advance. Both samples lean
negative, but the whole effect is ~3.6R/year across 33 symbols — too small to justify a change either way.
Drop this lead.
