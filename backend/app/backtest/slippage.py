"""Context-dependent execution-cost (spread + slippage) model — BACKTEST / ANALYSIS SCOPE ONLY.

Turns the backtest's flat ``cost_r`` into a per-trade estimate that widens where real fills are
worse than a mid-price 'perfect fill':
  - near ROUND NUMBERS (spreads widen and stop clusters sit there),
  - near PRIOR HIGHS/LOWS (recent swing structure — liquidity air-pockets / stop clusters),
  - in THIN/illiquid sessions (wider quoted spread), tighter in the peak-liquidity window,
  - plus extra slippage when a STOP is hit (a market order crossing a moving book, vs a target
    that rests as a limit).

This is deliberately isolated from the live path and the deterministic funnel — it only re-scores
historical trades so we can compare realistic vs perfect-fill performance. It is NOT imported by the
orchestrator or the Risk Manager.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime

# All costs are expressed as fractions of ATR so they scale across instruments without per-symbol
# spread tables. Tunable in one place.
_BASE_SPREAD_ATR = 0.02   # typical ONE-WAY spread ~2% of ATR (a mid-liquidity default)
_ROUND_MULT = 1.6         # entry sitting on a round number
_LEVEL_MULT = 1.4         # entry sitting on a prior swing high/low
_THIN_MULT = 2.0          # thin/illiquid session -> quoted spread balloons
_ACTIVE_MULT = 0.8        # peak-liquidity session -> tighter
_STOP_SLIP_ATR = 0.03     # extra one-way slippage specifically on a STOP exit
_NEAR_ATR = 0.25          # "near a level" = within 0.25xATR of it


def _round_step(price: float) -> float:
    """A sensible round-number grid for the instrument's price scale, e.g. ~0.1 for EURUSD big
    figures, ~100 for gold, ~1000 for an index."""
    if price <= 0:
        return 0.0
    return 10 ** math.floor(math.log10(price)) / 10.0


def _near_round(price: float, atr: float) -> bool:
    step = _round_step(price)
    if step <= 0:
        return False
    nearest = round(price / step) * step
    return abs(price - nearest) < _NEAR_ATR * atr


def _session(asset_class, when: datetime) -> str:
    """Coarse liquidity session from the UTC hour (backtest cost proxy). 'active'/'normal'/'thin'."""
    ac = str(asset_class).lower()
    if "crypto" in ac:
        return "normal"                       # 24/7
    h = when.hour
    if 12 <= h < 16:                          # London-NY overlap
        return "active"
    if 7 <= h < 21:                           # London/NY hours
        return "normal"
    return "thin"                             # post-NY / pre-London


def spread_cost_r(*, entry: float, stop: float, atr: float | None, entry_time: datetime,
                  asset_class, outcome: str, prior_levels: Iterable[float] = ()) -> float:
    """Round-trip execution cost of one trade, in R (multiples of |entry - stop|).

    outcome: "stop" | "target" | "timeout" (stops pay extra slippage).
    prior_levels: nearby structural levels (e.g. the entry-TF swing high/low) — 'near a prior H/L'.
    Returns 0.0 when it can't be estimated (no ATR / non-positive risk).
    """
    risk = abs(entry - stop)
    if risk <= 0 or not atr or atr <= 0:
        return 0.0

    one_way = _BASE_SPREAD_ATR * atr
    mult = 1.0
    if _near_round(entry, atr):
        mult *= _ROUND_MULT
    for lv in prior_levels:
        if lv is not None and abs(entry - lv) < _NEAR_ATR * atr:
            mult *= _LEVEL_MULT
            break
    sess = _session(asset_class, entry_time)
    if sess == "thin":
        mult *= _THIN_MULT
    elif sess == "active":
        mult *= _ACTIVE_MULT

    entry_cost = one_way * mult
    exit_cost = one_way * mult
    if outcome == "stop":
        exit_cost += _STOP_SLIP_ATR * atr     # a stop crosses a moving market
    return (entry_cost + exit_cost) / risk
