# Loss audit — why the desk loses money (2026-09-16)

Scope: the live journal since the 2026-07-08 reset (MT5 demo, Exness), every closed deal matched to
its MT5 close reason, plus a year of cached MT5 candles replayed through the deterministic engine.

## 1. What the journal says

| | |
|---|---|
| Closed trades (broker truth) | 374 |
| Win rate | 45.2% |
| Average win / loss | **+0.98R / −0.97R** |
| Profit factor | 0.82 |
| Total | −23.8R |

By source since the reset: deterministic **+$584**, armed +$39, armed_manual +$49, rsi_over +$53 —
against auto_trade **−$917**, armed_hybrid **−$421**, hybrid **−$393**, manual −$186.

Held to their ORIGINAL stop/target (replayed on 15m candles), the 369 matchable trades reach the target
**115 times and the stop 251 times** — a 31% hit rate at ~2.2R targets, i.e. breakeven before costs and
negative after. The entries have no edge; everything downstream only moves the result around zero.

### Exits are not the leak

| exit | trades | realised vs held-to-plan |
|---|---|---|
| advisor moved the stop (breakeven / trail) | 29 | **−18.0R** — choked winners |
| advisor closed / partial | 43 | +3.7R |
| manual Close from the UI | 77 | **+32.3R** |
| untouched | 220 | −12.2R (spread, weekend gap) |

Keep advisor auto-execute OFF. The discretionary manual exits were the best-performing decision in the book.

### Execution
A full stop costs −1.03R on average: sizing is exact (planned $ risk == real $ risk), entry slippage is
~3% of R. Not a leak — except the tail: **UKOILm 2026-07-24 was opened Friday 20:45 UTC and gapped
through its stop on the reopen: −$434, −8.9R in one trade.** Weekend-flatten exists but is OFF.

### Armed break-and-retest: a chase disguised as a retest
Fills landed far from the "retest" price: USTECm armed at 29,592 → filled 29,751; ETHUSDm 2,018.9 →
2,084 (R:R 0.16); BTCUSDm 80,031 → 80,678. Cause: the retest "touch" look-back included the breakout
bars themselves, so it counted as retested the moment the break confirmed. Separately, every "close"
check read MT5's still-forming bar, whose close is just the latest tick. Both fixed in `conditional.py`.

## 2. Root cause: the backtest could see the future

`simulator.py` sliced higher timeframes by bar OPEN time, so at the close of a 1h bar the engine was
handed the FINISHED 4h bar (up to 3h ahead) and TODAY's FINISHED daily candle (up to 23h ahead) —
exactly the inputs of the higher-TF trend gate, the daily confirmation and the big-TF levels. Every
"validated" result in this project (trend-only, MTF filter load-bearing, the walk-forward watchlist,
armed +0.34R, the `analysis/*.md` reports) was measured with that leak.

