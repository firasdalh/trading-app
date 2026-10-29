# MACD, supply/demand and last week's high/low as entry conditions (2026-10-29)

Questions from the desk:
1. Does the MACD-histogram condition ("not clearly against the trade") help, or does it block good trades?
2. Would a MACD cross be a better source of confidence?
3. Should confidence rise when price bounces off a demand zone (buy) or rejects a supply zone (sell)?
4. Should confidence rise when price breaks, or bounces off, last week's high or low?

**Verdict: change nothing.**
- **The MACD-histogram condition stays.** It is one of the engine's useful filters, and it held on 20 symbols never used before.
- **None of the new ideas improves the engine:**
  - as confidence points, they barely change which trades pass the Hybrid bar, and the trades they add are not better;
  - two looked strong in-sample as hard conditions (MACD cross, supply/demand bounce), and both failed the pre-registered test on unseen symbols ([PREREG.md](PREREG.md)).

## Method
- **Data:** the 33-symbol, 18-month MT5 set of the AI-read test (2025-04-30 → 2026-10-28).
- **Engine:** the live one — trend-only ON, live filter set, AI off.
- **Harness:** the faithful one, R net of spread, with path-dependent backtests (one position per symbol, 3-bar cooldown). "Hybrid" means confidence > 0.68, with the gate applied inside the path.
- **Variants:** `replay.py` decides every bar three ways — live, without the MACD-histogram gate, and without the gate and the flat-momentum guard. At each decision it records the MACD state, the supply/demand zones (the app's own 1h order blocks) and last week's high/low.
- **Scoring:** `evaluate.py` scores about 50 variants. The acceptance rule was written into the script before results were read: beat live on total R over the 33 symbols, out-of-sample (last 30%) and in at least 3 of 4 time folds; not lose on the watchlist; and neighbouring settings must agree.
- **Holdout:** candidates that pass go to a holdout of 20 unused symbols, under a test fixed in writing beforehand (`PREREG.md`, `holdout_test.py`).

Validation: the live configuration reproduces the AI-read test exactly (861 trades, +43.2R).

## 1. The MACD-histogram gate — keep
Hybrid view, total R:

| | 33 symbols | watchlist (19) | holdout (20 unseen) |
|---|---|---|---|
| live | **+43.2R** (861 trades) | **+9.7R** | **−145.0R** |
| without the gate | −39.7R (1,359) | −44.5R | −227.6R |

The trades the gate blocks average:
- **in-sample: −0.196R** each (95% CI −0.32 to −0.06, n=1,909), negative in both halves of the period;
- **holdout: −0.366R** (CI −0.54 to −0.17).

Without the gate the engine buys the pullback while momentum is still running against it. Those entries lose.

**The engine's other MACD inputs: keep, the evidence is weak.**
- *Flat-momentum guard:* removing it costs a further −4.2R. The trades it blocks are inconclusive: +0.02R, n=108.
- *Confidence points* ("histogram rising" ±5, daily MACD against −10): in-sample, removing them costs −14R, and −26R out-of-sample. On the holdout, removing them gained +14R, but in only 2 of 4 folds.

## 2. MACD cross — no
- **As confidence points** (+0.05 or +0.10 for a cross in the trade's direction within 1, 3, 6 or 12 bars), the change vs live runs from −15.9R to +3.9R. There is no consistent gain: a cross rarely moves a trade across the 0.68 bar.
- **As a condition, in-sample:**

| cross required within | trades | per trade | total |
|---|---|---|---|
| 1 bar | 107 | +0.042R | +4.5R |
| 3 bars | 161 | +0.090R | +14.4R |
| **6 bars** | 232 | **+0.230R** | **+53.3R** (max DD 8.9R) |
| 12 bars | 405 | +0.127R | +51.3R |
| live (no requirement) | 861 | +0.050R | +43.2R |

  The 6-bar version is driven by crosses 4–6 bars old (+0.414R, n=228), which fell from +0.72R in the first 70% of the period to +0.08R in the last 30%.
- **On the holdout:** gap +0.106R, p = 0.25. **Rejected.**

## 3. Supply/demand — no
- **As confidence points:** −7R to +5R vs live, nothing consistent.
- **"Bounced off a zone on the trade's side within 3 bars" as a condition, in-sample:** 236 trades, +0.234R per trade, +55.2R; on the watchlist +43.2R vs +9.7R. But out-of-sample it was below live (−6.7R), and the 6- and 12-bar versions were worse still (−14.8R, −34.4R).
- **On the holdout it reversed:** −0.217R vs the trades without a bounce. **Rejected.**

## 4. Last week's high/low — no
- **As confidence points:** a break within 3 or 12 bars or this week, or a bounce within 3, 6 or 12 bars, is never better than +1.2R and mostly worse, down to −13.7R.
- **As conditions:**
  - "Break this week required": +53.5R, but −21.1R vs live out-of-sample.
  - "Bounce within 12 bars required": +35.2R, below live.
- Neither passed the in-sample rule, so neither went to the holdout.

## 5. The bigger finding (without the spread gate; see the correction below)
The unchanged engine **loses on symbols it has not been tuned on**: 548 trades on the holdout, −0.265R a trade.
- G10 crosses: −0.19R.
- Base metals and palladium: −0.53R.
- DXY: about 0R.

In-sample the profit comes from a few groups (Hybrid view, 18 months):

| group | trades | per trade | total |
|---|---|---|---|
| gold & silver | 68 | +0.72R | +48.8R |
| crypto | 66 | +0.20R | +12.9R |
| indices | 267 | +0.07R | +18.3R |
| forex (as the app classes it, incl. XPTUSDm and XZNUSDm) | 379 | 0.00R | 0.0R |
| energies | 81 | −0.46R | −36.8R |

Single-symbol totals rest on 15–38 trades each and are too noisy to judge one by one. The group pattern, though, matches both holdouts. Which instruments the desk trades matters more than any confidence point tested here.

## Correction: the live spread gate
These numbers charge each trade its spread, but they let through trades the live risk manager refuses (spread above 15% of the stop distance). With that gate applied:
- **The engine looks better.**
  - In-sample: 740 trades, +0.107R a trade, +79.3R.
  - Holdout: 319 trades, −0.060R, −19.0R — not the −0.265R above.
- **The MACD-histogram gate still clearly helps.** Without it: +11.4R in-sample, −53.2R on the holdout.
- **The supply/demand idea still fails.**
- **The MACD cross (H1) still misses the bar:** holdout gap +0.35R, p = 0.07. It stays rejected under the pre-registered rule; see the note in [PREREG.md](PREREG.md).

The group picture with the gate is in `analysis/watchlist/REPORT.md`.

## Reproduce (from `backend/`)
```
python ../analysis/macd/replay.py <dir>/bars.pkl <dir>/macd2            # in-sample, ~15 min
python ../analysis/macd/evaluate.py <dir>/macd2 <dir>/bars.pkl
python ../analysis/macd/fetch_holdout.py <dir>/holdout.pkl              # read-only MT5 export
python ../analysis/macd/replay.py <dir>/holdout.pkl <dir>/holdout
python ../analysis/macd/holdout_test.py <dir>/holdout <dir>/holdout.pkl
```
`bars.pkl` is the export of `analysis/ai_reads/fetch.py`.
