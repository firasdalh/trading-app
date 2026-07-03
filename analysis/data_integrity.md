# Task 10 — Data-feed integrity checks

## Why
Corrupted price data silently poisons the two things the funnel trusts most: **ATR-based stop
sizing** (one bad-tick bar inflates ATR → a wildly wide stop) and **swing/structure detection** (a
spike prints a fake swing high/low → a fake breakout). These checks catch clearly-broken input
*before* it reaches the funnel.

## What is checked (`backend/app/data/integrity.py`)
| Check | Rule | Protects |
|---|---|---|
| `ohlc_invalid` | H<L, or open/close outside [L,H] | structure / any level math |
| `nonpositive` | any OHLC ≤ 0 | log/return math, ATR |
| `anomalous_range` | bar range > **6× ATR(14)** | ATR sizing, fake swings |
| `stale` | **≥ 4** identical consecutive closes | frozen feed → dead indicators |
| `gap` | time gap > **2.5×** the series' median cadence, excluding normal weekend/holiday-sized gaps | missing bars → wrong lookbacks |

Thresholds are deliberately loose — the goal is to flag the *clearly broken*, not normal volatility.
The function is pure and self-contained (no agent/strategy imports), and returns a typed list of
issues.

## How it's wired (APPROVED — hard-reject now ON)
Every fresh broker fetch through `get_ohlcv_cached` runs `repair_and_log`:
- **REPAIRED (default, `DATA_INTEGRITY_REJECT=true`):** the clearest corruption is neutralized before
  the funnel ever sees it —
  - a **spike** bar (range > 6× ATR) has its **wicks clamped** back toward the real body (a robust
    median-range target, so ATR-sizing/structure can't be blown up by one bad tick); the open/close
    are preserved.
  - a **non-positive / inconsistent OHLC** bar is replaced with a **flat bar at the prior close**.
  No bars are added or removed, so time alignment is preserved.
- **LOGGED, not touched:** soft issues (feed **gaps**, **stale** runs) are logged but left intact —
  dropping/inventing bars there is riskier than the problem.
- **Backtests are unaffected** — the backtest path deliberately bypasses this cache, so it still sees
  raw data.
- **Reversible:** set `DATA_INTEGRITY_REJECT=false` to fall back to log-only.

## Verification
`tests/test_data_integrity.py` (12 tests): detection of every issue kind; weekend gap NOT flagged;
plus repair tests — a spike is clamped (body preserved, issue cleared), an invalid bar is flattened
to the prior close, soft issues are left untouched, and a clean series is unchanged. All pass.

## Note
Repair is deliberately conservative (only the unambiguous corruption). Watch the
`data-feed REPAIRED before funnel` / `(soft, not repaired)` logs during paper trading — if real
Exness weekend ticks or rollover gaps show up, we can extend handling to those specific kinds.
