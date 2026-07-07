# Market-map (regression-channel level-proximity) — does it help?

249 actionable setups. Comparing the >= 70% subset (what the Hybrid trades) with vs without the channel factor.

| variant | trades >=70% | win% | expectancy R | total R |
|---|---|---|---|---|
| WITH channel | 115 | 35.7% | +0.138 | +15.8 |
| WITHOUT channel | 121 | 35.5% | +0.161 | +19.5 |

## In-sample vs out-of-sample (last 30% held out)

| window | WITH win% / exp | WITHOUT win% / exp |
|---|---|---|
| in-sample | 39% / +0.299 | 38% / +0.266 |
| OUT-OF-SAMPLE | 28% / -0.216 | 30% / -0.050 |

## Verdict

- Full sample: WITH **+0.138R** vs WITHOUT **+0.161R** (delta -0.024R).
- **Does NOT reliably beat WITHOUT (or fails out-of-sample) -> keep it OFF.** The channel factor doesn't earn its place on this sample; leave the formula as-is.

_Deterministic engine; costs 0.05R; the factor only changes the confidence SCORE, not the trade outcomes. Small sample — read directionally._