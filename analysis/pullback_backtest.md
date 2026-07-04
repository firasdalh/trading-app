# Enhancement (A) — pullback/armed re-entry vs chasing at market

203 actionable setups; **163** carried a conditional (better-priced pullback/break entry). Only those are the head-to-head; at-value setups are taken at market either way.

## Head-to-head on the setups that carry a conditional

| entry method | trades | win% | expectancy R | total R |
|---|---|---|---|---|
| MARKET (chase) | 163 | 31.3% | +0.110 | +18.0 |
| PULLBACK (fills only) | 53 | 28.3% | +0.065 | +3.4 |

- **Fill rate:** 53/163 triggered (33%); 25 expired, 85 invalidated (never taken).
- **Cost of waiting (missed setups):** the 110 that never filled would have done **+0.074R** at market (30% win) — that's the winners you skip by waiting.

## Whole-system: chase-everything vs pullback-where-available

| strategy | trades | win% | expectancy R | total R |
|---|---|---|---|---|
| MARKET (all at market) | 203 | 32.5% | +0.125 | +25.4 |
| PULLBACK (cond when available) | 93 | 32.3% | +0.117 | +10.9 |

## In-sample vs out-of-sample (last 30% held out) — head-to-head

| window | MARKET win% / exp | PULLBACK win% / exp |
|---|---|---|
| in-sample | 26% / -0.103 | 17% / -0.345 |
| OUT-OF-SAMPLE | 41% / +0.486 | 50% / +0.862 |

## Verdict

- On the conditional-carrying setups, pullback expectancy **+0.065R** vs market **+0.110R**, win% **28%** vs **31%** -> **market (chase) is as good or better.**
- But pullback only fills 33% of the time; the rest expire. Net effect on the WHOLE system is the 'chase-everything vs pullback-where-available' table above — that's the number that matters for the account.
- Decide on the WHOLE-SYSTEM total R and the OOS row, not the per-trade edge alone: a higher per-fill win rate that skips too many winners can lower total return.

_Deterministic engine only; costs 0.05R flat; trigger window 24 bars; conservative intrabar fills. Small samples per split — read directionally._