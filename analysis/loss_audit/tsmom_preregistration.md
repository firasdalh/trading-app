# Pre-registration: daily time-series momentum (written 2026-10-20, BEFORE any result was computed)

## Why this, and why now
Every idea tested in this repo since September was either derived from or tuned on our own 1h data, and
all of them failed once measured honestly (see `strategy_review_2026-09-23.md`, the Oct 14 research,
`ob_path_preregistration.md`, `fade_preregistration.md`). The engine's only demonstrated profit was trend
on metals during the 2025-26 rally.

Time-series momentum is the opposite kind of candidate: its evidence comes from OUTSIDE this repo —
Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum"; Hurst, Ooi & Pedersen (2017), "A Century of
Evidence on Trend-Following Investing" — across commodities, equity indices, currencies and bonds over
decades. The question is not "can we find a trend rule that worked here", it is "does the published rule,
unchanged, survive these instruments and this broker's costs".

## The rule — literature parameters, fixed now, NO tuning
- **Signal:** sign of the instrument's trailing 252-trading-day (12-month) return, measured at month end.
- **Sizing:** each position scaled to an ex-ante annualised volatility of 40% (MOP 2012), using an
  exponentially weighted variance of daily returns (centre of mass 60 days). Equal weight across the
  instruments that have enough history on that date. Portfolio returns are reported per unit of that
  scheme and also scaled to 10% annual volatility for readability (scaling does not change Sharpe).
- **Rebalance:** monthly, at the last trading day's close. Hold one month.
- **Universe:** every instrument we have daily history for — the original 33 and the 16 holdout pairs,
  reported together AND separately. No instrument dropped after seeing results.
- **Primary metric:** annualised Sharpe ratio of the portfolio's daily returns, NET of costs.
- **Secondary (robustness only, never used to rescue the primary):** the Hurst-Ooi-Pedersen blend of
  1, 3 and 12-month signals, equally weighted.

## Costs
- **Spread:** every rebalance pays the full spread on the traded change in position (per-bar D1 spread
  where available, else the broker spec spread). Conservative: a full spread, not half.
- **Financing (swap):** a monthly-held CFD book pays or earns overnight swap every night. The demo account
  charges none, a real account does. Modelled from the broker's current `swap_long` / `swap_short`, and
  reported both with and without it so the demo/real gap is visible. Current swap rates are applied to the
  whole history — an approximation, stated rather than hidden.

## Decision rule
- **SUPPORTED** if the primary net Sharpe (WITH swap) is > 0 with one-sided p < 0.05 (t-stat of mean daily
  return > 1.645) over the full sample, AND net Sharpe is positive in both halves of the sample.
- **REJECTED** otherwise.
- **Power, stated in advance:** realistic diversified TSMOM Sharpe is ~0.3-0.8 net. With T years of data
  the standard error of Sharpe is ~1/sqrt(T), so fewer than ~8 years of common history cannot distinguish
  a Sharpe of 0.5 from zero. If the history is shorter than that, a non-significant result is reported as
  UNDERPOWERED, not as evidence against the rule.

## Implementability (reported regardless of the verdict)
With ~$1,250 equity: at 10% portfolio volatility, is each instrument's target position at least the
broker's minimum lot? If not, how large must the account be for the rule to be expressed as designed?
A strategy the account cannot actually hold is not an option, however good the backtest.

## Amendment A1 (2026-10-20, after the data fetch, BEFORE any result was computed)
The fetch returned 2001-onward history for GBPJPYm only; every other instrument starts 2013-2014 or later
(indices 2019, crypto 2018, XNG 2022). Read literally, the "equal weight across instruments with enough
history" rule would make 2002-2014 a ONE-instrument portfolio, and the "both halves" test would compare
GBPJPY alone against the diversified book. That is not the test this document describes. Therefore:
- The portfolio sample starts on the first day with **at least 10 active instruments**. All instruments
  keep contributing from then on; nothing is dropped.
- Halves are split on that sample.
- GBPJPY's pre-sample history is reported separately as a single-instrument footnote, never used for the
  verdict.
- Also reported (information only, not verdict inputs): annual cost drag from spread and from swap.
  MT5's D1 `spread` field is a per-bar summary and can understate the spread actually paid, so the spread
  line is also shown at 3x.
The signal, sizing, rebalance, cost model and decision rule are unchanged.

## RESULT (2026-10-20) — REJECTED

Sample (A1): 2015-02-02 .. 2026-10-16, 11.7 years, 33-49 instruments live (median 45). Data: Exness MT5 D1,
49 symbols. Run: `python ../analysis/loss_audit/tsmom_backtest.py <daily.pkl> 1250 <holdout list>`.

| primary rule | Sharpe | t |
|---|---|---|
| gross (no costs) | +0.03 | +0.10 |
| net of spread | -0.04 | -0.14 |
| **net of spread + swap** | **-0.54** | **-1.86** |
| halves (net of all) | -0.93 / -0.12 | |
| secondary 1/3/12 blend (net of all) | -0.85 | -2.92 |

Fails every leg of the decision rule. The verdict does NOT depend on the swap model: net of spread alone is
t -0.14 against the required +1.645, and even a swap-free account (-0.04) or a generous carry bound (-0.18)
stays rejected. 11.7 years is above the 8-year power threshold, so this is a rejection, not "underpowered".

**Corrections made AFTER the first result, each reported, none changed the verdict:**
1. Portfolio denominator counted "instruments with a bar today", so holidays became 1-instrument portfolios.
   Fixed to "instruments holding a position" (gross +0.07 -> +0.03).
2. Subset lines ignored the A1 start. Fixed.
3. (Found by the verifier) swap used today's POINTS / the HISTORICAL price, which inflated gold/silver's rate
   ~2.5x. Fixed to today's RATE as this document specifies (net -0.72 -> -0.54, swap drag -0.68 -> -0.50).

**Verification:** two independent skeptics (code/accounting lens, cost/implementability lens) each rebuilt
the test in pandas from this document and reproduced every number. Lookback sweep 231-273 bars: gross
Sharpe -0.08..+0.33, best t ~1.1 — no nearby parameter is significant either. No bad prints in the data;
the >25% moves are real events.

**Why (information only — NOT a rescue):** 28 of 49 instruments are FX crosses, and FX trend was negative
(gross -0.15). Metals +0.29, indices +0.32 (6.2y), energy +0.24, crypto +0.43 gross — none significant
alone, and swap turns all but crypto negative. "Non-FX trend" was seen only AFTER looking; it would need
its own pre-registration and independent evidence.

**Swap caveat:** Exness floors the earning side of every swap at 0 (no positive swap on any of the 49), so
vol-targeted FX at 5-8x notional pays 2-5%/yr per instrument with no carry income. Real-account terms may
differ; the verdict holds under every bound tested.

**Implementability at $1,250: impossible.** 0 of 49 instruments can be held within 1.5x of target (best
ratio 4.49x, NZDCHF). The full book needs ~$174k (exact sizing ~$262k; ex-metals ~$109k).
