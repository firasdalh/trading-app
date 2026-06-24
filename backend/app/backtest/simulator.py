"""Historical backtest of the DETERMINISTIC decision engine.

Replays ``run_technical`` + ``_deterministic_decision`` over historical OHLCV exactly as the live
system sees it — a rolling 200-bar window per timeframe, the same ``limit=200`` the scanner fetches —
with NO look-ahead, then simulates each actionable proposal forward to its stop/target and scores it
in R-multiples. This measures the engine's structural edge: win rate, expectancy, profit factor and
max drawdown, broken down per regime and per symbol.

Read before trusting the numbers:
- DETERMINISTIC engine only. The LLM reviewer (confirm/veto) and the news/fundamental stand-aside
  are NOT applied here — the LLM can only REMOVE trades, so live results differ. This is the raw
  skeleton of the strategy.
- Conservative intrabar fill: if one bar spans BOTH the stop and the target, the STOP is assumed to
  have been hit first (the pessimistic, honest assumption).
- Entries fill at the signal bar's close (no peeking at the next bar). Costs (spread/commission/swap)
  are modeled as a flat ``cost_r`` per trade — ``0`` means GROSS (no costs).
- Past performance is necessary, not sufficient. A positive backtest is a green light to keep
  testing on a small live sample, not a guarantee.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime

from app.agents.orchestrator import _deterministic_decision
from app.agents.technical import run_technical
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import FundamentalRead, OHLCVSeries

_WINDOW = 200  # bars per timeframe handed to the engine — matches the live scanner's limit=200


@dataclass
class BTTrade:
    """One simulated round-trip, scored in R (multiples of the initial risk = |entry - stop|)."""

    symbol: str
    direction: str
    regime: str | None
    strategy: str | None
    confidence: float
    entry_time: datetime
    entry: float
    stop: float
    target: float
    planned_rr: float
    exit_time: datetime
    exit: float
    outcome: str  # "target" | "stop" | "timeout"
    r: float      # realized R-multiple, net of cost_r
    bars_held: int
    atr: float | None = None   # entry-TF ATR at the signal (for volatility-bucket analysis)


@dataclass
class BTMetrics:
    n: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    expectancy_r: float = 0.0
    total_r: float = 0.0
    profit_factor: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0      # reported as a negative number
    max_dd_r: float = 0.0
    breakeven_wr: float = 0.0
    max_dd_pct: float = 0.0


def _neutral_fundamental(symbol: str) -> FundamentalRead:
    """A neutral fundamental read so the backtest measures the TECHNICAL engine only — the live
    news/calendar is 'now', not historical, so applying it would be look-ahead and irrelevant."""
    return FundamentalRead(symbol=symbol, bias=TradingBias.NEUTRAL)


def _simulate_trade(symbol: str, candles: list, i: int, prop, *, max_hold: int,
                    cost_r: float, atr: float | None = None) -> BTTrade | None:
    """Walk forward from the signal bar ``i`` (entry at its close) to the first stop/target hit, or
    a time-stop at ``max_hold`` bars. Conservative: if a bar tags both levels, the STOP wins."""
    entry = float(prop.entry)
    stop = float(prop.stop_loss)
    target = float(prop.take_profit)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = prop.direction == Direction.LONG
    planned_rr = abs(target - entry) / risk
    n = len(candles)
    end = min(i + max_hold, n - 1)

    # Reuse the existing engine's intrabar fill detection (stop checked first = conservative).
    from app.backtest.engine import _exit_price

    outcome: str | None = None
    exit_px = candles[end].close
    exit_j = end
    for j in range(i + 1, end + 1):
        hit = _exit_price(prop.direction, candles[j], stop, target)
        if hit is not None:
            exit_px, outcome, exit_j = hit[0], hit[1], j
            break
    if outcome is None:
        outcome = "timeout"

    raw_r = ((exit_px - entry) if is_long else (entry - exit_px)) / risk
    return BTTrade(
        symbol=symbol, direction=prop.direction.value, regime=prop.regime, strategy=prop.strategy,
        confidence=prop.confidence, entry_time=candles[i].ts, entry=entry, stop=stop, target=target,
        planned_rr=round(planned_rr, 2), exit_time=candles[exit_j].ts, exit=round(exit_px, 6),
        outcome=outcome, r=round(raw_r - cost_r, 4), bars_held=exit_j - i,
        atr=round(atr, 6) if atr else None,
    )


def simulate_symbol(broker, symbol: str, asset_class: AssetClass, timeframe: str = "1h", *,
                    bars: int = 1500, context_bars: int = 600, max_hold: int = 96,
                    cooldown: int = 3, cost_r: float = 0.0,
                    regimes: set[str] | None = None, scalp: bool = False) -> list[BTTrade]:
    """Replay the engine bar-by-bar over ``bars`` of history and return the simulated trades.

    At each entry-timeframe bar the engine is given the last 200 bars of EACH timeframe ending at
    that bar (no look-ahead), exactly like the live scanner. While a trade is open (plus a short
    cooldown) no new trade is taken, so overlapping signals aren't double-counted.

    ``regimes`` (e.g. {"trending"}) restricts which market regimes are tradeable — the engine stands
    aside in all others. This faithfully tests a 'trade only regime X' policy WITH path-dependency
    (skipped trades free up later bars), not just by filtering the result."""
    from app.agents.pipeline import _timeframes_for

    tfs = _timeframes_for(timeframe)
    series: dict[str, list] = {}
    for tf in tfs:
        limit = bars if tf == timeframe else context_bars
        try:
            s = broker.get_ohlcv(symbol, tf, limit=limit)
            series[tf] = list(s.candles) if s and s.candles else []
        except Exception:  # noqa: BLE001 - a data gap on one TF shouldn't abort the run
            series[tf] = []

    entry_candles = series.get(timeframe, [])
    if len(entry_candles) < _WINDOW + 5:
        return []  # not enough history to even warm up the indicators

    ts_index = {tf: [c.ts for c in series[tf]] for tf in tfs}
    fund = _neutral_fundamental(symbol)
    trades: list[BTTrade] = []
    n = len(entry_candles)

    i = _WINDOW - 1
    block_until = -1
    while i < n:
        if i <= block_until:
            i += 1
            continue
        t_i = entry_candles[i].ts

        window: list[OHLCVSeries] = []
        ok = True
        for tf in tfs:
            if tf == timeframe:
                w = entry_candles[max(0, i - _WINDOW + 1): i + 1]
            else:
                hi = bisect.bisect_right(ts_index[tf], t_i)   # context bars strictly up to now
                w = series[tf][max(0, hi - _WINDOW): hi]
            if not w:
                ok = False
                break
            window.append(OHLCVSeries(symbol=symbol, timeframe=tf, candles=w))
        if not ok:
            i += 1
            continue

        technical = run_technical(symbol, window, use_llm=False)
        prop = _deterministic_decision(symbol, asset_class, timeframe, technical, fund, now=t_i,
                                       scalp=scalp)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1
            continue
        if regimes is not None and prop.regime not in regimes:
            i += 1   # stand aside in regimes we're not trading (e.g. trade trending only)
            continue

        tf0 = next((x for x in technical.timeframes if x.timeframe == timeframe),
                   technical.timeframes[0] if technical.timeframes else None)
        atr_at = tf0.indicators.get("atr14") if tf0 else None
        trade = _simulate_trade(symbol, entry_candles, i, prop, max_hold=max_hold, cost_r=cost_r,
                                atr=atr_at)
        if trade is None:
            i += 1
            continue
        trades.append(trade)
        exit_i = i + trade.bars_held
        block_until = exit_i + cooldown
        i = exit_i + 1   # resume scanning after the trade closes

    return trades


def compute_metrics(trades: list[BTTrade], *, risk_pct: float = 0.01) -> BTMetrics:
    """Aggregate trades into the metrics a desk actually reads. ``risk_pct`` is the per-trade risk
    used only for the %-equity drawdown (the R-based stats are size-independent)."""
    n = len(trades)
    if n == 0:
        return BTMetrics()
    rs = [t.r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    cum = peak = max_dd = 0.0
    eq = eq_peak = 1.0
    max_dd_pct = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        eq *= (1 + r * risk_pct)
        eq_peak = max(eq_peak, eq)
        if eq_peak > 0:
            max_dd_pct = max(max_dd_pct, (eq_peak - eq) / eq_peak)

    breakeven = (avg_loss / (avg_win + avg_loss)) if (avg_win + avg_loss) > 0 else 0.0
    return BTMetrics(
        n=n, wins=len(wins), losses=len(losses), win_rate=len(wins) / n,
        expectancy_r=sum(rs) / n, total_r=sum(rs), profit_factor=pf,
        avg_win_r=avg_win, avg_loss_r=-avg_loss, max_dd_r=max_dd,
        breakeven_wr=breakeven, max_dd_pct=max_dd_pct,
    )


def split_by_time(trades: list[BTTrade], holdout: float) -> tuple[list[BTTrade], list[BTTrade]]:
    """Chronological in-sample / out-of-sample split. The last ``holdout`` fraction of the TIME span
    is held out — a robust edge persists into this unseen tail; an overfit one decays. Returns
    (in_sample, out_of_sample)."""
    if not trades or holdout <= 0:
        return list(trades), []
    times = sorted(t.entry_time for t in trades)
    lo, hi = times[0], times[-1]
    cutoff = lo + (hi - lo) * (1 - holdout)
    return ([t for t in trades if t.entry_time <= cutoff],
            [t for t in trades if t.entry_time > cutoff])


def time_folds(trades: list[BTTrade], n: int, lo=None, hi=None) -> list[list[BTTrade]]:
    """Assign trades to ``n`` equal-TIME folds over [lo, hi] (the trades' own time range if not
    given). Pass a shared lo/hi to align folds to the SAME calendar windows across symbols — so a
    walk-forward judges every instrument on the same market periods. A robust edge is positive in the
    MAJORITY of folds; a one-window fluke is not."""
    if not trades or n < 1:
        return [list(trades)]
    times = [t.entry_time for t in trades]
    lo = lo or min(times)
    hi = hi or max(times)
    span = (hi - lo).total_seconds() or 1.0
    folds: list[list[BTTrade]] = [[] for _ in range(n)]
    for t in trades:
        frac = (t.entry_time - lo).total_seconds() / span
        folds[min(n - 1, max(0, int(frac * n)))].append(t)
    return folds


def group_by(trades: list[BTTrade], key) -> dict:
    out: dict = {}
    for t in trades:
        out.setdefault(key(t) or "—", []).append(t)
    return out


def _pf(m: BTMetrics) -> str:
    return "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"


def _line(name: str, m: BTMetrics) -> str:
    return (f"{name:<16} n={m.n:<4} win={m.win_rate * 100:4.1f}%  exp={m.expectancy_r:+.3f}R  "
            f"PF={_pf(m):<5} totR={m.total_r:+6.1f}  maxDD={m.max_dd_r:4.1f}R")


def _verdict(m: BTMetrics, cost_r: float) -> str:
    if m.n < 30:
        return f"NOT ENOUGH DATA ({m.n} trades) — need ~30+ for a hint, 100+ to trust."
    gross = "  (GROSS — real edge is lower after spread/slippage)" if cost_r == 0 else ""
    if m.expectancy_r >= 0.10 and m.profit_factor >= 1.3:
        return f"POSITIVE edge: {m.expectancy_r:+.2f}R/trade, PF {_pf(m)}{gross}."
    if m.expectancy_r > 0:
        return f"MARGINAL: {m.expectancy_r:+.2f}R/trade{gross} — thin; costs may erase it."
    return f"NEGATIVE: {m.expectancy_r:+.2f}R/trade — no edge as configured."


def format_report(trades: list[BTTrade], *, title: str, risk_pct: float = 0.01,
                  cost_r: float = 0.0) -> str:
    """A clean text report: overall stats, per-regime, per-symbol, and a plain-English verdict."""
    bar = "=" * 80
    out = [bar, title, bar]
    if not trades:
        out.append("No trades generated. Try more --bars, or different --symbols/--timeframe.")
        return "\n".join(out)

    m = compute_metrics(trades, risk_pct=risk_pct)
    out += [
        f"Trades:          {m.n}",
        f"Win rate:        {m.win_rate * 100:.1f}%   (breakeven needs {m.breakeven_wr * 100:.1f}%)",
        f"Expectancy:      {m.expectancy_r:+.3f} R per trade",
        f"Profit factor:   {_pf(m)}",
        f"Avg win / loss:  +{m.avg_win_r:.2f}R / {m.avg_loss_r:.2f}R",
        f"Total:           {m.total_r:+.1f} R",
        f"Max drawdown:    {m.max_dd_r:.1f} R   ({m.max_dd_pct * 100:.1f}% equity @ {risk_pct * 100:.0f}%/trade)",
        f"Costs modeled:   {cost_r:.3f} R/trade" + ("   (GROSS — no costs)" if cost_r == 0 else ""),
        "",
        "By regime:",
    ]
    out += ["  " + _line(k, compute_metrics(v, risk_pct=risk_pct))
            for k, v in sorted(group_by(trades, lambda t: t.regime).items())]
    out += ["", "By symbol:"]
    out += ["  " + _line(k, compute_metrics(v, risk_pct=risk_pct))
            for k, v in sorted(group_by(trades, lambda t: t.symbol).items())]
    out += ["", "Verdict: " + _verdict(m, cost_r), bar]
    return "\n".join(out)
