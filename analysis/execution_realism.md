# Task 8 — Execution realism (slippage/spread vs perfect fill)

Re-scored **170 deterministic trades** with the context-dependent cost model (round numbers / prior swing H-L / thin sessions / stop slippage). Average modeled cost **0.050R/trade**. 12% of entries sat on a round number; 44% filled in a thin session; 65% exited on a stop (extra slippage).

## Perfect fill vs realistic

| fills | trades | win% | expectancy R | total R | profit factor | maxDD R |
|---|---|---|---|---|---|---|
| perfect (gross) | 170 | 34% | +0.222 | +37.7 | 1.34 | 16.2 |
| realistic (slippage) | 170 | 34% | +0.171 | +29.1 | 1.25 | 17.4 |

## Impact

- Expectancy **+0.222R -> +0.171R** (a **0.050R**/trade haircut, ~23% of the gross edge).
- Profit factor 1.34 -> 1.25.
- The realistic R distribution is dumped to `entries_slippage.json` and is the input Task 6 (drawdown) and Task 14 (sizing) should use instead of the gross numbers.

## Per-symbol expectancy (gross -> realistic)

| symbol | gross R | realistic R |
|---|---|---|
| USTECm | +0.032 | -0.022 |
| EURUSDM | +0.080 | +0.031 |
| XAUUSDm | +0.106 | +0.056 |
| CADJPYm | +0.132 | +0.091 |
| USOILm | +0.178 | +0.126 |
| HK50m | +0.197 | +0.139 |
| USDCHFm | +0.381 | +0.338 |
| AUDNZDm | +0.418 | +0.374 |
| JP225m | +0.525 | +0.471 |

## Read

- The edge **survives realistic costs** with a modest haircut — but always size from the net (realistic) R, not the gross backtest.
- Isolated to the backtest: the live funnel/Risk Manager are untouched. This only gives Tasks 6 & 14 an honest R input.

_Model caveats: costs are ATR-fraction proxies (no per-symbol spread table); 'prior H/L' uses the entry-TF swing levels; sessions are a coarse UTC-hour heuristic. Directionally right, not tick-accurate._