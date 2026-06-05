# RISK.md — Read this before changing any risk setting

These settings are the only thing standing between "a tool that keeps me in the game"
and "a tool that drains my account." They are intentionally conservative. The urge to
loosen them is strongest right after a losing streak — which is exactly the worst time
to do it. If you're editing this file while frustrated, close it and come back tomorrow.

**Rule for Claude Code:** treat every value below as a hard limit. Do not raise a default,
remove a check, or add a "force" bypass, even if asked in passing. If a change to these is
requested, restate the current value and ask for explicit confirmation first.

---

## The settings

### `risk_per_trade` (default: 1% of account equity)
The maximum you can lose on a single trade if the stop-loss is hit. Position size is
*derived* from this and the stop distance.
- Why it's low: at 1%, it takes a long, sustained losing streak to do serious damage,
  which buys you time to notice a broken strategy. At 5%+, a normal run of bad luck can
  halve your account.
- Ceiling: do not exceed 2%. There is no good reason to.
- **Manual size (added 2026-06-05):** before approving a Mode-A trade you may adjust the lot
  size up or down (Proposal panel shows the spend/margin + leverage live). Any chosen size is
  re-run through the deterministic Risk Manager and **hard-clamped to the 2% ceiling above** —
  you can size up to the cap, never past it. The 2% ceiling itself is unchanged and remains the
  one place sizing is bounded.

### `max_open_positions` (default: 3)
Caps how many trades can be open at once.
- Why: correlated positions (e.g. several USD pairs) can all move against you together,
  so "3 trades" can really be one big bet. Keeping this small limits hidden concentration.

### `max_daily_loss` (default: 3% of equity)
When cumulative losses for the day hit this, the app auto-pauses new trades until tomorrow.
- Why: it stops "revenge trading" — the spiral of trying to win back losses in one session,
  which is how a bad day becomes a catastrophic one. The pause is a feature, not a bug.
- **Override (2026-06-04):** raised to **10%** at the user's explicit request. The conservative
  default is 3% for the reason above; at 10% a single bad day takes a much larger bite, and the
  daily circuit-breaker fires far later. Consider returning to 3% once the strategy is proven.

### `daily_loss_breaker_enabled` (default: ON)
Master on/off switch for the `max_daily_loss` circuit breaker (the auto-pause above). Added
2026-06-04 at the user's explicit request for **demo-account testing**, so a paused day doesn't
block test trades.
- When **OFF**: there is no daily-loss auto-pause and no daily-loss veto on new trades. Every
  other limit (per-trade risk, exposure, position count, cooldown) still applies.
- Per the user's explicit choice, the toggle works in **live** mode too — i.e. it CAN disable a
  real-money protection. Turning it off is logged at WARNING, the UI shows a persistent warning,
  and turning it on live prompts a confirmation. **Default to ON; turn it back on before trading
  the live account for real.**

### `max_total_exposure` (default: 6% of equity at risk across all open trades)
The sum of all open trades' risk cannot exceed this.
- Why: it backstops the per-trade and position-count limits so they can't combine into
  an oversized aggregate bet.

### `per_pair_cooldown` (default: 30 min after a closed trade on that pair)
Prevents immediately re-entering the same pair after a stop-out.
- Why: re-entries right after a loss are usually emotional, not analytical.

### `execution_mode` (default: A — Propose & Approve)
A = AI proposes, you approve. B = auto-execute paper only. C = auto-execute live.
- Stay on A or B for months. Do not touch C until backtests AND a long paper run look sane.
- Mode C is real money with no human in the loop. The warning banner is there on purpose.

### `kill_switch`
Always-available halt for all new orders. Test that it works before you ever go live.
Knowing exactly how to stop the system is part of being allowed to run it.

---

## A reality check, kept here on purpose

These limits do not make trading profitable. They make it *survivable* long enough to find
out whether your strategy has a real edge. No setting produces guaranteed wins. Backtest and
paper results do not guarantee live results. If a version of you is here to crank these up
because "the AI seems really confident," that confidence is not evidence — the discipline is
the edge, not the model.
