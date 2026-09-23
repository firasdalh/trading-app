# Strategy review — where the desk still leaks (2026-09-23)

A second pass over the system *after* the 2026-09-16/17 look-ahead audit. That audit fixed the
measurement (`_context_window`) and the engine (late-trend gate, 3R target, inverted confidence
factors removed). This pass asks three different questions:

1. Are the audit's own conclusions actually **switched on** in the live config?
2. Does anything in the system **rank** one setup above another?
3. Is there a filter the audit never tested, because it isn't a chart property?

The answer to (1) is "mostly no", (2) is "nothing does", and (3) turned out to be **no** — the one
candidate looked excellent and then failed validation, which is written up in full in §4 because the
way it failed matters more than the idea did.

Evidence: the live journal since the 2026-07-08 reset (386 closed trades), the MT5 account, 1,200
Hybrid tick records, an independent replay of every live trade on real 15m candles, and two fresh
faithful backtests over a re-fetched year of MT5 data (33 symbols, 6,500 1h bars — 3,093 and 2,986
trades), priced at the real spread of each trade's own entry bar.

Two of the three things this pass proposed did not survive being measured (§3, §4). Both are kept
in full rather than deleted: on this project the *way* an idea fails has been worth more than the
idea.

---

## 0. The arithmetic that actually emptied the account

The audit reported drawdown in R and never converted it to money. That conversion is the whole story.

| | |
|---|---|
| Closed trades since the reset | 386 |
| Win rate | 45.1% |
| Net | **−20.3R / −$1,473** |
| Account now | **$1,181** (started ≈ $2,655) |
| Realised drawdown | **≈ −55%** |

−20.3R at ~3% risk per R compounds to −55%. The account did exactly what the risk setting says it
must. Now apply the same conversion to the audit's **best** configuration (max DD 44R):

| risk/trade | a 44R drawdown becomes | needed to recover |
|---|---|---|
| 1% | −36% | +56% |
| 2% | −59% | +143% |
| **3% (live)** | **−74%** | **+282%** |

The shipped, "validated", positive-expectancy engine is **not survivable at 3%**. Its own measured
drawdown wipes out three quarters of the account before the +188R ever arrives. `RISK.md` documents
the cap as 2% and recommends 1%; the live value is 3%, with `max_open_positions` raised from 3 to 5
— up to 15% of equity at risk simultaneously.

**This is the single highest-impact change, and it is not a strategy question.** No entry filter in
this repo moves the result as much as halving the risk setting does.

---

## 1. The audit's conclusions were written down but never switched on

| setting | live value | what the audit measured |
|---|---|---|
| `conditional_enabled` (arming) | **ON** | every armed variant −0.14R to −0.25R; *"Arming should be OFF"* |
| `max_spread_r_fraction` | **0.25** | ≥25% spread/stop = **−0.626R**; 10–25% = −0.109R; only <3% positive |
| `weekend_flatten_enabled` | **OFF** | UKOILm gapped through its stop for −8.9R in one trade |
| watchlist | includes **AUS200m, XNGUSDm, UK100m** | all three on the audit's *"consistently negative in every run"* list |
| `minrr` filter | **DISABLED** | an ablation flag left on in production — measured as ~neutral (see §3) |
| `risk_per_trade` | **3%** | `RISK.md`: cap 2%, recommended 1% |
| `max_open_positions` | **5** | `RISK.md` default 3 |

Live confirmation of the arming row: `armed_hybrid` is **−$421 over 29 trades (−$14.5/trade)** —
worse per trade than any other source in the book, and worse in total than the 177 Hybrid market
entries combined (−$335).

---

## 2. Nothing in the engine ranks trades — and the Hybrid is built as if something does

**Confidence does not rank outcomes above its own gate.** Live book, all sources:

| confidence | n | expectancy |
|---|---|---|
| < 0.67 | 67 | −0.250R |
| 0.67–0.75 | 117 | −0.066R |
| 0.75–0.82 | 32 | +0.006R |
| 0.82–0.88 | 35 | +0.082R |
| ≥ 0.88 | 135 | +0.008R |

corr(confidence, R) = **+0.069, t = 1.36** — not significant. The 0.67 gate separates bad from
not-bad; **above it the score carries no information.** Consistent with this, the Hybrid's trades
average *higher* confidence than the human-approved ones (0.829 vs 0.776) and perform far worse
(+1.0R over 177 trades vs +15.4R over 36).

