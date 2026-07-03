# AI reviewer — repeatability & significance (last 30 days)

Same **93 setups**, AI review run **5x**. Deterministic baseline: **38.7%** win (36/93).

## 1. Repeatability (run-to-run on the SAME setups)

| run | veto rate | confirmed win% | vetoed win% | LLM fails |
|---|---|---|---|---|
| 1 | 54% | 39.5% | 38.0% | 0 |
| 2 | 51% | 45.7% | 31.9% | 0 |
| 3 | 53% | 36.4% | 40.8% | 0 |
| 4 | 46% | 46.0% | 30.2% | 0 |
| 5 | 46% | 48.0% | 27.9% | 0 |

- **Veto rate:** mean 50%, range 46-54%, std 3.2 pts.
- **Confirmed win%:** mean 43.1%, range 36.4-48.0%, std 4.4 pts.
- **Unstable setups (confirmed in some runs, vetoed in others): 76/93 (82%)** — the AI flips its verdict on these. High = noise, not a stable filter.

## 2. Is the AI cutting losers, or just variance?

- Win rate of the trades the AI VETOED: mean **33.8%** across runs. Well below the confirmed win% -> it IS preferentially cutting losers.

## 3. Statistical significance (Wilson 95% CI)

- Deterministic (36/93): **29%-49%**
- AI-confirmed (~43% of ~47): **30%-57%**
- The CIs OVERLAP heavily -> the win-rate 'improvement' is NOT statistically established at this sample size. Treat as a first read.

## 4. Cheaper deterministic filter on the SAME setups

- Confidence >= 70%: **45.2%** win (n=62)
- ADX >= 28: **36.8%** win (n=57)
- AI mean confirmed: **43.1%** win (n~47)
- If a rule-based filter matches the AI's win% here, it's the better choice: deterministic, free, and zero repeatability problem.

## Verdict

- Repeatability is the deciding factor: **82% of setups flip** verdict between identical runs. gpt-5-mini is a reasoning model (no temperature/seed), so this drift is inherent — the current AI veto is **not a stable, reproducible filter**.
- Options: (a) switch the reviewer to a NON-reasoning model at temperature 0 for determinism, (b) ensemble the review (vote over k calls) to average out the noise, or (c) prefer the deterministic confidence/ADX filter above if it matches the win%.

_One 30-day sample, one machine-run; costs = flat 0.05R; favorable (trending) month so absolute win rates run high vs the long-run 34%._