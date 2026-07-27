"""Measure the entry circuit breakers against a backtest's historical trade stream.

Given the trades a backtest produced (``BTTrade`` list, all symbols merged), this replays them in
time order and asks, for each breaker: which trades would it have BLOCKED, and what would skipping
them have done to the result?

This is a NAIVE measurement — it evaluates each breaker against the ACTUAL historical sequence and
does not model the feedback loop (a blocked loss would have shortened a later streak). So read it as
"how often would this have tripped, and on what trades", not a re-simulated equity curve. The
cooldowns are in wall-clock minutes (as the live breaker enforces them), so on slow timeframes where
trades are naturally spaced hours apart the loss-streak / performance breakers mostly guard fast
re-entry / churn rather than slow swing streaks — the report flags that when it happens.

Deterministic, no network, no LLM — safe to run anytime.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from app.backtest.simulator import BTTrade


@dataclass
class BreakerImpact:
    name: str
    blocked: int          # trades this breaker would have skipped
    blocked_wins: int
    blocked_losses: int
    blocked_r: float      # gross R sum of the skipped trades
    net_effect_r: float   # −blocked_r: R change from skipping them (positive = it would have helped)
    note: str = ""


def _is_loss(t: BTTrade) -> bool:
    return t.r < 0


def _prior_closed_desc(closed_by_exit: list[BTTrade], entry_time) -> list[BTTrade]:
    """Trades that closed at or before ``entry_time``, most-recently-closed first."""
    prior = [x for x in closed_by_exit if x.exit_time <= entry_time]
    prior.sort(key=lambda x: x.exit_time, reverse=True)
    return prior


def _trade_count_blocked(trades: list[BTTrade], cap: int) -> list[BTTrade]:
    if cap <= 0:
        return []
    per_day: dict[object, int] = defaultdict(int)
    blocked: list[BTTrade] = []
    for t in sorted(trades, key=lambda x: x.entry_time):
        day = t.entry_time.date()
        per_day[day] += 1
        if per_day[day] > cap:
            blocked.append(t)
    return blocked


def _consecutive_loss_blocked(trades: list[BTTrade], n: int, cooldown_min: int) -> list[BTTrade]:
    if n <= 0:
        return []
    cd = timedelta(minutes=cooldown_min)
    closed_by_exit = sorted(trades, key=lambda x: x.exit_time)
    blocked: list[BTTrade] = []
    for t in sorted(trades, key=lambda x: x.entry_time):
        prior = _prior_closed_desc(closed_by_exit, t.entry_time)
        if not prior:
            continue
        streak = 0
        for x in prior:
            if _is_loss(x):
                streak += 1
            else:
                break
        if streak >= n and t.entry_time < prior[0].exit_time + cd:
            blocked.append(t)
    return blocked


def _perf_blocked(trades: list[BTTrade], window: int, floor: float, cooldown_min: int) -> list[BTTrade]:
    if window <= 0:
        return []
    cd = timedelta(minutes=cooldown_min)
    closed_by_exit = sorted(trades, key=lambda x: x.exit_time)
    blocked: list[BTTrade] = []
    for t in sorted(trades, key=lambda x: x.entry_time):
        prior = _prior_closed_desc(closed_by_exit, t.entry_time)
        if len(prior) < window:
            continue
        window_trades = prior[:window]
        avg = sum(x.r for x in window_trades) / window
        if avg < floor and t.entry_time < window_trades[0].exit_time + cd:
            blocked.append(t)
    return blocked


def _impact(name: str, blocked: list[BTTrade], slow_note: str = "") -> BreakerImpact:
    wins = sum(1 for t in blocked if t.r > 0)
    losses = sum(1 for t in blocked if t.r < 0)
    blocked_r = round(sum(t.r for t in blocked), 3)
    note = slow_note if not blocked and slow_note else ""
    return BreakerImpact(name=name, blocked=len(blocked), blocked_wins=wins, blocked_losses=losses,
                         blocked_r=blocked_r, net_effect_r=round(-blocked_r, 3), note=note)


def simulate_breakers(
    trades: list[BTTrade],
    *,
    max_trades_per_day: int = 8,
    max_consecutive_losses: int = 3,
    breaker_cooldown_minutes: int = 120,
    expectancy_window: int = 10,
    min_expectancy_r: float = -0.2,
) -> list[BreakerImpact]:
    """One BreakerImpact per breaker, measured over the merged historical trade stream."""
    slow = (f"0 blocked — trades are spaced further apart than the {breaker_cooldown_minutes}min "
            "cooldown, so on this timeframe it mainly guards fast re-entry / churn")
    return [
        _impact(f"Max trades/day = {max_trades_per_day}",
                _trade_count_blocked(trades, max_trades_per_day)),
        _impact(f"Max {max_consecutive_losses} losses in a row (cooldown {breaker_cooldown_minutes}min)",
                _consecutive_loss_blocked(trades, max_consecutive_losses, breaker_cooldown_minutes),
                slow_note=slow),
        _impact(f"Perf floor {min_expectancy_r:+.2f}R over {expectancy_window} trades "
                f"(cooldown {breaker_cooldown_minutes}min)",
                _perf_blocked(trades, expectancy_window, min_expectancy_r, breaker_cooldown_minutes),
                slow_note=slow),
    ]


def format_breaker_report(trades: list[BTTrade], impacts: list[BreakerImpact]) -> str:
    total_r = round(sum(t.r for t in trades), 2)
    lines = [
        "=" * 78,
        "ENTRY CIRCUIT BREAKERS — measured on the historical trade stream",
        "=" * 78,
        f"Baseline: {len(trades)} trades, net {total_r:+.2f}R "
        f"({sum(1 for t in trades if t.r > 0)} win / {sum(1 for t in trades if t.r < 0)} loss)",
        "(net effect = the R you'd gain/lose by skipping the blocked trades; + = it would have helped)",
        "-" * 78,
    ]
    for im in impacts:
        head = (f"{im.name:<52} blocked {im.blocked:>3}  "
                f"({im.blocked_wins}W/{im.blocked_losses}L, {im.blocked_r:+.2f}R)  "
                f"net {im.net_effect_r:+.2f}R")
        lines.append(head)
        if im.note:
            lines.append(f"    ↳ {im.note}")
    lines.append("=" * 78)
    return "\n".join(lines)
