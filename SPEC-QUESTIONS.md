# SPEC-QUESTIONS — ambiguities & inconsistencies found

Places where the code's actual behavior is ambiguous, differs from how the task/brief described it,
or is internally inconsistent. None are changed here except where a task explicitly authorized it
(Tasks 4 & 5, noted below).

## Confirmed inconsistencies (found, some fixed under authorized tasks)

1. **Daily-loss breaker was realized-only despite its own docstring saying "realized + floating."**
   `evaluate_daily_pause` ([risk/service.py](backend/app/risk/service.py)) documented "the account's
   loss (realized today + floating)" but computed `drawdown = -realized` only. The inline comment
   even stated it deliberately excludes floating. So the docstring and the code disagreed.
   → **Resolved under Task 4** (now realized + floating). Flagging because it means the *documented*
   contract was never the *actual* behavior before this change.

2. **Armed orders "have no expiry/invalidation" (task premise) is only half true.** Armed conditionals
   already had a wall-clock expiry (`valid_until`, default 12h) and a *trigger-time* invalidation
   (`_mechanical_invalidation`). The real gap was **no pre-trigger price invalidation** while waiting.
   → **Resolved under Task 5** (added `_trend_broken`: close back through EMA50 against the setup).
   Question for you: is **EMA50** the right "trend-defining EMA"? The funnel defines trend by the
   EMA20/50/200 stack; EMA20 would over-trigger during a normal pullback (that's the setup itself),
   so I keyed off EMA50. Confirm, or specify (e.g., EMA20/EMA200, or the swing that anchored the pullback).

## Design ambiguities worth your call

3. **Breaker latches vs. dynamic (Task 4).** The breaker trips and **latches for the day** once
   realized+floating breaches the limit (never auto-unpauses; a new UTC day resets it). "In real time"
   could instead mean *dynamic* (unpause if floating recovers). I chose latch (standard circuit-breaker
   behavior, matches the existing "never auto-unpause" contract). A temporary floating spike past the
   limit therefore pauses NEW entries for the rest of the session even if the position later recovers.
   It does **not** close the open position (that stays managed by its stop/advisor). Confirm this is what
   you want, or switch to dynamic.

4. **Breaker order-time gate not floating-aware.** `evaluate_daily_pause` (called each monitor tick)
   now includes floating, and it sets `trading_paused`, which the order-time gate in
   [risk/manager.py](backend/app/risk/manager.py) respects via `account.trading_paused`. But the
   manager's *own* fallback check still uses `account.daily_realized_pnl` (realized-only). Between
   monitor ticks, a brand-new floating loss is caught only on the next tick, not at the instant of a
   new order. In practice this is fine (ticks are frequent and a new order first passes the per-trade
   3% cap), but if you want the manager gate itself to be floating-aware I can wire floating into the
   `account` snapshot it receives. **Not changed** (would touch the manager beyond Task 4's "only the
   breaker" scope).

5. **Confidence formula can saturate.** The trend `confidence` sums ~18 additive terms then clamps to
   `[0.05, 0.95]`. Several setups will hit the clamp, so the raw score is not linearly calibrated near
   the extremes. This is by design but worth noting for anyone using confidence as a probability.

6. **Two "volume" indicators with similar names.** `vol_ratio` (participation/volume) and
   `vol_atr_ratio` (volatility expansion) are distinct and both feed different gates. Easy to confuse.

7. **`trend_only_mode` interacts with `ranging`.** With `trend_only` ON (live default), the engine
   stands aside on every non-trending regime *before* the ranging mean-reversion branch — so the
   mean-reversion strategy never runs in the live default configuration. Intended? (It is reachable
   only with `trend_only` OFF.)

8. **`disable` gate-ablation switch is backtest-only.** `_deterministic_decision(..., disable=...)`
   can skip individual gates ("mtf", "momentum", "structure", "volatility", "divergence", "minrr") to
   measure each filter's contribution. The live path never passes it. Documented so no one wires it up
   by accident.

## Not inconsistencies, just noted
- The funnel and the AI review layer are cleanly separated (the funnel returns a `TradeProposal`; the
  reviewer only confirms/vetoes). No new coupling was introduced by Tasks 4/5.
- Risk Manager is deterministic and never an LLM — preserved.
