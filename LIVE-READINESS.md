# LIVE-READINESS checklist — paper → live

A **checkable, measurable** gate before switching this desk from paper to live. Do not flip to live
until **every** box is ticked. Each item has a concrete threshold, not a vibe. Numbers in _italics_
are the defaults I recommend — adjust with intent, not convenience.

> Scope: this is a go/no-go checklist, not a code change. The live switch itself still requires the
> typed live-confirmation flow already in the app; this checklist is the human gate BEFORE that.

---

## A. Sample size & duration (is there enough evidence?)
- [ ] **≥ _50_ closed paper trades** on the exact config you intend to run live (same mode, same
  watchlist, same risk %). Fewer than this and the win-rate/expectancy are statistical noise.
- [ ] **≥ _6_ calendar weeks** of paper trading, spanning **at least 2 distinct market regimes**
  (e.g. a trending stretch AND a chop/range stretch), so the sample isn't one lucky regime.
- [ ] **≥ _15_ trades on each primary symbol** you'll trade live (per-symbol edge, not just a pooled
  average carried by one instrument — see the journal's per-symbol breakdown).

## B. Performance vs. expectation (does reality match the backtest?)
- [ ] **Live win rate within ±_10_ points** of the backtest win rate (Task 7 / journal calibration).
- [ ] **Live average R within ±_0.15R_** of the backtest expectancy. A persistent shortfall = an
  unmodeled cost (slippage/spread — see Task 8) or curve-fit; investigate before sizing up.
- [ ] **Observed max drawdown ≤ the Task 6 modeled band** for your chosen risk %:
  at **0.5%/trade**, live peak-to-trough drawdown should stay **well under ~16%** (the 95th-pct from
  `analysis/drawdown_simulation.md`). A live drawdown beyond the modeled 95th percentile is a red flag.
- [ ] **Confidence is calibrated**: in `/api/journal/calibration`, the 70-80% bucket actually wins
  roughly 70-80% (no wildly over-confident buckets).

## C. Risk configuration (are the guardrails set correctly?)
- [ ] **Risk-per-trade ≤ _0.5%_ to start** (Task 6: 0.25-0.5% is the safe band; **≥1% breaches the
  daily breaker routinely; 1.5-2% is statistically unsafe**). Do NOT start live at the paper risk %
  if it was higher.
- [ ] **Daily-loss breaker ENABLED** (`daily_loss_breaker_enabled = true`) and its limit set to the
  RISK.md value; verified it now counts **realized + floating** and auto-resumes on recovery (Task 4).
- [ ] **Max open positions & exposure caps** set to intended live values; per-pair cooldown on.
- [ ] **Kill-switch tested**: engage it, confirm no new orders open and (if used) flatten works.
- [ ] **Execution mode = A (propose → approve)** for the first live sessions — no auto-execute until
  live behavior matches paper.

## D. Data & operational integrity
- [ ] **Broker map correct** (each asset class → the real Exness/MT5 broker, never the sim) and MT5
  connection stable for a full session without dropouts.
- [ ] **Data-feed sanity checks green** (Task 10, once implemented): no recent rejected/stale candles
  or anomalous-range bars on the live symbols.
- [ ] **Journal baseline reset** at go-live (`POST /api/journal/reset`) so live stats start clean.
- [ ] **Live-confirmation flow rehearsed** on the demo/paper account (you know exactly what to type).

## E. Divergence watch (manual — Task 11 auto kill-switch was DEFERRED)
> The automatic performance-divergence kill-switch (Task 11) was skipped by request, so this is a
> **manual weekly review** until/unless it's built.
- [ ] **Weekly divergence review**: compare the rolling live win-rate and average R against the
  backtest band (B above). If either falls outside the band **two weeks running with no explained
  cause (news, regime, slippage), STOP and revert to paper.**
- [ ] **A written "why we're going live" note** on file: which edge, which symbols, which regime, and
  the single metric that would make you turn it off.

---

## Go / No-Go
**GO** only when A, B, C, D are fully ticked and E's review process is in place. Any single unticked
box in A-D = **NO-GO**. Start live at the smallest risk % and the fewest symbols that satisfy the
checklist, then scale only after the live sample re-confirms the edge.

_This checklist references: Task 4 (breaker), Task 6 (`analysis/drawdown_simulation.md`), Task 7
(`analysis/walk_forward.md`), Task 8 (slippage), Task 10 (data integrity). It intentionally does NOT
grant approval to trade live — it's the evidence bar you must clear first._
