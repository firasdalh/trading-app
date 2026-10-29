# AI reads — tested, all three removed (2026-10-28)

Scope: the three switches Settings → Strategy marked "UNTESTED" — AI momentum-read, AI regime-read,
AI price-action-read — replayed through the live engine on 18 months of MT5 data, with the live model
answering the exact prompts it gets live.

**Verdict: remove all three.** None makes the engine better; the momentum read makes it clearly worse.
Applied the same day: the three switches are OFF (through the settings API, no backend restart) and the
Settings panel no longer offers them.

| read | what its "go" answer does | verdict | in one line |
|---|---|---|---|
| momentum | `healthy_pullback` at value → market entry into the pullback | **remove** | its entries lose **−0.22R a trade** (95% CI −0.36 to −0.07) — worse than the ones it turns down |
| regime | `choppy_range` → range playbook; with trend-only ON, `emerging_trend` → trade the moderate-ADX trend | **remove** | says "choppy" 7 times in 2,077; its "emerging" calls pick no better than taking every trade |
| price-action | `likely_break` → market entry through a big-TF level | **remove** | ranks breaks a little, but the trades it adds are break-even overall and lose in the last 30% of the data and on the watchlist |

## 1. How each read reaches a live decision

| read | when it is asked | what a label changes |
|---|---|---|
| momentum | an entry-TF pullback against the trend (MACD histogram against by >10% ATR after the higher-TF gate, or RSI ≥70 / ≤30 with ADX <23) | `healthy_pullback` at value (within 1 ATR of EMA20) → **market entry now**; stretched → arm a limit at value; `weak_momentum` → arm a resumption; `probable_reversal` → stand aside |
| price-action | a 4h/1d level within 1 ATR in the trade's path | `likely_break` → **market entry through the level**; otherwise wait / arm the break |
| regime | ADX 20–23 and ATR ratio <1.6 ("moderate") | `choppy_range` → treated as a range (trend trade dropped). `emerging_trend` → treated as a trend, which only matters with trend-only ON (moderate is already traded as a trend with it OFF). `transition` → nothing |

A non-"go" answer never produces a market entry, and an answer below 0.55 confidence (or no answer) falls
back to the rule, so each read can only ADD trades (momentum, price-action, regime with trend-only ON) or
DROP them (regime "choppy").

Who consumed them live (AI decides OFF, Hybrid arming OFF): **Run analysis** — all three. **Hybrid's scan**
runs without them (`preview_symbol(use_llm=False)`), so a momentum / price-action / "emerging" answer
could never create a Hybrid candidate; in the final re-check of the chosen pair only a regime "choppy"
could act (a veto), and the model practically never says it. The "Hybrid" view below therefore answers
"would they help if Hybrid used them?", not "what did they do to Hybrid".

## 2. Method

- **Data**: MT5 (Exness), 33 symbols — the live watchlist minus IN50m (no data since 2023), 19 symbols,
  plus 14 from the loss-audit universe — ~7,000 1h bars each, 2025-04-30 → 2026-10-28, with 4h/1d context.
- **Engine**: the live `_deterministic_decision` with the live filter set, faithful windows
  (`simulator._context_window`: completed higher-TF bars + the forming one rebuilt from 1h bars), entry at
  the decision bar's close, stop first on an ambiguous bar, 96-bar time stop, 3-bar cooldown, one position
  per symbol, R net of the symbol's spread (the larger of today's and the entry bar's). No commission/swap.
- **Exploration first** (`explore.py`): every bar decided under eight variants that force each read's "go"
  answer (and trend-only OFF/ON). Because a non-"go" answer never trades, the variants rebuild the decision
  for any set of answers, and the model is asked only where its answer can change a trade: **16,197
  decisions** (momentum 3,718, regime 4,530, price-action 7,949).
- **Answers** (`label.py`): the live model (gpt-5-mini, `reasoning_effort=low`) with each module's own
  `_SYSTEM` + `_snapshot` prompt, schema and token budget, and the engine's 0.55 confidence floor. The
  model's knowledge ends before this data starts and the prompt carries no date.
- **Coverage**: the OpenAI credit ran out at 19:18 (UTC+4) partway through, after **9,986 decisions (62%)** —
  momentum 61%, regime 61%, price-action 63%. They were asked in random order, so the answered set is a
  random sample. The event study (§4) uses answered decisions only. The backtests (§5) apply the AI where
  it answered and the rule elsewhere, so their totals understate an AI effect by about a third; the
  per-trade numbers are unaffected.
