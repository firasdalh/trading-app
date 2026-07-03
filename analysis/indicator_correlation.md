# Task 2 — Indicator correlation at entry

Sample: **169 entries** (deterministic funnel, trend_only, all watchlist symbols). Features are direction-aligned and ATR-normalized so a positive value always means 'confirms the trade'.

## Correlation matrix (Pearson r)

| | ema align | ema slope | adx | macd hist | rsi dir |
|---|---|---|---|---|---|
| **ema align** | +1.00 | +0.51 | +0.53 | -0.14 | +0.42 |
| **ema slope** | +0.51 | +1.00 | +0.39 | +0.71 | +0.77 |
| **adx** | +0.53 | +0.39 | +1.00 | -0.03 | +0.24 |
| **macd hist** | -0.14 | +0.71 | -0.03 | +1.00 | +0.67 |
| **rsi dir** | +0.42 | +0.77 | +0.24 | +0.67 | +1.00 |

## Pairwise, strongest first

- **EMA20 slope, 5-bar (/ATR)** ~ **RSI vs 50 (directional)**: r = +0.77 (high)
- **EMA20 slope, 5-bar (/ATR)** ~ **MACD histogram (/ATR)**: r = +0.71 (high)
- **MACD histogram (/ATR)** ~ **RSI vs 50 (directional)**: r = +0.67 (high)
- **EMA20-50 alignment (/ATR)** ~ **ADX (trend strength)**: r = +0.53 (moderate)
- **EMA20-50 alignment (/ATR)** ~ **EMA20 slope, 5-bar (/ATR)**: r = +0.51 (moderate)
- **EMA20-50 alignment (/ATR)** ~ **RSI vs 50 (directional)**: r = +0.42 (moderate)
- **EMA20 slope, 5-bar (/ATR)** ~ **ADX (trend strength)**: r = +0.39 (moderate)
- **ADX (trend strength)** ~ **RSI vs 50 (directional)**: r = +0.24 (low)
- **EMA20-50 alignment (/ATR)** ~ **MACD histogram (/ATR)**: r = -0.14 (low)
- **ADX (trend strength)** ~ **MACD histogram (/ATR)**: r = -0.03 (low)

## Read

- The momentum-family signals (EMA slope, MACD histogram, RSI) are **strongly correlated** — they are largely **restating the same underlying momentum**, not independent votes. Stacking them mostly compounds one signal; it does not add much orthogonal confirmation.
- **ADX** is somewhat related of the directional momentum signals (as expected — it measures trend *strength*, not *direction*), so it is the gate adding the most orthogonal information.
- Mean |r| across all pairs = **0.44**. Overall the gates are moderately-to-highly collinear — the funnel has fewer truly independent confirmations than it has indicators.

_Caveat: correlations are at the point of entry only (a filtered, conditioned sample — the funnel already required trend + momentum alignment to fire), so these are conditional correlations, not unconditional indicator relationships._