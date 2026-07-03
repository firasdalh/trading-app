# SPEC — Deterministic Trading Funnel (as-built)

> Source of truth for the **deterministic** signal engine as currently implemented. This documents
> existing behavior; it does **not** propose changes. Primary code:
> [`backend/app/agents/orchestrator.py`](backend/app/agents/orchestrator.py) `_deterministic_decision`,
> indicators in [`backend/app/agents/technical.py`](backend/app/agents/technical.py) and
> [`backend/app/agents/indicators.py`](backend/app/agents/indicators.py).
> Ambiguities / inconsistencies found while writing this are in [`SPEC-QUESTIONS.md`](SPEC-QUESTIONS.md).

The deterministic engine turns a multi-timeframe technical read into a `TradeProposal`
(`direction`, `entry`, `stop_loss`, `take_profit`, `confidence`, `rationale`, optional armed
`conditional`). Same inputs → same output (no randomness, no LLM). It is a **funnel of hard gates**;
the default answer is `NO_TRADE`. An AI review layer may then confirm/veto (separate, downstream —
out of scope for this spec).

---

## 1. Inputs (computed per timeframe in `technical.py`)

For each timeframe the technical layer computes and stores in `indicators`:

| Key | Meaning |
|---|---|
| `ema20`, `ema50`, `ema200` | Trend EMAs |
| `ema20_high`, `ema20_low` | EMA20 of highs / of lows (band) |
| `adx`, `plus_di`, `minus_di` | Trend strength + directional index |
| `macd_hist` | MACD histogram (momentum) |
| `rsi14`, `rsi14_prev` | Wilder RSI + prior value |
| `atr14` | Average True Range (volatility / sizing) |
| `vol_atr_ratio` | recent ATR ÷ longer ATR baseline (vol expansion) |
| `vol_ratio` | volume vs baseline (participation) |
| `structure` (1/‑1/0), `swing_high`, `swing_low`, `choch` | Market structure + change-of-character |
| `recent_high`, `recent_low` | recent wick extremes (anti stop-hunt) |
| `last_close`, `last_high`, `last_low` | latest bar |
| `div_bull`, `div_bear`, `div_bull_hidden`, `div_bear_hidden` | RSI divergence flags |
| `support_levels`, `resistance_levels` | pivot/structure levels (on `TimeframeRead`) |
| `supertrend`, `supertrend_dir`, `supertrend_bars_since_flip` | SuperTrend (chart + st_band mode) |

The **entry timeframe** is selected by matching the requested `timeframe` (not position 0).
The **macro (higher-TF) trend** = the highest-ranked timeframe present (`_TF_RANK`:
1m<5m<15m<30m<1h<4h<1d).

---

## 2. Trend derivation

