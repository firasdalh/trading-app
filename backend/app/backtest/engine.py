"""Backtesting engine.

Replays historical OHLCV bar-by-bar through the SAME deterministic Technical -> Orchestrator
-> Risk pipeline used live, simulates entries (at the decision bar's close) and stop/target
exits (against subsequent bars' high/low), and reports realistic metrics + an equity curve.

Deterministic agents are used on purpose: backtests must be fast and reproducible, and
running the LLM over thousands of bars would be slow and costly. The Fundamental input is a
neutral stub here (historical news replay can be added later). One position at a time per
run, which is the right granularity for evaluating a single instrument's edge.

Nothing about a backtest touches a broker or places an order.
"""
from __future__ import annotations

from app.agents.orchestrator import _deterministic_decision
from app.agents.technical import _deterministic_read
from app.core.logging import get_logger
from app.models.enums import AssetClass, Direction, TradingBias
from app.models.schemas import (
    BacktestMetrics,
    BacktestResult,
    BacktestTrade,
    Candle,
    EquityPoint,
    FundamentalRead,
    OHLCVSeries,
    RiskLimits,
)
from app.risk.manager import evaluate_proposal

log = get_logger("backtest")

_WARMUP = 50  # bars needed before the indicators are meaningful

_QTY_STEP = {
    AssetClass.STOCK: 1.0,
    AssetClass.CRYPTO: None,
    AssetClass.FOREX: None,
    AssetClass.METAL: None,
}


def _neutral_fundamental(symbol: str) -> FundamentalRead:
    return FundamentalRead(symbol=symbol, bias=TradingBias.NEUTRAL)


def _exit_price(direction: Direction, bar: Candle, stop: float, target: float) -> tuple[float, str] | None:
    """Detect a stop/target hit within a bar. Stop is checked first (conservative)."""
    if direction == Direction.LONG:
        if bar.low <= stop:
            return stop, "stop"
        if bar.high >= target:
            return target, "target"
    else:
        if bar.high >= stop:
            return stop, "stop"
        if bar.low <= target:
            return target, "target"
    return None


def run_backtest(
    symbol: str,
    asset_class: AssetClass,
    series: OHLCVSeries,
    limits: RiskLimits,
    starting_equity: float = 100_000.0,
) -> BacktestResult:
    candles = series.candles
    timeframe = series.timeframe
    qty_step = _QTY_STEP.get(asset_class)

    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    equity_curve: list[EquityPoint] = []
    trades: list[BacktestTrade] = []

    # Open-position state.
    pos: dict | None = None

    n = len(candles)
    start = min(_WARMUP, max(0, n - 1))
    for i in range(start, n):
        bar = candles[i]

        if pos is not None:
            hit = _exit_price(pos["direction"], bar, pos["stop"], pos["target"])
            if hit is not None:
                exit_price, reason = hit
                sign = 1 if pos["direction"] == Direction.LONG else -1
                pnl = round(sign * pos["qty"] * (exit_price - pos["entry"]), 2)
                equity += pnl
                trades.append(BacktestTrade(
                    entry_time=candles[pos["entry_index"]].ts, exit_time=bar.ts,
                    direction=pos["direction"], entry=pos["entry"], exit=round(exit_price, 6),
                    qty=pos["qty"], pnl=pnl,
                    r_multiple=round(pnl / pos["risk"], 3) if pos["risk"] else 0.0,
                    bars_held=i - pos["entry_index"], exit_reason=reason,
                ))
                pos = None

        # Mark-to-market equity (include open position's unrealized P&L) for a smooth curve.
        mtm = equity
        if pos is not None:
            sign = 1 if pos["direction"] == Direction.LONG else -1
            mtm = round(equity + sign * pos["qty"] * (bar.close - pos["entry"]), 2)
        peak = max(peak, mtm)
        if peak > 0:
            max_dd = max(max_dd, (peak - mtm) / peak)
        equity_curve.append(EquityPoint(ts=bar.ts, equity=mtm))

        # Look for a new entry only when flat.
        if pos is None and i >= _WARMUP:
            window = OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=candles[: i + 1])
            technical = _deterministic_read(symbol, [window])
            proposal = _deterministic_decision(
                symbol, asset_class, timeframe, technical, _neutral_fundamental(symbol), now=bar.ts,
            )
            if proposal.is_actionable:
                from app.models.schemas import AccountState

                account = AccountState(equity=equity, cash=equity, open_positions=0,
                                       total_risk_amount=0.0, daily_realized_pnl=0.0)
                decision = evaluate_proposal(proposal, account, limits, now=bar.ts, qty_step=qty_step)
                if decision.approved and decision.approved_qty > 0:
                    pos = {
                        "direction": proposal.direction,
                        "entry": proposal.entry,
                        "stop": proposal.stop_loss,
                        "target": proposal.take_profit if proposal.take_profit is not None
                        else (proposal.entry * (1.04 if proposal.direction == Direction.LONG else 0.96)),
                        "qty": decision.approved_qty,
                        "risk": decision.risk_amount,
                        "entry_index": i,
                    }

    # Close any open position at the last bar's close.
    if pos is not None and candles:
        last = candles[-1]
        sign = 1 if pos["direction"] == Direction.LONG else -1
        pnl = round(sign * pos["qty"] * (last.close - pos["entry"]), 2)
        equity += pnl
        trades.append(BacktestTrade(
            entry_time=candles[pos["entry_index"]].ts, exit_time=last.ts,
            direction=pos["direction"], entry=pos["entry"], exit=round(last.close, 6),
            qty=pos["qty"], pnl=pnl,
            r_multiple=round(pnl / pos["risk"], 3) if pos["risk"] else 0.0,
            bars_held=len(candles) - 1 - pos["entry_index"], exit_reason="end_of_data",
        ))

    metrics = _metrics(trades, starting_equity, equity, max_dd)
    log.info("backtest complete", extra={"symbol": symbol, "trades": metrics.total_trades,
                                         "win_rate": metrics.win_rate, "return_pct": metrics.return_pct})
    return BacktestResult(
        symbol=symbol, asset_class=asset_class, timeframe=timeframe, bars=n,
        metrics=metrics, equity_curve=equity_curve, trades=trades,
    )


def _metrics(trades: list[BacktestTrade], start_eq: float, end_eq: float, max_dd: float) -> BacktestMetrics:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    total = len(trades)
    gross_win = sum(t.pnl for t in wins)
    gross_loss = sum(t.pnl for t in losses)  # negative
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss != 0 else None

    def _avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 3) if xs else 0.0

    return BacktestMetrics(
        total_trades=total,
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / total, 3) if total else 0.0,
        avg_r_multiple=_avg([t.r_multiple for t in trades]),
        avg_win_r=_avg([t.r_multiple for t in wins]),
        avg_loss_r=_avg([t.r_multiple for t in losses]),
        profit_factor=round(profit_factor, 3) if profit_factor is not None else None,
        net_pnl=round(end_eq - start_eq, 2),
        return_pct=round((end_eq - start_eq) / start_eq, 4) if start_eq else 0.0,
        max_drawdown_pct=round(max_dd, 4),
        starting_equity=round(start_eq, 2),
        ending_equity=round(end_eq, 2),
    )