**The new measured-odds module does not rank either.** `odds.py` was adopted on 2026-09-18 as the
answer to "neither our confidence score nor the outside read looked at barrier distance". Computed
on **pre-trade candles only** for all 371 matchable live trades:

corr(edge_r, realised R) = **−0.004, t = −0.08.**

Zero. Gating on `edge_r >= 0` would have *kept* the worse half (−0.138R) and *dropped* the better
one (+0.002R). It is honest as a read-out; **it must not be promoted to a gate or a ranking key.**

**And the ranking never fires anyway.** Across 1,200 recorded Hybrid ticks:

| candidates found in a tick | ticks |
|---|---|
| 0 | 1,148 |
| 1 | 49 |
| 2 | 2 |
| 3 | 1 |

The Hybrid is **not a selection engine, it is an acceptance engine.** "Open the single best setup",
the confidence ordering, and the shipped `_FRESH_HTF_LEG_BARS` 4h-freshness tiebreak are all
effectively dead code — they can only matter in 3 ticks out of 1,200. Over those same ticks the
Hybrid opened **42 trades and armed 188 conditionals**: its dominant output is the one path the
audit proved loses money.

---

## 3. `minrr` vs the 3R target: the mechanism is real, the leak is not. Measured.

**Verdict: optional. Mildly preferable to re-enable, but it fixes nothing.** I first wrote this
section up as a bug. Measurement disagreed; the original claim is kept below so the correction is
legible.

### The mechanism (this part is true)

`minrr` sits in `disabled_filters` — an ablation flag left on in production. With it off
(`orchestrator.py:1979`):

    take_market = "minrr" in disable

the engine takes a market entry in the case it has just diagnosed as awkward: a key level sitting
**closer than 1.5R** ahead, with the trend judged **not strong enough to break it** (`strong` is
False on that branch). With `minrr` on it stands aside and arms a better-priced entry instead.

Then `target_floor`, added months later on 2026-09-17 (`orchestrator.py:2211`), lifts the
take-profit to **3R — beyond the wall the engine just said price cannot clear.** The two were never
designed to coexist: the ablation flag predates the 3R target.

### What it is actually worth

A full faithful re-run with `minrr` enabled (2,986 trades vs 3,093), Hybrid-gated, at true per-bar
cost:

| | minrr OFF (live) | minrr ON |
|---|---|---|
| gated `trend` | n=1,239, **+0.109R**, +134.6R | n=1,120, **+0.121R**, +135.2R |
| 4 time folds | +0.109 +0.133 +0.110 +0.082 | +0.106 +0.151 +0.129 +0.097 |

And the trades `minrr` actually removes, priced on their own:

| | n | expectancy | PF | total |
|---|---|---|---|---|
| removed, gated trend | 301 | **+0.171R** | 1.24 | **+51.5R** |

**The trades the "bug" admits are better than the book average, not worse.** My reasoning — enter
into a wall, aim through it, therefore lose — was wrong. The runs are path-dependent (blocking a
trade frees the engine to take 267 different ones later), so it is not a clean subtraction, but the
conclusion holds: this is not a leak.

What re-enabling `minrr` really buys is **the same total return from 10% fewer trades** (+135.2R vs
+134.6R), with slightly better per-trade expectancy and slightly steadier folds. That is worth
having — fewer trades means less slippage and spread exposure than the harness models, and less
execution risk — but it is a preference, not a fix. Nothing breaks if you leave it as it is.

**The lesson is the same as §4, one section early:** a mechanism that is clearly visible in the code
and obviously bad when described in words still has to be measured before it is called a leak. This
is the second plausible story in this review that measurement rejected.

---

## 4. A session filter looked strong on the live book — and then FAILED validation. Rejected.

**Verdict: do not ship this. Recorded here so it is not re-discovered.**

I found what looked like the strongest signal in the book, and a full-year backtest killed it.
The sequence is worth keeping because the failure mode is the one this project keeps hitting.

### What looked so convincing

Live journal by entry time (UTC):

| window | n | expectancy | total | win% |
|---|---|---|---|---|
| **overlap 12:00–16:00** | 66 | **+0.331R** | **+21.8R** | 59% |
| ny-pm 16:00–22:00 | 82 | −0.073R | −6.0R | 46% |
| london 06:00–12:00 | 78 | −0.148R | −11.5R | 40% |
| asia 22:00–06:00 | 160 | −0.154R | −24.6R | 41% |

