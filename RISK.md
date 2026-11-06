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
- Ceiling: the documented hard cap is 2%. **Override (2026-06-17):** raised to **3%** at the
  user's explicit request, AND the default per-trade risk raised from 1% to **3%** (so automatic
  trades now risk 3%, not 1%). That is a +50% bigger single-trade loss than the 2% cap and 3× the
  1% default; a sustained losing streak now bites much faster. The conservative values (1% default
  / 2% ceiling) are the documented recommendation — consider returning to them once the strategy
  is proven.
- **Scalp-mode ceiling 6% (2026-11-02, at the user's explicit request, after the current values were
  restated):** the scalp ticket's orders (manual, tagged `scalp`) and the user's custom strategies' orders
  (source `strategy:<id>`) may risk up to **6%** of equity per trade. That is 2x the 3% ceiling and 3x the
  documented 2% cap. Every other order (normal manual tickets, the Hybrid, the auto-traders, armed setups,
  approvals) keeps the 3% ceiling. The ticket's presets keep their defaults (0.5 / 1 / 2%), each adjustable up
  to 6%; a strategy's risk per trade on the board goes up to 6%. Unchanged: the 9% total exposure cap (so one
  6% trade leaves room for only 3% more), the minimum-lot bound (2x the trade's own risk - up to 12% on a
  min-lot trade at 6%), and every other gate. (`SCALP_RISK_CEILING` / `scalp_risk_ceiling` in
  `app/risk/service.py`, applied in the ticket's routes, the size preview, the strategy engine and the
  executor's last check.)
- **Manual size (added 2026-06-05):** before approving a Mode-A trade you may adjust the lot
  size up or down (Proposal panel shows the spend/margin + leverage live). Any chosen size is
  re-run through the deterministic Risk Manager and **hard-clamped to the 3% ceiling above** —
  you can size up to the cap, never past it. The ceiling remains the one place sizing is bounded.
- **Broker-minimum-lot exception (2026-06-23, at the user's explicit request):** when the 3%-budget
  size is SMALLER than the broker's minimum tradable lot (e.g. 0.01), the trade is opened at that
  minimum even though it risks MORE than 3% — because you cannot trade smaller. It is bounded by the
  minimum lot ("small money"), flagged to the user (`min_lot_floored`, an amber "⚠ broker min · over
  cap" note + the exact % in the verdict), and is the ONLY case sizing may exceed the per-trade cap.
  The **portfolio exposure cap is NOT waived**: a min-lot trade must still fit the remaining
  `max_total_exposure` budget (vetoed if even the minimum won't fit), so several min-lot trades can't
  stack past the budget. Every other gate (daily-loss, max positions, anti-stacking, correlation,
  kill-switch, live-confirmation) still applies. This replaces the previous behaviour, which either
  vetoed the trade or let the broker silently clamp a sub-minimum size up at order time (a hidden
  over-risk).
  **Bounded (2026-10-13, at the user's explicit request):** the minimum-lot trade may risk at most
  **2× the per-trade risk**; beyond that it is VETOED ("the account is too small for this symbol at
  this stop"). On a small real account the minimum lot can be many times the budget (gold on $1,000
  at 0.5% risk = 2.75% per trade, 5.5×), and the auto-openers were taking those unattended.
  (`_MIN_LOT_MAX_MULTIPLE` in `app/risk/manager.py`.)
- **Manual stop moves (2026-10-13; loosened 2026-10-14 at the user's explicit request):** on the
  chart / positions table the user may move SL and TP anywhere, including widening past the per-trade
  risk — the app shows a warning with the new risk % instead of refusing. Still refused: REMOVING the
  stop outright (a typed 0 must not leave a live trade unprotected) and a level on the wrong side of
  price. Every change is logged and mirrored onto the app's rows, so the exposure budget for NEW
  trades still sees the real (widened) risk.

### `max_open_positions` (default: 3)
Caps how many trades can be open at once.
- Why: correlated positions (e.g. several USD pairs) can all move against you together,
  so "3 trades" can really be one big bet. Keeping this small limits hidden concentration.
- **No-stacking rule (added 2026-06-09):** the Risk Manager now also vetoes a second trade in
  the *same symbol and same direction* while one is already open, and the executor blocks it as
  a final gate. (Three BTC shorts opened within 11 minutes turned one wrong call into a tripled
  loss; this prevents that pile-up.)
- **Correlation rule (added 2026-06-10):** the Risk Manager also vetoes a trade that would be a
  3rd *correlated* bet on one risk factor — each trade is mapped to its signed factors (each
  currency, plus crypto / equity-index / metal / energy blocs), and a new trade is refused when
  the open book already nets 2 positions the same way on a shared factor (e.g. short EURUSD +
  short GBPUSD are both "long USD"; a 3rd long-USD trade is blocked). Offsetting trades net down,
  so they're allowed. See `app/risk/correlation.py`.

### Stop placement (engine)
- Protective stop = entry ± an ATR multiple: **1.5×ATR** for forex/metals/indices/stocks/energy,
  **2.5×ATR for crypto** (crypto is far more volatile — a tight stop just gets wicked out).
- An **anti-wick floor (added 2026-06-09)** keeps the stop at least ~1×ATR from entry: the
  structure-tightening that snaps the stop to a nearby level can no longer pull it inside that
  floor (a 0.2% stop on BTC was being hit by normal noise within minutes).
- Note: a wider stop does **not** add dollar risk — position size is derived from
  `risk_per_trade ÷ stop distance`, so a wider stop just means a smaller position at the same %.

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
- **Override (2026-06-05):** raised to **9%** (from 6%, a +50% increase) at the user's explicit
  request. At 9% the combined risk across open trades can take a larger bite at once; the 6%
  default is the documented recommendation. Per-trade (2% ceiling) and daily-loss caps unchanged.
  Consider returning to 6% once the strategy is proven.
- **Override (2026-11-02, at the user's explicit request, confirmed after the restatement):** the
  ceiling is now **15%** (from 9%), and the user's setting was raised to 15%. The user can change it in
  Settings › Max exposure, anywhere from 1 to 15%; the server rejects anything above 15%
  (`_EXPOSURE_CEILING` in `app/api/settings_routes.py`).
  - At 15%, all open trades together can lose 15% of equity if every stop is hit: about $110 on that
    day's $734.68. At the strategy's 4.5% a trade, that is three trades open at once instead of two.
  - It covers every order (manual, strategies, the auto-traders) and a live account too.
  - Unchanged: the per-trade ceilings (3%; 6% for scalp and strategy orders), the 2x min-lot bound and
    every other gate.
  - The daily-loss breaker was OFF when this was set, so nothing pauses new trades after a bad run.
    Turn it back on before trading the live account, and consider returning to 6-9% once the strategy
    is proven.

### `per_pair_cooldown` (default: 30 min after a closed trade on that pair)
Prevents immediately re-entering the same pair after a stop-out.
- Why: re-entries right after a loss are usually emotional, not analytical.
- **Scalp-mode override (2026-11-01, at the user's explicit request):** hand-placed orders from the
  chart's scalp ticket (source `manual`, tagged `scalp`) skip this cooldown entirely (0 min), so a
  scalper can re-enter a pair right after closing it. Every other order — the Hybrid, the auto-traders,
  armed setups, approvals, ordinary manual tickets — keeps the 30 min. Nothing else is waived for
  scalps: per-trade risk and its ceiling, exposure, max positions, no-stacking, correlation, the spread
  gate, the daily-loss pause (when its breaker is on), the kill-switch and the live confirmation all
  still apply. (`scalp_cooldowns` in `app/risk/service.py`.) The scalp ticket's own daily stop (2 / 4 / 6%
  by preset) locks its buttons until 00:00 UTC, but it is enforced in the browser only — turn the
  `daily_loss_breaker_enabled` switch back on for a backend-enforced daily limit.
  **Adjustable and switchable (2026-11-01, at the user's explicit request):** next to the ticket's Day meter
  the user can change each preset's daily-stop % (0.5-30% of the day's starting equity; defaults stay
  2 / 4 / 6%) and switch the daily stop OFF (default ON). With it off the meter still shows the day's loss,
  but nothing stops manual scalping on a bad day. It counts the whole account's realized P&L for the day,
  the user's strategies included. (`dailyStop` in `frontend/src/scalp.ts`, `scalp.dailyStopOn` /
  `scalp.dailyStopPct` in the browser.)
- **Custom-strategy override (2026-11-01, at the user's explicit request):** orders from the user's own
  strategies on the Strategies board (source `strategy:<id>`) get the same 0-min per-pair cooldown as
  scalp orders. The strategies are paper/demo-only unless their live switch is on (below), capped at
  their own max trades a day, and every other gate above still applies. (`scalp_cooldowns` in
  `app/risk/service.py`.)
  **Live account (2026-11-04, at the user's explicit request after the restatement):** a strategy may
  trade a REAL account when its "Trade the live account" switch is on (`StrategyConfig.live`, board:
  "Live account: trades it too"). It is OFF by default, so a new strategy is demo-only. On a real
  account it trades exactly as on the demo - the same rules, risk per trade, cooldowns and caps; the
  user asked for "the same as the demo, exactly". Restated before the yes: strategy 1 risks 5% a trade
  (~$41 on the real account's $815.64), up to 180 trades a day on 9 pairs, no backtest, the daily-loss
  breaker off, and no human approval per order - RISK.md's Mode C advice (backtest and a long paper
  run first) was given and set aside by the user. Still enforced on every real-money order: the typed
  live confirmation (re-typed after every restart; without it the strategy opens nothing and says so),
  the kill-switch, the news gate, the 6% strategy ceiling, exposure, max positions and no-stacking.
  Switched on for strategy 1 the same day, at the user's request.
  **Per strategy (2026-11-01, at the user's request):** each strategy sets its own cooldowns on the
  board - re-entry after any close 0-240 min, same-direction wait after a loss 5-360 min - with the
  confirmed values as the FLOORS: never under 0 / 5 min, enforced in the API and again in
  `strategy_cooldowns` (`app/risk/service.py`). Raising them is always allowed; going under them is a
  change to this file.

### `loss_cooldown` (default: 180 min after a STOP-OUT, same pair + same direction)
A longer cooldown that triggers only after a LOSS: once the most recent closed trade on a
symbol+direction was stopped out, that direction is blocked for `loss_cooldown_minutes`. The
opposite direction is NOT blocked (a failed/reclaimed break often reverses); a later win resets it.
- Why: re-entering a just-failed setup is how one stop becomes three (XAGGBPm re-shorted a failed
  break of 47 three times in a session). The 30-min per-pair cooldown is too short to catch
  re-entries that are hours apart.
- **Scalp-mode override (2026-11-01, at the user's explicit request):** for scalp-tagged manual orders
  this is a **5-min** pause instead of 180 min — after a losing scalp, the same pair in the same
  direction waits 5 minutes; the other direction and other pairs are open at once. A deliberately
  short anti-tilt pause, kept rather than removed (the 30-min per-pair cooldown above is removed for
  scalps). Custom-strategy orders (source `strategy:<id>`) get the same 5-min pause since
  2026-11-01, at the user's explicit request: it is what stops a strategy whose rules still hold
  after a stop-out from re-entering on the very next 5s candle. All other orders keep the 180 min.

### `execution_mode` (default: A — Propose & Approve)
A = AI proposes, you approve. B = auto-execute paper only. C = auto-execute live.
- Stay on A or B for months. Do not touch C until backtests AND a long paper run look sane.
- Mode C is real money with no human in the loop. The warning banner is there on purpose.

### Hybrid auto-pilot (added 2026-06-09)
A separate one-button toggle (Opportunities panel). When ON, a tick every ~35 min (configurable
30–90) opens **at most one** trade per cycle: only if open positions < `max_open_positions`, only
the single best watchlist setup, and only when its confidence exceeds the threshold (default
**70%**). It re-runs the full analysis (LLM review can still veto) before opening.
- **Adjustable in the UI (added 2026-06-10):** a ⚙ Settings editor on the Hybrid panel lets you
  change the **check interval (clamped 30–90 min)** and the **confidence threshold (clamped
  50–95%; default still 70%)**. The threshold only governs *which* setups qualify for auto-open —
  it is **not** a money cap. Every dollar limit (≤2% per trade, daily-loss, exposure, position
  count, no-stacking, correlation) is enforced in the executor regardless of the threshold, so
  lowering it cannot enlarge any single or aggregate bet (per-trade cap now 3% — see override
  above); it only lets Hybrid act on
  lower-conviction setups. The UI shows a warning when it is set below the 70% default.
- It does **not** bypass anything: kill-switch, live-confirmation, daily-loss pause, exposure
  budget, per-pair cooldown, and no-stacking are all enforced in the executor exactly as for a
  manual approval. On a LIVE account it is blocked unless live trading is confirmed (same gate as
  Mode C). Off by default.

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