Fixed with `_context_window`: completed bars plus the forming bar rebuilt from entry-TF bars up to the
decision — exactly what live holds. Same data, same engine, same Hybrid confidence gate (33 symbols,
~6,500 1h bars, net of each symbol's real spread):

| config | harness | trades | expectancy | PF | out-of-sample | 14-pair watchlist |
|---|---|---|---|---|---|---|
| live settings | old (look-ahead) | 2071 | **+0.177R** | 1.28 | +0.154R | +0.146R |
| live settings | faithful | 1990 | **−0.008R** | 0.99 | −0.059R | −0.043R |
| trend-only + all filters | old (look-ahead) | 1348 | **+0.268R** | 1.41 | +0.158R | +0.119R |
| trend-only + all filters | faithful | 1241 | **+0.006R** | 1.01 | −0.108R | −0.089R |

~0.2R per trade of "edge" was look-ahead. Without it the engine is breakeven gross and negative net.

## 3. Other measured findings (faithful harness)

**Spread as a share of the stop decides the sign** (Hybrid-gated live config):

| spread / stop | trades | net |
|---|---|---|
| < 3% | 856 | **+0.052R** |
| 3–6% | 631 | −0.011R |
| 6–10% | 329 | −0.074R |
| 10–25% | 153 | −0.109R |
| ≥ 25% | 21 | −0.626R |

The live spread gate allows 25%.

**Armed path — every form loses** (live filters, 4,000 bars):

| variant | fills | net | OOS |
|---|---|---|---|
| break stop | 720 | −0.140R | −0.156R |
| retest, as it ran live | 637 | −0.172R | −0.298R |
| retest, as designed (now in code) | 508 | −0.250R | −0.421R |

Waiting for a retest selects the breaks that are failing. The fix makes the code do what it says; it does
not make arming profitable. Arming should be OFF.

**`failed_break` range fade:** −251R over 1,102 trades ungated. Mostly harmless live only because its
fixed 0.60 confidence sits under the Hybrid's 0.67 bar.

**Deciding on a half-formed candle** (15/30/45 min into the hour) vs at the close: −0.012 / −0.042 /
−0.116 / −0.097R. No consistent penalty — not a leak, left unchanged.

**Confidence does not rank outcomes:** trend trades at 0.90 conf −0.13R, at 0.65 +0.27R.

**Only consistently positive group:** gold/silver. XAUUSDm +0.33R (live) / +0.65R (trend-only), positive in
both halves of the year in both configs; XAGUSDm and XAUAUDm positive overall. Caveat: best of 33 in a
year gold trended hard — a hypothesis to forward-test, not a proven edge.
Consistently negative in every run: XNGUSDm, UK100m, AUS200m, UKOILm, GBPUSDm.

## 4. Inside the deterministic engine: it joins trends too late (fixed)

Same engine behind "deterministic" (+$584, you approve) and "hybrid" (−$393, auto-pilot). Held to plan,
your approved entries made +0.23R/trade vs Hybrid's +0.06R; your exits added more on top.

Instrumented faithful replay (`bt_diag.py`, every indicator at entry + MFE/MAE + stop×target grid):

- **Trend age decides it.** Bars since the SuperTrend flip (with the trade): ≤5 bars +0.07R, >40 bars
  −0.10R (OOS −0.23R), falling steadily with age. Cut chosen on the first 70% only: every limit from 15
  to 30 bars kept the unseen 30% positive (a plateau). Live journal agrees: engine trades into young legs
  +8.5R / +$106 (n=87), into late legs −2.4R / **−$455** (n=97).
- **Confidence rewarded lateness.** Aligned swing structure (+0.1), no fresh CHoCH (−0.1 when present) and
  an active session (+0.05 / thin −0.10) all pointed the wrong way in both halves — each rewards a trend
  that is already obvious. So confidence ran backwards (0.6 → +0.30R … 0.9 → −0.05R) and the Hybrid's
  "open the highest-confidence setup" picked the most mature trends.
- Not the problem: stop distance (wider stops ≈ +0.03R, noise), aiming targets through a level, DI
  direction, ADX rising/falling (flips sign between halves).

Fix: `trend_age` filter (skip a trend leg older than 24 bars when SuperTrend agrees; no flip in the window
= older than the window), and the three inverted confidence factors removed. Re-run path-dependently on
the same data, live filter set, through the Hybrid gate:

| | before | after |
|---|---|---|
| expectancy | −0.008R | **+0.093R** |
| profit factor | 0.99 | **1.15** |
| total / max DD | −16.6R / 111R | **+132R / 56R** |
| out-of-sample | −0.060R | **+0.126R** |
| 4 time folds | +0.00 +0.08 −0.03 −0.11 | **+0.11 +0.09 +0.10 +0.06** |
| 14-pair watchlist | −0.043R | +0.060R |
| since 2026-07-08 | −0.166R | −0.024R |

26/33 symbols improved. Confidence below the 0.67 gate is now the weak side (0.60–0.67: −0.10R) and every
band above it is positive in both halves. A modest edge, not a proven one: the choices were made on this
same year, and the last two months are still slightly negative. Forward-test before trusting size.

## 5. "Smarter" ideas — tested, most rejected (2026-09-17)

`bt_diag2.py` records 4h + daily indicators, today's partial daily candle and each trade's full 96-bar
path, so entry filters are split IS/OOS + 4 folds and exit rules are replayed bar by bar (stop first).
Rule: ship only what moves the SAME way in both halves and most folds, on a plateau of neighbouring values.

| idea | verdict | evidence (Hybrid-gated trend trades, net) |
|---|---|---|
| daily range already used / chasing the day | rejected | no exhaustion effect; buckets flip between folds |
| daily RSI, daily ADX, distance from 4h/daily EMA20 | rejected | non-monotonic, sign flips IS→OOS |
| 4h ADX ≥ 40 = exhaustion | rejected | −0.11R in all folds at 40, but 35 mixed and 45/50 noise: a spike |
| prior day/week breakout | rejected | long-only effect = the bull-year bias |
| Bollinger width | rejected | proxies asset class |
| partial profits (50% at 1R, 33% at 1.5R), breakeven | rejected | worse in every variant |
| ATR trailing stops | rejected | better in-sample, not out-of-sample |
| **target ≥ 3R** | **shipped** | 2.5→3→3.5→4R rise steadily, all folds |
| **fresh 4h leg (≤6 bars) ranks first in Hybrid** | **shipped (ranking only)** | ≤4/6/8 bars: +0.21/+0.29/+0.28R, all folds; below the 0.67 gate it FAILED OOS, so it never admits a trade |
| time-stop: exit at 12h if < +0.25R | shipped as opt-in, **leave OFF** | alone +0.12R vs +0.10R in all folds, but on top of the 3R target it adds nothing reliable (OOS 0.141 vs 0.155) |

Path-dependent re-run with the 3R target in the engine (Hybrid gate):

| | late-trend fix | + 3R target |
|---|---|---|
| expectancy | +0.093R | **+0.147R** |
| profit factor | 1.15 | **1.20** |
| total / max DD | +132R / 56R | **+188R / 44R** |
| IS / OOS | +0.086 / +0.114 | **+0.153 / +0.132** |
| 4 folds | +0.11 +0.09 +0.14 +0.01 | **+0.17 +0.16 +0.21 +0.02** |
| win rate | 39% | 30% |
| since 2026-07-08 | −0.024R | −0.045R |

The win rate drops to ~30% by design — trend books pay through a few big runs. The last two months are
still slightly negative on both versions: a modest edge to forward-test, not a proven one.

## 6. What a second opinion was worth (2026-09-18)

A USTECm short (entry 29,438 / stop 29,552 / target 29,281) with an outside read arguing "down first is
slightly more likely". Measured on the symbol's own history — same state (1h uptrend, price within 0.4%
of a 20-bar high), 2,383 cases on USTECm and 21,000 across the index CFDs:

