"""Measured odds for a planned trade: how often did THIS symbol reach this target before this stop?

The engine's confidence score says how well a setup ticks its checklist. It never asks the one
question that decides a trade: given where the stop and target actually sit, which gets touched
first? A 1.4:1 plan whose stop is closer than its target can still be a coin flip, and the honest
way to know is to count it on the symbol's own history rather than argue from the chart.

So: take every past bar where the market was in the same kind of tape (same trend side), put the
same two barriers around it at the same PERCENTAGE distances, and walk forward to see which was hit
first. The result is a base rate, calibrated by construction — no model, no fitting, just counting.

It is a READ-OUT, not a gate: nothing here blocks a trade. Sample overlap (the same swing counted by
neighbouring bars) makes the percentages directional rather than precise, which is why ``samples`` is
always reported next to them.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.models.schemas import Candle

log = get_logger("agents.odds")

_HORIZON = 96      # bars to give the trade — the same time-stop the backtest uses
_MIN_SAMPLES = 60  # below this the base rate is noise; report nothing
_MAX_SAMPLES = 1500  # enough to settle a percentage; stop counting there (this runs on every analysis)
_STRIDE = 2        # neighbouring bars measure almost the same swing, so every other one is plenty


def _ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    out: list[float] = []
    cur = values[0]
    for v in values:
        cur = v * k + cur * (1 - k)
        out.append(cur)
    return out


def barrier_odds(candles: list[Candle], *, direction: str, entry: float, stop: float,
                 target: float, horizon: int = _HORIZON) -> dict | None:
    """``{p_target, p_stop, samples, horizon, edge_r}`` or None when there isn't enough history.

    ``edge_r`` is the expectancy those odds imply, in R: p_target * (reward/risk) − p_stop.
    """
    if len(candles) < 300 or not entry or not stop or not target:
        return None
    is_long = direction == "long"
    risk = (entry - stop) if is_long else (stop - entry)
    reward = (target - entry) if is_long else (entry - target)
    if risk <= 0 or reward <= 0:
        return None
    up_frac = (risk if not is_long else reward) / entry      # distance to the barrier ABOVE price
    dn_frac = (reward if not is_long else risk) / entry       # ...and BELOW it
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    hit_t = hit_s = 0
    for i in range(60, len(candles) - horizon, _STRIDE):
        if hit_t + hit_s >= _MAX_SAMPLES:
            break
        # Same kind of tape: the trade's own trend side (EMA20 vs EMA50), so a long is judged on
        # rising markets and a short on falling ones rather than on the average of both.
        if is_long and not e20[i] > e50[i]:
            continue
        if not is_long and not e20[i] < e50[i]:
            continue
        ref = closes[i]
        up, dn = ref * (1 + up_frac), ref * (1 - dn_frac)
        for j in range(i + 1, i + horizon + 1):
            up_hit, dn_hit = highs[j] >= up, lows[j] <= dn
            if up_hit and dn_hit:
                break                      # same bar: unknowable order, don't guess
            if up_hit:
                hit_t += is_long
                hit_s += not is_long
                break
            if dn_hit:
                hit_t += not is_long
                hit_s += is_long
                break
    n = hit_t + hit_s
    if n < _MIN_SAMPLES:
        return None
    p_t = hit_t / n
    return {"p_target": round(p_t, 3), "p_stop": round(1 - p_t, 3), "samples": n,
            "horizon": horizon, "edge_r": round(p_t * (reward / risk) - (1 - p_t), 3)}


def odds_for_proposal(broker, proposal, timeframe: str, *, bars: int = 2000) -> dict | None:
    """Base rate for a proposal, measured on a long window of its own symbol/timeframe.

    Best-effort: any data problem returns None rather than delaying an analysis."""
    if proposal is None or proposal.direction.value not in ("long", "short"):
        return None
    if not (proposal.entry and proposal.stop_loss and proposal.take_profit):
        return None
    # The sim broker serves a synthetic random walk. A base rate counted on invented prices is noise
    # dressed up as evidence, so report nothing rather than something meaningless.
    if getattr(broker, "name", "") == "sim":
        return None
    try:
        from app.data.ohlcv_cache import get_ohlcv_cached

        series = get_ohlcv_cached(broker, proposal.symbol, timeframe, limit=bars, ttl=900.0)
        return barrier_odds(list(series.candles or []), direction=proposal.direction.value,
                            entry=float(proposal.entry), stop=float(proposal.stop_loss),
                            target=float(proposal.take_profit))
    except Exception as exc:  # noqa: BLE001 - a read-out must never break the analysis
        log.warning("odds unavailable", extra={"symbol": proposal.symbol, "error": str(exc)})
        return None
