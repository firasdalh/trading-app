# Confidence-formula ablation — which terms are redundant?

203 actionable setups. For each formula variant, the >= 70% subset (what the Hybrid would actually trade) is scored. If dropping a term barely changes that subset, the term was double-counting.

Full formula, >= 70%: **n=107, win 33.6%, exp +0.174R, total +18.6R**

| variant (term removed) | trades >=70% | win% | expectancy R | total R | vs full |
|---|---|---|---|---|---|
| full | 107 | 33.6% | +0.174 | +18.6 | +0.000R |
| no_macd | 91 | 37.4% | +0.301 | +27.4 | +0.127R |
| no_rsi | 116 | 32.8% | +0.165 | +19.2 | -0.009R |
| no_ema200 | 91 | 36.3% | +0.268 | +24.4 | +0.094R |
| no_div | 110 | 37.3% | +0.288 | +31.6 | +0.113R |
| no_ALL_four | 90 | 33.3% | +0.150 | +13.5 | -0.024R |

## Verdict (per term)

- **drop macd**: expectancy +0.127R vs full, trades -16 -> **redundant — safe to remove.**
- **drop rsi**: expectancy -0.009R vs full, trades +9 -> **redundant — safe to remove.**
- **drop ema200**: expectancy +0.094R vs full, trades -16 -> **redundant — safe to remove.**
- **drop div**: expectancy +0.113R vs full, trades +3 -> **redundant — safe to remove.**

- **Drop all four at once:** >=70% n=90, win 33.3%, exp +0.150R (-0.024R vs full). Cutting all four together costs expectancy — remove only the individually-safe ones.

_Deterministic engine; costs 0.05R; the ablation only changes the confidence SCORE (which setups clear 70%), not the trade outcomes. Small sample — read directionally._