| | target (short) first | stop first |
|---|---|---|
| as it stood | 43% | 57% |
| with RSI ≥ 70 / stalling (the stated thesis) | 44-45% | 55% |
| after price broke its 29,410 trigger | **54-55%** | 45% |
| after price reclaimed 29,500 | 20% | **80%** |

43% ≈ the pure distance ratio (stop 118 points away vs target 153): the "overbought and stalling"
premise added nothing. The read's LEVELS were right and its trigger genuinely flips the odds — it was
describing a setup that hadn't happened yet and quoting it as the current probability.

Transferred to the engine, and what survived:
- **wait-for-a-trigger entries — REJECTED.** Requiring +0.1R confirmation within 3-6 bars before entering:
  per trade +0.156R vs +0.152R, but per SIGNAL +0.135R and total +167R vs +188R. It skips the runs that
  never come back. (A first pass looked spectacular at +0.43R — an accounting error: it required a CLOSE
  beyond the trigger while filling at the trigger. Priced at the real close it is +0.160R.) Right rule for
  a counter-trend fade, wrong one for a with-momentum engine.
- **abandon level before the stop — already covered/rejected** for trend trades (the stop grid showed
  tighter/earlier exits are worse). Keep it as a manual rule for discretionary fades.
- **distance-aware odds — ADOPTED** (`app/agents/odds.py`). Every actionable proposal now carries a measured
  base rate: how often this symbol reached a target this far away before a stop that far away, in this
  kind of tape (~1,500 sampled cases, 96-bar horizon, trend-side matched), plus the expectancy in R those
  odds imply. Shown in the setup checklist as "Measured odds". Read-out only — it gates nothing. This is
  the gap both our confidence score and the outside read shared: neither looked at barrier distance.

## Reproduce
From `backend/` with the MT5 terminal running and the app stopped (or one short read while it runs):

    python ../analysis/loss_audit/fetch_data.py <dir>/bars.pkl
    python ../analysis/loss_audit/bt.py --data <dir>/bars.pkl --symbols XAUUSDm,... --bars 6500 --mode faithful --out <dir>/r.json
    python ../analysis/loss_audit/analyze.py <dir> <dir>/r.json

`bt.py --mode lookahead` reproduces the old harness for comparison. `bt_armed.py` and `bt_intrabar.py`
cover the armed path and mid-candle decisions. The repo's own `python -m app.backtest` is now faithful.