- **Repeatability**: a second, independent ask of 2,500 random decisions.
- **Views**: **Hybrid** = only setups Hybrid's gate passes (confidence > 0.68, never a range fade), gate
  applied inside the path; **All signals** = everything Run analysis shows. In-sample/out-of-sample split
  at 70% of the time span (OOS from 2026-05-17), plus four equal time folds. 95% CIs resample whole
  symbol-weeks (neighbouring bars at the same level are near-duplicates, not independent trades).

## 3. How the model answers

| read | answered | answer mix | same prompt asked twice: label differs | trade decision differs |
|---|---|---|---|---|
| momentum | 2,252 | healthy_pullback 58%, probable_reversal 32%, weak_momentum 10% | 36% | **28%** (n=345) |
| regime | 2,750 | transition 50%, emerging_trend 50%, choppy_range 0.3% | 15% | 15% (n=427) |
| price-action | 4,984 | likely_break 48%, indecision 29%, likely_reject 22%, below 0.55 1% | 23% | 11% (n=810) |

More than one momentum decision in four flips when the identical question is asked again. The regime read
is a coin toss between "transition" and "emerging" and essentially never says "choppy" — the one answer
that acts while trend-only is OFF.

## 4. Does the answer pick the better trades?

Every decision where "go" changes the outcome, valued by the trade "go" opens (net R; path-independent).

| read | view | if always "go" | model said "go" | model said otherwise | "go" at conf ≥0.8 / <0.8 |
|---|---|---|---|---|---|
| momentum | all | −0.184R (1,701) | **−0.223R** (1,023) [−0.36, −0.07] | −0.126R (678) | −0.283R / −0.162R |
| momentum | Hybrid | −0.161R (1,263) | **−0.214R** (768) [−0.38, −0.05] | −0.079R (495) | −0.287R / −0.137R |
| price-action | all | +0.001R (4,390) | +0.059R (2,351) [−0.07, +0.18] | −0.065R (2,039) | +0.140R / −0.006R |
| price-action | Hybrid | +0.015R (3,233) | +0.056R (1,854) [−0.08, +0.20] | −0.042R (1,379) | +0.147R / −0.027R |
| regime "emerging", trend-only ON | all | +0.104R (209) | +0.068R (160) | +0.222R (49) | +0.546R (26) / −0.025R |
| regime "emerging", trend-only ON | Hybrid | +0.016R (141) | +0.022R (110) | −0.008R (31) | +1.255R (13) / −0.143R |
| regime "choppy", trend-only OFF | all | — | said 7 times in 2,077 | — | — |

**Momentum**: its "go" trades lose with a CI that excludes zero, lose more than the trades it turns down,
and lose more the more confident it is. "Go" is negative in all four time folds (−0.35, −0.42, −0.05,
−0.17R). The second draw agrees (−0.377R, CI −0.59 to −0.16).

**Price-action**: the one read with some skill — its "break" picks beat its declines by **+0.124R**
(CI +0.01 to +0.24; Hybrid view +0.098R, CI −0.04 to +0.25). But the trades it adds are about break-even,
and the edge sits in the first part of the period:

| | first 70% | last 30% |
|---|---|---|
| "go", all 33 | +0.125R (1,403) | **−0.040R** (948) |
| "go" at conf ≥0.8, all 33 | +0.201R (637) | +0.041R (401) |
| "go" at conf ≥0.8, watchlist 19 | +0.100R (387) | **−0.085R** (274) |

Folds ("go", all): −0.45, +0.22, +0.05, −0.02R. A free rule on the numbers already in the prompt
("momentum building and volume expanding") separates nearly as well: gap +0.087R (CI −0.07 to +0.25).

**Regime**: with trend-only ON, the trades it lets through are no better than the ones it holds back
(gap −0.15R, CI −0.75 to +0.47). The ≥0.8-confidence rows look good but rest on 26 and 13 trades.

## 5. Backtests (path-dependent, one position per symbol)

Hybrid view, draw 1 (draw 2 in brackets where the AI acts):

