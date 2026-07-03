# Task 6 — Drawdown & ruin Monte Carlo

Bootstrapped from **170 backtested trades** — win rate **34.1%**, expectancy **+0.177R/trade**, avg **3.0 trades/day**. 20,000 paths x 250 trades each. Ruin = 50% peak-to-trough drawdown. Daily breaker = 3% day loss.

## Results by risk-per-trade

| risk/trade | median maxDD | 95th-pct maxDD | P(DD>=20%) | P(DD>=30%) | P(ruin 50%) | P(daily-breaker) |
|---|---|---|---|---|---|---|
| 0.25% | 4% | 8% | 0% | 0% | 0.0% | 0% |
| 0.50% | 9% | 16% | 1% | 0% | 0.0% | 0% |
| 1.00% | 17% | 30% | 31% | 5% | 0.0% | 100% |
| 1.50% | 25% | 42% | 76% | 28% | 1.3% | 100% |
| 2.00% | 32% | 52% | 95% | 58% | 6.7% | 100% |

## Flags

- **1.50%/trade looks statistically unsafe**: P(ruin) 1.3%, 95th-pct drawdown 42%.
- **2.00%/trade looks statistically unsafe**: P(ruin) 6.7%, 95th-pct drawdown 52%.

## Notes / assumptions
- Compounding: each trade risks the given fraction of CURRENT equity; P&L = risk x equity x R.
- Bootstrap assumes trades are i.i.d. draws from the historical R distribution — it ignores serial correlation (streaks/regime clustering), so REAL drawdowns are usually somewhat worse than shown.
- Daily-breaker probability groups trades into days of ~3 and uses an arithmetic day loss; it approximates the realized+floating breaker (Task 4), which trips intraday.
- Horizon = 250 trades; ruin/breaker probabilities scale with horizon.
- Position-sizing defaults were NOT changed by this script — it only reports.