41% of all trades were taken in the worst window, and the app does run a 40-minute tick around the
clock against a watchlist that is 11 of 14 European and US instruments — so the story was tidy too.

Every robustness check I could run on the live book passed:

- **Stable across time.** First half of the book +0.346R, second half +0.315R.
- **Permutation test:** overlap vs rest, p = **0.0017** (asia vs rest is weaker, p = 0.08).
- **Broad-based:** 16 of 21 distinct symbols positive in the window; still +0.273R after dropping
  the single best symbol; positive in both directions (long +0.531R, short +0.104R); positive for
  both the Hybrid (+0.399R) and the human-approved trades (+0.953R).
- **Confirmed by an independent method.** Replaying every live trade on real 15m candles at fixed R
  targets (stop checked first, same-bar ties resolved as a loss — the conservative assumption)
  removes all exit-management noise and tests the *entries* alone:

| target | overlap (n=63) | other (n=157) | asia (n=157) |
|---|---|---|---|
| 1.5R | +0.190R | +0.006R | −0.243R |
| 2.0R | **+0.299R** | +0.048R | −0.229R |
| 3.0R | +0.208R | −0.005R | −0.155R |
| 4.0R | +0.081R | −0.047R | −0.159R |

Overlap is positive at **every** target; asia is negative at every target. That is an entry-quality
difference, not an exit or a sizing artefact.

### How it died

