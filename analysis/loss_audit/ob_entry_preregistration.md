# Pre-registration: order blocks as an ENTRY method (written 2026-10-21, BEFORE any trade was priced)

## Why this exists
The user asked whether "price in a demand zone → buy, target the next supply zone" works as a way in.
Order blocks were tested only as a FILTER on the engine's trades (`ob_wall`) and as a TARGET
(`ob_target`) on 2026-10-19 - both rejected. They were never tested as an entry on their own. Every
rule below is fixed now, before a single trade is priced, and is not edited after the results.

## Data (already on disk, no broker connection)
- DEV: `audit/bars.pkl` - the original 33 symbols, 7,000 1h bars each (2025-07 → 2026-10-19), per-bar spread.
- HOLDOUT: `holdout/bars.pkl` - the 16 G10 crosses never used to build anything:
  AUDCADm, CADCHFm, EURAUDm, EURCADm, EURCHFm, EURGBPm, EURNZDm, GBPAUDm, GBPCADm, GBPCHFm, GBPNZDm,
  NZDCADm, NZDCHFm, NZDJPYm, NZDUSDm, USDCADm. (Used before for two unrelated tests; never for this.)
- Timeframe 1h. The first 500 bars of each symbol are warm-up (no trades).

## Zones
Exactly `app/agents/order_blocks.py`'s creation rule (swing pivot 3/3, break by a FINISHED bar's close,
origin = last opposite candle within 12 bars, displacement ≥ 1.2 ATR at the origin, impulse ≤ 10 bars,
zone = origin candle range, or body when wider than 1.5 ATR). Run once per symbol on the full series;
each zone is usable only AFTER its breaking bar has closed (`confirmed_index`), so nothing is seen early.
Strength = the detector's own `_score` at creation (untouched, so it carries the +15 "fresh" points).
Each zone is traded at most once - on its FIRST return - and ignored after 400 bars (the detector's age cap).
Merging and the top-8 display cap are not applied: every zone the rule produces is a candidate.

## Hypotheses (two, fixed)
**H1 - first-touch limit (the plain idea).** For a demand zone with strength ≥ 70: a buy LIMIT at the
zone top, live from the bar after confirmation. It fills on the first bar whose ask touches the top
(bid low ≤ top − that bar's spread); entry = top. Supply zones mirror it (sell limit at the bottom,
filled when the bid high ≥ bottom).

**H2 - confirmation + trend (the careful version).** Same zones and strength. On the zone's FIRST touch
bar only: the bar must CLOSE back outside the zone in the trade's direction (demand: close > top), AND
the 4h trend must agree (last COMPLETED 4h bar: EMA20 > EMA50 for longs, < for shorts). Entry at the
NEXT bar's open (ask for a buy, bid for a sell). If the touch bar fails either check, that zone is done.

## Common trade rules
- Stop: zone bottom − 0.1 × ATR(14) for longs (zone top + 0.1 ATR for shorts), ATR at the fill bar.
- Target: the nearest opposing zone (supply for a long) that exists and is still untouched at entry,
  strength ≥ 55, beyond entry - its near edge. Skipped if that gives R:R < 1.5; capped at 4R. With no
  opposing zone ahead: 3R.
- Time stop: exit at the close 72 bars after entry.
- Exits on bid/ask honestly: a long's stop/target trigger on the bid; a short's on the ask (bid + spread).
  A bar that touches both stop and target counts as the STOP. On the entry bar, only the stop is checked.
- One open trade per symbol; a signal while one is open is skipped.
- Result per trade in R = (exit − entry) / (entry − stop) for longs (mirrored for shorts), net of spread.

## Decision rule
For EACH hypothesis separately:
- **SUPPORTED** only if the HOLDOUT mean net R > 0 with a one-sided bootstrap p < 0.025 (two
  hypotheses → 0.05/2), AND the DEV mean is also > 0.
- **REJECTED** otherwise. No re-running with other strengths, stops, targets, timeframes, symbols or
  windows to rescue it.
- If the HOLDOUT has fewer than 50 trades: **INCONCLUSIVE (underpowered)**, whatever the sign.
Also reported (descriptive, not deciding): win rate, profit factor, total R, 4 time folds, per asset
class, DEV ex-metals (DEV's past edge was ~80% metals - see edge-concentration notes).

## Known caveats, stated in advance
- FX crosses are correlated with the FX pairs in DEV, so the holdout is not fully independent.
- 1h bars: intrabar order is unknown; the stop-first rule makes results conservative.
- Swap and commission are not included (Exness Standard: no commission; swap ignored for ≤ 72h holds).

---

## RESULT (appended 2026-10-21 after both samples ran - nothing above was edited)

Validation first: the replay's zones were checked against `find_order_blocks` on a live-style 500-bar
window (EURUSDm, ending bar 3000): every zone the live detector reports is in the replay (0 missing).

| sample | hypothesis | n | mean net R | win | PF | total R | one-sided p (trades / days) |
|---|---|---|---|---|---|---|---|
| DEV 33 | H1 first-touch limit | 7,145 | −0.149 | 20.6% | 0.82 | −1,062 | 1.000 / 1.000 |
| DEV 33 | H2 confirm + 4h trend | 1,797 | −0.146 | 23.2% | 0.81 | −263 | 1.000 / 1.000 |
| HOLDOUT 16 | H1 first-touch limit | 3,386 | **−0.279** | 17.9% | 0.66 | −945 | 1.000 / 1.000 |
| HOLDOUT 16 | H2 confirm + 4h trend | 747 | **−0.215** | 22.2% | 0.73 | −161 | 1.000 / 1.000 |

Negative in **every one of the 4 time folds** in all four rows. DEV ex-metals: H1 −0.145R, H2 −0.161R.
By class (DEV): only crypto is marginally above zero (H1 +0.044R n=419, H2 +0.020R n=140) - not tested,
not a lead (one of five classes, tiny, inside noise).

**Verdict under the pre-registered rule: H1 REJECTED, H2 REJECTED.** Both lose on unseen data, and on
the development set too. Not re-run with other parameters.

Diagnostics (descriptive only, NOT the test): with spread set to zero the edge is ~0 (DEV H1 −0.003R,
H2 −0.061R; HOLDOUT H1 −0.033R, H2 −0.041R). With zero spread AND the most generous intrabar assumption
(the entry bar can neither stop nor pay you) it is still ~0 (−0.055R … +0.024R). So the zones carry no
directional information at the 1h scale; the losses are simply the spread paid on ~1 ATR-wide stops.
