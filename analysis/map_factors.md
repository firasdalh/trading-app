# Map-read soft factors — Wall proximity + Volume trend (do they help?)

The user asked the engine to *decide* using the 🗺️ Read scorecard, not just display it. Four of the six
scorecard items (Structure, Trend, Momentum, RSI) were already decision inputs. The two genuinely new
ones — **Wall proximity** (Resistance/Support) and **Volume trend** — were added as *soft confidence
adjustments* and validated on walk-forward BEFORE being made default.

Both factors only move the confidence SCORE (computed after `take_market`), so the trade universe is
identical across variants — the 70% Hybrid gate then selects a different subset. We measure the gated
(>= 70%) set, in- and out-of-sample (last 30% of time held out).

## Design (why these differ from the failed channel factor)

- **Wall proximity** penalises entering with little headroom to the nearest level (`< 0.75 ATR` ahead)
  **only when the trend is NOT strong** — a strong trend legitimately breaks through levels (that was
  the lesson from the removed channel factor, `channel_test.md`). A key level just *cleared* behind the
  entry on rising volume gets a small breakout bonus.
- **Volume trend** rewards volume expanding into the move, penalises fading volume.

## Result (enabled watchlist, 15 symbols, 245 actionable trend setups, >= 70% gate)

| variant | OOS n | OOS win% | **OOS exp** | IS exp | full-sample exp |
|---|---|---|---|---|---|
| base (both off)   | 54 | 33.3% | **+0.144R** | +0.237R | +0.196R |
| **+wall only**    | 53 | 34.0% | **+0.166R** | +0.272R | +0.227R |
| +voltrend only    | 51 | 33.3% | +0.121R | +0.164R | +0.145R |
| +both             | 52 | 32.7% | +0.114R | +0.186R | +0.155R |

## Verdict

- **Wall proximity → KEEP (now default).** Improves expectancy both in-sample (+0.272 vs +0.237R) and
  **out-of-sample (+0.166 vs +0.144R)**, consistently, without cutting trade count. It re-ranks marginal
  chases below the 70% gate and lets clean setups through.
- **Volume trend → DROP.** Worse in *and* out of sample. Raison: this is a trend engine, and real trends
  routinely grind higher on *fading* volume while volume *spikes* often mark climaxes/reversals — so
  volume slope is a poor conviction signal here. The `vol_trend` indicator is still computed (it powers
  the wall breakout bonus and the 🗺️ Read scorecard), just not scored into confidence.

_Deterministic engine, trending regime only; factors change the confidence score, not trade outcomes.
Small OOS sample (~53 trades) — read directionally, but the wall improvement holds in both windows and
does not degrade OOS, which is the bar it had to clear._