I re-fetched a year of candles and replayed the **live filter set** through the faithful harness:
33 symbols, 6,500 1h bars, **3,093 trades**, net of each symbol's spread. The harness reproduces the
audit (Hybrid-gated: +0.121R, PF 1.17, +155.9R — the audit's +0.147R on independently pulled data).
Bucketing those trades the same way, corrected for the fact that the backtest stamps a trade with
its bar's OPEN time while the fill is at that bar's close (+1h):

| population | overlap | london | ny-pm | asia |
|---|---|---|---|---|
| gated, 33 symbols, full year (n=1,279) | **−0.037R** | +0.077R | +0.175R | **+0.214R** |
| gated, watchlist 14 only (n=584) | **−0.108R** | +0.073R | +0.080R | **+0.085R** |
| gated, trend strategy only (n=1,239) | **−0.049R** | +0.100R | +0.201R | **+0.202R** |

**Exactly inverted.** On 1,279 trades the overlap window is the *worst* bucket and the Asian session
is the *best* — the opposite of what 386 live trades said, in every population slice.

The obvious objection is that the harness under-charged the thin hours, so I fixed that (§6) and
re-ran with each trade paying the **real spread of its own entry bar**. The inversion survives:
gated trend trades come out **overlap −0.062R, london +0.100R, ny-pm +0.187R, asia +0.168R**. The
Asian session still wins after being charged its true cost.

Things I checked before accepting that, all of which came back clean:

- **Clock mismatch?** No. The Exness server is **UTC+0** (measured against `symbol_info_tick`), so
  the journal and the candles are on the same clock. The only offset is the bar-open/fill +1h,
  corrected above.
- **Was it just "the human was awake"?** No. The effect was present in the automated-only subset
  (Hybrid alone: overlap +0.399R vs asia −0.132R), and the human and Hybrid books put a similar
  share of trades in the window (21% vs 16%).
- **A cost effect?** No, and this is worth keeping: Exness spreads on these symbols are **flat
  across sessions** — the median H1 spread in the Asian block equals the overlap block for DE30m,
  USTECm, USOILm, BTCUSDm, US500 and AUDUSDm. The one real widening is the **22:00–00:00 rollover**
  (UK100m 858 vs 142 points, DE30m 100 vs 16, HK50m 440 vs 148).

### Why it was wrong, and the lesson

**I chose the window after looking at the hourly table.** I scanned 24 hourly buckets and 4 session
buckets, picked the best-looking one, and then ran a permutation test *on the window I had already
selected*. That p = 0.0017 does not correct for the ~28 comparisons it came from, so it was never
worth what it appeared to be. Every "robustness check" that followed was run inside the same 386
trades and could only confirm the thing they were built from.

This is the same failure the look-ahead audit was written to stop, wearing different clothes: the
earlier one measured a real pattern with a broken harness; this one measured a fake pattern with a
sound harness on too little data. **A live sample of 386 trades cannot support a filter choice.**
The audit's own rule — ship only what moves the same way in both halves *and* most folds of a large
independent sample, on a plateau of neighbouring values — would have rejected this at step one.

`_session_quality()` at `orchestrator.py:730` should therefore stay exactly where it is: computed,
printed in the rationale, and **not** wired to the decision.

---

## 5. The 3R target: live replay disagrees, but the backtest wins

From the candle replay, all 377 live trades:

| target | 1.0R | 1.5R | 2.0R | 2.5R | **3.0R** | 4.0R | 5.0R |
|---|---|---|---|---|---|---|---|
| expectancy | +0.004R | −0.067R | −0.026R | −0.035R | **−0.032R** | −0.072R | −0.083R |

2R edges out the shipped 3R (−0.026R vs −0.032R). **This is not enough to act on**, for the same
reason §4 collapsed: it is the same 377-trade sample, the differences are inside the noise, and the
audit's 3R decision rested on a path-dependent replay that was positive in all four folds of a much
larger set. A fixed-target replay also cannot see what the audit's could — that a longer hold blocks
re-entries, which is part of why 3R scored well there. Logged as a mild disagreement, not a change.

---

## 5b. What the fresh backtest DOES support

All figures below are at **true per-bar cost** (see §6 — the old cost model was optimistic by
0.042R/trade, so these are lower than a re-run of the audit would print):

| | |
|---|---|
| all 3,093 signals, net | −0.113R, PF 0.87 |
| **through the Hybrid gate (1,279)** | **+0.104R, PF 1.14, +132.7R** |
| of which `trend` strategy | +0.109R, PF 1.15 — **IS +0.105 / OOS +0.116** |
| 4 time folds (trend) | +0.109, +0.133, +0.110, **+0.082** — positive in all four |
| `failed_break` (ungated) | **−0.276R over 1,124 trades, −310.2R** |
| `ranging` regime (ungated) | **−0.240R over 1,647 trades, −394.5R** |

Two things follow:

1. **The confidence gate is the one component that clearly works.** Of 3,093 raw signals only 1,279
   survive it, and **97% of those are `trend`** — the gate already excludes almost all of
   `failed_break` and `mean_reversion`, which is exactly what the audit predicted ("mostly harmless
   live"). It cannot *rank* (§2), but it does separate. Do not lower it.
2. **The engine's trend path reproduces on independently fetched data** — positive in all four
   folds and out-of-sample, and it survives being charged the real spread it actually paid
   (+0.126R → +0.109R). That is the real asset in this repo.

**But the account damage was not done by this engine.** The late-trend gate, the 3R target and the
confidence fix all shipped on 2026-09-16/17. The −20.3R book was produced almost entirely by the
*pre-fix* engine. The fixed engine has **7 live trades (+2.0R)** — no information at all.

That reframes everything: you are about to forward-test a barely-evidenced engine, and the only
decision that matters is at what size. Which is §0.

---

## 6. Harness fix (shipped): every backtest in this repo was optimistic by ~0.04R/trade

`analyze.py` charged every trade of a symbol the **same** spread — one snapshot taken at fetch time
— because `fetch_data.py` discarded MT5's per-bar `spread` field. The harness was therefore
structurally unable to see the cost of trading outside the liquid window.

That cost is not small. UK100m's real median spread, by UTC hour:

| hours | 00–06 | 07–19 | 20 | 21 | 22–23 |
|---|---|---|---|---|---|
| spread (points) | 300 | **142** | 675 | **1000** | 858 |

A trade opened at 21:00 was being costed as if it had been opened at midday — a **7×**
under-charge. (My earlier block-median estimate in §4 washed this out; the per-bar data is the
honest version.)

Both files are fixed: `fetch_data.py` now stores the bar's spread as a 7th tuple element, and
`analyze.py` indexes it by `(symbol, bar-open ts)` and falls back to the snapshot for pickles
fetched before the change, so old results still reproduce exactly.

Re-costing the same 3,093 trades:

| | old cost model | true per-bar cost |
|---|---|---|
| median cost/trade | 0.074R | **0.082R** |
| all signals, net | −0.071R | **−0.113R** |
| gated `trend` | +0.126R | **+0.109R** |

**Every prior number in this project is ~0.04R/trade optimistic**, including the audit's headline
+0.147R. Two things follow, and they point opposite ways:

- The engine's trend edge is **smaller than advertised** — call it ~+0.10R, not ~+0.15R.
- It is also **real**: it survives paying its true costs, still positive in all four folds and
  out-of-sample. That is the stronger claim of the two, because it is the one that was tested
  against its own worst case.

---

## 7. What I would change, in order of expected impact

**Tier 1 — risk (do this first; it dominates everything below).**

1. `risk_per_trade` 3% → **1%**. At the audit's own 44R drawdown this is the difference between
   −36% and −74%. Nothing else in this document moves the needle as far.
2. `max_open_positions` 5 → **3** (the `RISK.md` default).

**Tier 2 — turn off what is already measured as losing.**

3. Hybrid `conditional_enabled` → **OFF**. It is the Hybrid's dominant output (188 arms vs 42 opens)
   and its worst source (−$14.5/trade).
4. `max_spread_r_fraction` 0.25 → **0.06**. Free: the audit measured everything above it as negative.
5. `weekend_flatten_enabled` → **ON**.
6. Remove **AUS200m, XNGUSDm, UK100m** from the watchlist — the audit's own blacklist.

**Tier 3 — code changes.**

7. **Optional:** re-enable `minrr` (remove it from `disabled_filters`). Measured (§3), this is not
   the bug I first took it for — it buys the same total return from 10% fewer trades, with slightly
   steadier folds. Worth it for the reduced execution exposure; safe to skip.
8. ~~Fix `analyze.py`'s cost model.~~ **Done** — see §6. `fetch_data.py` + `analyze.py` now use the
   real per-bar spread. It did not change the retraction in §4 (the inversion survives true costs),
   but it did reveal that every backtest in this repo was ~0.04R/trade optimistic. Re-fetch
   `bars.pkl` to benefit; old pickles still work via the snapshot fallback.
9. Leave `_TARGET_MIN_R` at 3.0 (§5) and leave `_session_quality()` unwired (§4). Both were
   candidates; neither survived.

**Tier 4 — the honest strategic point.**

The engine's trend path measures **+0.109R, PF 1.15, positive in all four folds and out-of-sample**
on a freshly pulled year of data, paying its real per-bar spread. That is a real, modest edge as far as any harness in this repo can
tell. But it is also a *hypothesis*: the late-trend gate, the 3R target and the confidence repair
were all chosen on this same year, and they have **7 live trades** behind them.

So the position you are actually in is: an unproven engine, a $1,181 account, and a risk setting
that turns its own measured drawdown into −74%. At 1% risk the whole apparatus is worth a few
dollars a week — which is the correct scale for something with 7 trades of evidence. The value of
the next few months is the **sample**, not the money.

Two rules that follow, and they are the entire recommendation:

- **Do not size up until the forward sample exists.** Sizing up ahead of evidence is precisely what
  turned a −20R book into a −55% account.
- **Do not add a filter on fewer than ~1,000 trades.** §4 of this document is a worked example of
  how convincing a false one can look: stable across both halves, p = 0.0017, broad across 21
  symbols, confirmed by a second method — and flatly inverted on the first large sample that saw it.

---

## Reproduce

From `backend/`, with the MT5 terminal running:

    python ../analysis/loss_audit/fetch_data.py <dir>/bars.pkl
    python ../analysis/loss_audit/odds_test.py <dir>    # §2 — odds.py predictiveness, pre-trade candles
    python ../analysis/loss_audit/target_test.py <dir>  # §4/§5 — candle replay at fixed R targets
    # §4/§5b — 3,093 trades, the live filter set
    python ../analysis/loss_audit/bt.py --data <dir>/bars.pkl --symbols <33 syms> --bars 6500 --mode faithful --out <dir>/live.json --disable minrr,trend_slope,range_breakout,wall,ema_pullback,adx_rising
    python ../analysis/loss_audit/analyze.py <dir> <dir>/live.json
    # §3 — the same run with minrr ENABLED, to price what that toggle is worth
    python ../analysis/loss_audit/bt.py --data <dir>/bars.pkl --symbols <33 syms> --bars 6500 --mode faithful --out <dir>/minrr_on.json --disable trend_slope,range_breakout,wall,ema_pullback,adx_rising

Both new scripts take the directory holding `bars.pkl` as their first argument.
The journal queries in §0–§2 run directly against `backend/trading.sqlite3` (`positions`,
`agent_runs`). The MT5 server-clock check in §4 is `mt5.symbol_info_tick(...).time` vs
`datetime.now(timezone.utc)`.
