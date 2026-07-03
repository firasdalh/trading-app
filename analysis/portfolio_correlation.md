# Task 9 — Portfolio correlation (model vs empirical)

Daily-return correlation over ~399 days for 9 watchlist symbols. The Risk Manager's factor model (app/risk/correlation.py) is validated against it.

## Empirical correlation matrix (daily log returns)

| | EURUSD | XAUUSD | JP225 | USDCHF | USOIL | AUDNZD | CADJPY | USTEC | HK50 |
|---|---|---|---|---|---|---|---|---|---|
| **EURUSD** | +1.00 | +0.06 | +0.12 | -0.88 | -0.11 | -0.17 | -0.45 | +0.21 | +0.14 |
| **XAUUSD** | +0.06 | +1.00 | +0.07 | -0.08 | -0.11 | +0.03 | +0.00 | +0.08 | +0.03 |
| **JP225** | +0.12 | +0.07 | +1.00 | -0.07 | -0.16 | +0.12 | +0.05 | +0.79 | -0.03 |
| **USDCHF** | -0.88 | -0.08 | -0.07 | +1.00 | +0.18 | +0.22 | +0.47 | -0.19 | -0.15 |
| **USOIL** | -0.11 | -0.11 | -0.16 | +0.18 | +1.00 | -0.03 | +0.05 | -0.16 | -0.02 |
| **AUDNZD** | -0.17 | +0.03 | +0.12 | +0.22 | -0.03 | +1.00 | +0.24 | +0.06 | -0.00 |
| **CADJPY** | -0.45 | +0.00 | +0.05 | +0.47 | +0.05 | +0.24 | +1.00 | -0.06 | -0.02 |
| **USTEC** | +0.21 | +0.08 | +0.79 | -0.19 | -0.16 | +0.06 | -0.06 | +1.00 | +0.03 |
| **HK50** | +0.14 | +0.03 | -0.03 | -0.15 | -0.02 | -0.00 | -0.02 | +0.03 | +1.00 |

## Does the factor model capture real correlation?

- Mean |corr| for pairs the model calls **related**: **0.31** (6 pairs).
- Mean |corr| for pairs the model calls **independent**: **0.13** (30 pairs).
- Separation: **+0.18** — the model's 'related' pairs are meaningfully more correlated than its 'independent' ones, so the factor model is capturing the real structure.

**Top model-'independent' pairs by empirical |corr| (potential blind spots):**
- USDCHFm ~ CADJPYm: |corr| 0.47
- EURUSDM ~ CADJPYm: |corr| 0.45
- AUDNZDm ~ CADJPYm: |corr| 0.24
- USDCHFm ~ AUDNZDm: |corr| 0.22
- EURUSDM ~ USTECm: |corr| 0.21

## What exists vs the Task-9 gap

- **Exists (live):** `correlated_concentration` blocks a 3rd position that nets the same way on any factor (USD, JPY, equity bloc, metals, energy, crypto). Test `test_correlation_blocks_third_usd_bet` is exactly the Task-9 case. Effective per-factor cap ~= 2 x per-trade-risk (~6% at the 3% cap).
- **Gap 1 (count vs risk-weighted):** the block counts POSITIONS, not summed risk — two 0.5% positions and two 3% positions on USD are treated the same. A risk-amount-weighted per-factor cap would be tighter/fairer.
- **Gap 2 (block vs resize):** it hard-blocks the 3rd bet rather than DOWNWEIGHTING it to fit a remaining factor budget (the total-exposure gate already resizes; the correlation gate does not).
- **Gap 3 (cross-bloc):** the model treats blocs (equity / metals / energy) as separate; any high empirical corr between them above is unmodeled.

**Recommendation:** add a risk-weighted per-factor exposure cap (sum the % equity at risk on each factor across open positions + the new trade; block OR resize to a `max_correlated_exposure` budget). This is a LIVE-PATH Risk-Manager change — flagged for explicit approval, implemented additively (strictly more conservative) and behind a config value so it can't loosen any existing gate.