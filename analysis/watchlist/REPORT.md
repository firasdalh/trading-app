# Which symbols should the Hybrid trade? (2026-10-29)

Trades are the live engine's, from the replays in `analysis/macd/`:
- **Engine settings:** trend-only ON, live filters, AI off, Hybrid bar (confidence > 0.68).
- **Simulation:** path-dependent per symbol, R net of spread.
- **Spread gate:** applied this time — live refuses a trade whose spread is above 15% of its stop distance. Earlier reports in `analysis/` left it out.
- **Universe:** 33 in-sample symbols (2025-04-30 → 2026-10-28) plus the 20-symbol holdout of `analysis/macd/PREREG.md`.

## 1. The spread gate changes the picture

| | without the gate (earlier reports) | with it (what live does) |
|---|---|---|
| in-sample 33 | 861 trades, +0.050R, +43.2R, max DD 46.0R | **740 trades, +0.107R, +79.3R, max DD 33.9R** |
| watchlist 19 | 521 trades, +0.019R, +9.7R, max DD 41.8R | **435 trades, +0.118R, +51.5R, max DD 29.7R** |
| holdout 20 | 548 trades, −0.265R, −145.0R | **319 trades, −0.060R, −19.0R** |

Most of the losses on unseen symbols came from trades the gate would refuse. **Keep the spread gate ON.** It is one of the most valuable filters the desk has.

## 2. Picking symbols by their recent results: not reliable
**Method:** walk forward month by month. At the start of each month, keep only the symbols whose trades over the previous L months made money (a symbol with no trades is kept), trade them that month, and compare with trading everything. Each month's choice uses only past data.

**Rule fixed before the run:** selection must beat "trade everything" in total R for every L (3, 6 and 9 months) AND in both halves of the evaluation window.

| universe, evaluation months | trade everything | last 3 months | last 6 months | last 9 months |
|---|---|---|---|---|
| all 53, 2026-04 → 10 | −11.0R | **−25.9R** | +23.0R | +23.9R |
| in-sample 33, 2026-04 → 10 | +12.7R | **−17.8R** | +39.5R | +25.5R |
| watchlist 19, 2026-05 → 10 | +26.5R | **+14.9R** | +39.1R | +31.6R |

The 3-month version is worse than doing nothing everywhere, and the window is only 6–7 months long. **Fails the rule: do not prune the watchlist by recent per-symbol results.** One symbol's 15–40 trades are mostly noise.

## 3. Groups of instruments

| group | in-sample 1st half | in-sample 2nd half | holdout |
|---|---|---|---|
| gold, silver, platinum (palladium: every trade refused by the spread gate) | +0.64R (35) | +0.64R (39) | — |
| indices | +0.05R (132) | +0.13R (118) | — |
| crypto | +0.51R (29) | −0.10R (36) | — |
| FX majors + DXY | −0.08R (46) | +0.46R (49) | +0.14R (71) |
| base metals | +0.21R (9) | +0.40R (3) | +0.15R (23) |
| energy | −0.27R (27) | −0.18R (20) | — |
| **FX crosses** | +0.20R (90) | **−0.29R (107)** | **−0.14R (225)** |

Per trade, number of trades in brackets. The in-sample halves split at 2026-04-22.

- **Precious metals and indices** were positive in both halves.
- **FX crosses** are the weakest group.
  - In-sample they made −13.1R over 197 trades, but not evenly: negative before 2026-01-15 (−0.14R, 45 trades), +24R from mid-January to April 2026, then negative.
  - On the 14 unseen crosses: −32.4R over 225 trades, negative in both parts of the period.
- **The four crosses on the watchlist:**

  | symbol | result |
  |---|---|
  | AUDCADm | −2.7R |
  | CADJPYm | −3.8R |
  | AUDCHFm | −6.5R |
  | EURCADm | −16.7R |
  | **together** | **−29.7R** (95 trades, −0.31R a trade) |

## Recommendation
1. **Remove IN50m.** It has no prices since 2023 and Exness has it disabled, so it can never trade.
2. **Keep the spread gate ON.**
3. **Do not prune by recent results** (section 2).
4. **Taking the FX crosses off the Hybrid is a judgment call, not a proven rule.** They were the weakest group in every view but one: the 2026-01 → 04 stretch. The evidence is moderate; reversing it means re-adding four symbols.

## Reproduce (from `backend/`)
```
python ../analysis/watchlist/select_test.py <dir>/macd2 <dir>/bars.pkl <dir>/holdout <dir>/holdout.pkl
```