`_trend_from_indicators` (numbers, not any LLM label):
- `up` if `ema20 > ema50 > ema200` (or `ema20 > ema50` when ema200 absent)
- `down` if `ema20 < ema50 < ema200` (or `ema20 < ema50`)
- else `sideways` (fallback to the read's label)

`_macro_trend` applies the same to the highest timeframe.

## 3. Regime (`_regime`) — read FIRST

| Regime | Condition | Strategy |
|---|---|---|
| `trending` | `adx ≥ 25` (`_ADX_STRONG`) | trend continuation |
| `volatile` | not trending **and** `vol_atr_ratio ≥ 1.6` (`_REGIME_VOL_EXPANSION`) | stand aside (whipsaw) |
| `ranging` | not trending/volatile **and** `adx < 20` (`_ADX_MIN`) | mean-reversion fade |
| `moderate` | otherwise (20 ≤ adx < 25) | trend continuation (weaker) |

---

## 4. Gate order (`_deterministic_decision`)

```
0.  Event blackout: inside a high-impact event window -> NO_TRADE ("standing aside").
1.  Regime read (section 3). Sets base.regime / base.strategy.
        - st_band mode  -> _supertrend_band_decision (section 8) and RETURN.
        - scalp mode    -> _scalp_decision (section 7) and RETURN.
        - trend_only AND regime != trending -> stand aside (watch). [live default]
        - regime == ranging -> _mean_reversion_decision (section 6) and RETURN.
        - adx < _ADX_MIN and volatility expanding -> NO_TRADE (whipsaw).
2.  DIRECTION from the EMA trend:
        trend == up   -> LONG   (see momentum-pullback + MTF gates below)
        trend == down -> SHORT
        trend == sideways -> NO_TRADE.
    2a. Momentum-pullback: trend up but macd_hist < -(0.10*ATR)  (`_MOM_ATR_FRAC`) =
        a pullback. Do NOT enter; set watch=True and ARM a resumption break
        (_conditional_resumption) to fire when momentum turns back. (Mirror for down.)
    2b. Higher-TF conflict: macro trend opposes the direction -> NO_TRADE ("no confluence").
3.  STRUCTURE confluence: if EMA trend says one way but swing structure (entry TF AND macro TF)
    says the other -> watch (early reversal / chop), NO market entry.
4.  Volatility gate #2: regime volatile AND vol_atr_ratio >= 2.2 (`_REGIME_VOL_EXTREME`) -> watch.
5.  Entry = last_close. Overextension + divergence checks:
        - overextended = entry > ema20 + 2.5*ATR (long) (`_PULLBACK_ATR`)  -> down-weight (soft).
        - div AGAINST the trade AND overextended -> watch (exhaustion), NO entry.
6.  STOP (section 5a).  Skip if risk <= 0.
7.  TARGET (section 5b) from real key levels; decide take_market (skip a <1.5R market entry).
8.  CONFIDENCE (section 5c).  Attach an armed `conditional` alternative regardless.
9.  Thin direct R:R (only level < _MIN_RR_ENTRY away, non-strong trend) -> keep NO_TRADE/watch
    carrying the armed alternative rather than chasing ~1:1 at market.
```

### 5a. Protective stop
Stop is placed where the trade is **invalidated**, preferring structure:
1. Base ATR stop: `entry ∓ 1.5×ATR` (`_ATR_STOP_MULT`; crypto `2.5×` `_ATR_STOP_MULT_CRYPTO`).
2. Tighten to nearby support/resistance only if it stays **≥ 1×ATR** away (`_MIN_STOP_ATR_FRAC`, anti-wick floor).
3. Structural stop: just beyond the last swing (`swing_low`/`swing_high`) + `0.2×ATR` buffer
   (`_STRUCT_STOP_BUFFER_ATR`), extended beyond `recent_low`/`recent_high` wicks if those already
   pierced the swing (anti stop-hunt) — used only if within `3×ATR` (`_STRUCT_STOP_MAX_ATR`), else the
   ATR stop stands. Never tighter than the 1×ATR floor.

`risk = |entry − stop|`.

### 5b. Target (`_key_levels`: pivots, prior day/week H-L, round numbers)
- Baseline `_RR = 2.0`; hard cap `_RR_MAX = 4.0` (`cap = entry ± 4R`).
- Levels within `0.5R` of entry (`_STRUCT_IGNORE`) are treated as inside the breakout zone (ignored).
- If the nearest meaningful level is **≥ 2R** → target it (a **strong** trend, adx ≥ 25, may run to the
  next level up to the cap).
- If a level sits **before 2R**:
  - strong trend → aim through it to the next ≥2R level (else 2R);
  - moderate trend, level ≥ `_MIN_RR_ENTRY` (1.5R) → cap the target there;
  - level < 1.5R and not strong → **do not take at market** (`take_market=False`); stand aside and arm
    the better-priced alternative.
- No level in range → fixed 2R.

### 5c. Confidence formula (trend engine)
Start `conf = 0.30 + 0.20·tech.confidence + 0.15·fund.confidence`, then add/subtract:

| Factor | Δconf |
|---|---|
| Fundamental bias agrees / opposes direction | +0.05 / −0.05 |
| Macro (higher-TF) trend aligned | +0.15 |
| ADX ≥ 25 (strong) | +0.10 |
| `vol_ratio > 1.2` (participation) | +0.10 |
| MACD hist aligned with direction | +0.05 |
| Higher-TF MACD conflicts | −0.10 |
| Entry AT value (≤1×ATR from EMA20, `_VALUE_ENTRY_ATR`) | +0.10 |
| Entry stretched (≥1.5×ATR, `_STRETCHED_ATR`) / chased (≥2.5×ATR, `_PULLBACK_ATR`) | −0.06 / −0.18 |
| RSI overbought/oversold at entry (`_RSI_OB`75 / `_RSI_OS`25) | −0.10 |
| Entry on the right side of EMA200 | +0.05 / −0.05 |
| Structure aligned / against (when not range) | +0.10 / −0.10 |
| Change-of-character (`choch`) | −0.10 |
| RSI divergence against / hidden-with | −0.12 / +0.07 |
| Volatile regime | −0.10 |
| Session active / thin | +0.05 / −0.05 |

Final: `confidence = clamp(conf, 0.05, 0.95)`.

### 5d. Armed conditional carry
Computed regardless of the market decision (survives an LLM veto or a thin-R:R sit-out):
- a **break-STOP** past a blocking level (`_conditional_break`, R:R ≥ `_MIN_RR_COND` 1.5), else
- a **pullback LIMIT** back at value (~EMA20) when the entry isn't already at value (`_conditional_pullback`), or
- a **resumption STOP** during a momentum pullback (`_conditional_resumption`, step 2a).

Armed-order lifecycle: [`conditional.py`](backend/app/agents/conditional.py) — time expiry
(`valid_until`, default 12h `_DEFAULT_VALID_HOURS`), **pre-trigger trend-EMA(50) invalidation**
(`_trend_broken`, added by Task 5), trigger-time mechanical invalidation (`_mechanical_invalidation`),
then AI re-check + Risk Manager on the armed levels.

---

## 6. Mean-reversion mode (`regime == ranging`) — summary
Fade the range **edge** back to the mean: within `0.6×ATR` of a range edge (`_MR_EDGE_ATR`), stop
`0.6×ATR` beyond the edge (`_MR_STOP_ATR`), need ≥ `1.0R` back to the mean (`_MR_MIN_RR`), looser RSI
band `34/66` (`_MR_RSI_OS`/`_MR_RSI_OB`), confidence capped at `0.68` (`_MR_CONF_CAP`). Refuses a fade
that opposes the higher-TF trend (that's a pullback, not a range).

## 7. Scalp mode (15m, `_scalp_decision`) — summary
Single setup: trend-pullback continuation. Hard gates: regime ∈ {trending, moderate} and a liquid
session (`_session_quality`). Enter when price pulled back into the value zone (EMA50…EMA20+`0.30×ATR`
`_SCALP_VALUE_ATR`) with RSI turning back with the trend (not yet OB/OS). Stop beyond the pullback
extreme (floor 1×ATR, cap `2×ATR` `_SCALP_STOP_MAX_ATR`); target nearest opposing level ≥ `1.3R`
(`_SCALP_MIN_RR`) else fixed `1.5R` (`_SCALP_TP_FIXED_RR`). Confidence base 0.5 + small bonuses,
capped 0.9.

## 8. SuperTrend-band mode (`st_band`, `_supertrend_band_decision`) — summary
Mechanical, no LLM: needs a **fresh** SuperTrend flip (`supertrend_bars_since_flip ≤ 3`), enters with
the SuperTrend direction on a close beyond the EMA20 band; stop beyond nearest S/R (buffer 0.2×ATR,
fallback 1.5×ATR); target opposing S/R (fallback 2R), requires R:R ≥ 1.5 (`_ST_BAND_MIN_RR`).

---

## 9. Modes & routing (`AppSettings`, mutually exclusive in `pipeline.py`)
- `trend_only_mode` (live default ON): only trade `trending`; stand aside otherwise.
- `ai_led_mode`, `scalp_mode`, `st_band_mode`: exclusive routing — `st_band` overrides `ai_led`+`scalp`;
  `scalp` overrides `ai_led`. When `st_band` is on, the AI review is skipped (mechanical).

## 10. Risk Manager (deterministic, never LLM) — boundary
The funnel only *proposes*. Sizing + hard gates live in [`risk/manager.py`](backend/app/risk/manager.py)
and [`risk/service.py`](backend/app/risk/service.py): per-trade risk cap, max positions, exposure,
per-pair cooldown, no-stacking, and the **daily-loss circuit breaker** (`evaluate_daily_pause`;
realized + floating after Task 4). This layer is out of the deterministic funnel's scope but is the
final authority on every trade.
