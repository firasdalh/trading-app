# Task 12 — Structured decision logging

## Goal
Answer **"why didn't it fire on this setup?"** after the fact, without re-reading code — for every
funnel evaluation, including the `no trade` ones.

## What's logged
Every `analyze_symbol` evaluation writes a row to the existing queryable **`AgentRun`** table with
`agent="decision"` (no migration needed). Each row's JSON `detail` carries:

- `direction` + `actionable` (did it produce a trade?)
- **`gate`** — the deciding gate, as a stable tag you can filter on:
  `approved`, `ai_veto`, `risk_veto`, `armed_wait`, `regime_not_trending`, `mtf_conflict`,
  `structure_conflict`, `volatility`, `divergence`, `no_trend`, `thin_rr`, `ranging_fade`,
  `st_band*`, `pullback_wait`, `no_trade_other`
- `regime`, `strategy`, `confidence`
- **`indicators`** at that moment (ADX, ±DI, RSI, MACD hist, ATR, EMA20/50/200, SuperTrend dir, structure)
- **`review_decision`** — the AI reviewer's confirm/veto verdict (when the LLM ran)
- `risk_decision` + `risk_reason` — the deterministic Risk Manager's call
- the full human-readable `rationale`, and the armed `conditional` order type if any

## How to query
- **API:** `GET /api/decisions?symbol=EURUSDm&limit=100` — recent evaluations for a symbol;
  `GET /api/decisions?gate=regime_not_trending` — everything blocked by a given gate.
- **SQL:** `SELECT * FROM agent_runs WHERE agent='decision' ORDER BY id DESC` — JSON `detail` holds
  the fields above.

## Example workflow
"I expected EURUSD to fire this morning and it didn't" →
`GET /api/decisions?symbol=EURUSDm` → newest row shows `gate: "mtf_conflict"`,
`rationale: "No confluence: higher-timeframe trend is DOWN"`, plus the exact ADX/RSI/MACD/EMA values —
so you see it stood aside because the 4h/1d trend opposed the setup, not a bug.

## Scope
- Wired into `analyze_symbol` (deliberate evaluations: Run-analysis, the Hybrid's chosen symbol,
  conditional re-checks). High-frequency ranking previews are intentionally NOT logged to avoid
  flooding the table; if you want full-scan coverage, that's a one-line add behind a flag.
- Purely observational — it changes no trading behavior; safe on the live path.
- Tests: `tests/test_decision_log.py` (gate classification for approvals/vetoes/no-trade + a
  persisted-row check).