| config | trades | per trade | total | max DD | IS / OOS |
|---|---|---|---|---|---|
| **33 symbols** | | | | | |
| rules only, trend-only OFF | 953 | +0.017R | +16.4R | 47.2R | +0.041 / −0.015 |
| + momentum read | 1,278 | −0.035R | **−44.3R** (−20.4) | 81.9R | −0.049 / −0.014 |
| + price-action read | 1,263 | +0.022R | +27.4R (+4.8) | 55.2R | +0.071 / −0.047 |
| + regime read | 953 | +0.017R | +16.4R (no change) | 47.2R | +0.041 / −0.015 |
| + all three (your setup when the test started) | 1,560 | −0.024R | −37.6R (−40.3) | 83.2R | −0.006 / −0.050 |
| rules only, trend-only ON | 861 | **+0.050R** | **+43.2R** | 46.0R | +0.062 / +0.035 |
| trend-only ON + regime read | 905 | +0.041R | +36.9R (+39.6) | 51.4R | +0.061 / +0.014 |
| trend-only ON + all three (your setup when it ended) | 1,472 | +0.002R | +3.0R (−14.8) | 68.0R | +0.020 / −0.023 |
| **watchlist 19** | | | | | |
| rules only, trend-only OFF | 580 | −0.025R | −14.4R | 48.7R | −0.032 / −0.016 |
| + momentum read | 767 | −0.086R | −66.0R (−63.9) | 81.6R | −0.136 / −0.020 |
| + price-action read | 777 | −0.066R | −51.5R (−37.8) | 65.9R | −0.033 / −0.107 |
| rules only, trend-only ON | 521 | +0.019R | +9.7R | 41.8R | +0.021 / +0.016 |
| trend-only ON + regime read | 548 | −0.005R | −2.5R (+8.7) | 44.3R | +0.008 / −0.019 |
| trend-only ON + all three | 887 | −0.073R | −64.7R (−61.0) | 72.8R | −0.061 / −0.088 |

The trades each read adds or changes, on their own (Hybrid view, 33 symbols): momentum 370 at **−0.209R**
(draw 2: 126 at −0.374R); price-action 535 at +0.036R, OOS −0.034R (draw 2: 208 at −0.010R); regime with
trend-only ON 86 at +0.001R. On the watchlist the price-action trades are −0.102R (OOS −0.183R).

All-signals view (Run analysis), 33 symbols: the trades the momentum read adds average −0.203R (467);
the price-action read's +0.033R (543), OOS −0.083R.

## 6. Side finding: trend-only mode

The same data scores trend-only ON above OFF: Hybrid view +0.050R vs +0.017R a trade (+43.2R vs +16.4R;
watchlist +9.7R vs −14.4R), positive in three of four folds (−0.08, +0.04, +0.06, +0.05R). It's a small
edge, not a strong one. The large difference is in Run analysis: with trend-only OFF it also offers
range-regime setups — failed breaks (1,377, −0.387R), range breakouts (2,131, −0.223R) and fades
(408, −0.253R) — about −1,110R together; with it ON, all signals total +24.7R (930 trades). Trend-only was
switched ON during the test and stays ON. Its Settings text was updated: the threshold is ADX ≥ 23 (it said
25), and the old "~0R, not recommended" note is replaced with these numbers.

## 7. What changed, what's left

Done 2026-10-28, without a backend restart (the live confirmation is untouched):
- `ai_momentum_read`, `ai_regime_read`, `ai_priceaction_read` → OFF via `POST /api/settings/ai-*-read`.
- `SettingsPanel.tsx`: the three switches replaced by a "REMOVED" note; trend-only text corrected.

Left for a quiet moment (backend edits reload uvicorn and reset the live confirmation):
- Delete the three modules and their engine hooks (`_momentum_action`, `_regime_refine`, `_level_action`)
  — dormant while the flags are OFF.
- Defaults are still `True` in `db.py`, `schemas.py` and the `database.py` migration: a fresh database
  would start with the reads ON.

Not covered: RSI-Over's "AI level check" (`pa_confirm`) uses the price-action classifier differently
(a veto on fades). It is OFF and RSI-Over is disabled.

## 8. Cost

9,986 + 2,500 answers: 6.8M input + 4.6M output tokens at gpt-5-mini list prices ($0.25 / $2.00 per 1M) =
**$10.84**. The remaining 6,211 first-draw decisions would cost about $5.40; they can't overturn the
momentum verdict (its CI already excludes zero), and the price-action and regime verdicts would need the
unanswered 38% to behave very differently from a random sample of the same decisions.

## 9. Reproduce (from `backend/`, MT5 terminal running)

```
python ../analysis/ai_reads/fetch.py <dir>/bars.pkl                         # read-only MT5 export
python ../analysis/ai_reads/explore.py <dir>/bars.pkl <dir>/explore         # 8 variants, every bar (~16 min)
python ../analysis/ai_reads/collect.py <dir>/explore <dir>/needed.json      # decisions where an answer matters
python ../analysis/ai_reads/label.py <dir>/needed.json <dir>/labels.jsonl --draws 1          # live model
python ../analysis/ai_reads/label.py <dir>/needed.json <dir>/labels_d2.jsonl --draw-ids 2 --sample 2500
python ../analysis/ai_reads/evaluate.py <dir>/explore <dir>/labels.jsonl,<dir>/labels_d2.jsonl <dir>/bars.pkl
python ../analysis/ai_reads/extras.py <dir>/explore <dir>/labels.jsonl      # calibration, free rules, time splits
```

`label.py` is resumable and asks in a fixed random order, so a partial run is a random sample.
