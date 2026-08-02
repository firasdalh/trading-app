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
                    regimes: set[str] | None = None,
                    disable: frozenset[str] = frozenset(),
                    min_align: float = 0.0) -> list[BTTrade]:
    """Replay the engine bar-by-bar over ``bars`` of history and return the simulated trades.

    The big-picture (daily) confirmation is now a real engine filter, so measure it the same way as
    every other one: ``--disable daily_align`` runs without it.

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
                                       disable=disable)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1
            continue
        if min_align > 0.0 and (prop.alignment or 0.0) < min_align:
            i += 1  # segmentation: only trade setups at/above this alignment grade
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


def _tighter(is_long: bool, cur: float, new: float) -> bool:
    """True if ``new`` is a tighter (risk-reducing) stop than ``cur`` for the side."""
    if cur is None:
        return True
    return new > cur if is_long else new < cur


def _exit_indicators(entry: list, j: int, macro_closes: list, macro_ts: list, t_j):
    """Per-bar indicators for the advisor-aware exit: entry-TF trend (EMA20 vs 50), MACD hist, ATR,
    the higher-TF (macro) trend aligned to bar j's time, and the last swing low/high (structure, for
    the structure-based protective stop). Uses a 200-bar window (like the live engine)."""
    import bisect as _b

    from app.agents.indicators import atr as _atr
    from app.agents.indicators import ema as _ema
    from app.agents.indicators import macd as _macd
    from app.agents.indicators import market_structure as _ms

    w = entry[max(0, j - 199): j + 1]
    closes = [c.close for c in w]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    trend = "up" if (e20 and e50 and e20 > e50) else ("down" if (e20 and e50 and e20 < e50) else "sideways")
    m = _macd(closes)
    mh = m["hist"] if m else None
    a = _atr(w, 14)
    ms = _ms(w)
    swing_low, swing_high = ms.get("swing_low"), ms.get("swing_high")
    macro = "sideways"
    if macro_closes:
        hi = _b.bisect_right(macro_ts, t_j)
        cw = macro_closes[max(0, hi - 200): hi]
        ce20, ce50 = _ema(cw, 20), _ema(cw, 50)
        macro = "up" if (ce20 and ce50 and ce20 > ce50) else ("down" if (ce20 and ce50 and ce20 < ce50) else "sideways")
    return trend, mh, a, macro, swing_low, swing_high


def simulate_symbol_advisor(broker, symbol: str, asset_class: AssetClass, timeframe: str = "1h", *,
                            bars: int = 3000, context_bars: int = 600, max_hold: int = 96,
                            cooldown: int = 3, cost_r: float = 0.0, regimes: set[str] | None = None,
                            weaken_gate: str = "new", partial: bool = False,
                            be_mode: str = "entry") -> list[BTTrade]:
    """Like simulate_symbol, but the trade is exited the way the POSITION ADVISOR would manage it —
    a DYNAMIC stop (breakeven at +1R, trail at +1.5R, close on a confirmed trend flip, and the
    weakening->tighten) instead of the fixed entry stop. ``weaken_gate`` selects the tighten rule:
      "old" = tighten on ANY profit + any weakening (the pre-fix behavior);
      "new" = tighten only at >= +0.5R AND meaningful counter-momentum (the fix).
    Everything else is identical across the two, so a diff isolates the gate change.
    ``partial=True`` also models the advisor's scale-out: at +1.5R book half the position (locking
    +1.5R on that half) and move the runner's stop to breakeven — the trade's R is then a blend of
    the banked half and the runner's exit. (Still approximate: omits CHoCH / news / let-winners-run,
    which are the same across arms.)"""
    from app.agents.pipeline import _timeframes_for
    from app.agents.position_advisor import (
        _BREAKEVEN_R, _PARTIAL_FRACTION, _PARTIAL_R, _STRUCT_TRAIL_BUFFER_ATR, _TRAIL_ATR_MULT,
        _TRAIL_R, _WEAKEN_MIN_R, _WEAKEN_MOM_ATR_FRAC,
    )

    tfs = _timeframes_for(timeframe)
    series: dict[str, list] = {}
    for tf in tfs:
        limit = bars if tf == timeframe else context_bars
        try:
            sdata = broker.get_ohlcv(symbol, tf, limit=limit)
            series[tf] = list(sdata.candles) if sdata and sdata.candles else []
        except Exception:  # noqa: BLE001
            series[tf] = []
    entry = series.get(timeframe, [])
    if len(entry) < _WINDOW + 5:
        return []
    ts_index = {tf: [c.ts for c in series[tf]] for tf in tfs}
    macro_tf = tfs[-1] if len(tfs) > 1 else timeframe
    macro_closes = [c.close for c in series.get(macro_tf, [])]
    macro_ts = ts_index.get(macro_tf, [])
    fund = _neutral_fundamental(symbol)
    trades: list[BTTrade] = []
    n = len(entry)
    i = _WINDOW - 1
    block_until = -1
    while i < n:
        if i <= block_until:
            i += 1
            continue
        t_i = entry[i].ts
        window: list[OHLCVSeries] = []
        ok = True
        for tf in tfs:
            if tf == timeframe:
                w = entry[max(0, i - _WINDOW + 1): i + 1]
            else:
                hi = bisect.bisect_right(ts_index[tf], t_i)
                w = series[tf][max(0, hi - _WINDOW): hi]
            if not w:
                ok = False
                break
            window.append(OHLCVSeries(symbol=symbol, timeframe=tf, candles=w))
        if not ok:
            i += 1
            continue
        technical = run_technical(symbol, window, use_llm=False)
        prop = _deterministic_decision(symbol, asset_class, timeframe, technical, fund, now=t_i)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1
            continue
        if regimes is not None and prop.regime not in regimes:
            i += 1
            continue

        d = prop.direction
        is_long = d == Direction.LONG
        entry_px, stop0, tp = float(prop.entry), float(prop.stop_loss), float(prop.take_profit)
        plan_risk = abs(entry_px - stop0)
        if plan_risk <= 0:
            i += 1
            continue
        cur_stop = stop0
        end = min(i + max_hold, n - 1)
        outcome, exit_px, exit_j = None, entry[end].close, end
        want, opp = ("up", "down") if is_long else ("down", "up")
        banked_r, frac, scaled = 0.0, 1.0, False
        plevel = entry_px + (_PARTIAL_R * plan_risk if is_long else -_PARTIAL_R * plan_risk)
        for j in range(i + 1, end + 1):
            bar = entry[j]
            if is_long:
                if bar.low <= cur_stop:
                    outcome, exit_px, exit_j = "stop", cur_stop, j; break
            else:
                if bar.high >= cur_stop:
                    outcome, exit_px, exit_j = "stop", cur_stop, j; break
            # scale-out at +1.5R: bank half, move the runner's stop to breakeven (checked before the
            # target so a bar that clears 1.5R banks the partial even if it then runs to the target).
            if partial and not scaled and ((bar.high >= plevel) if is_long else (bar.low <= plevel)):
                banked_r += frac * _PARTIAL_FRACTION * _PARTIAL_R
                frac *= (1.0 - _PARTIAL_FRACTION)
                scaled = True
                if _tighter(is_long, cur_stop, entry_px):
                    cur_stop = entry_px
            if is_long:
                if bar.high >= tp:
                    outcome, exit_px, exit_j = "target", tp, j; break
            else:
                if bar.low <= tp:
                    outcome, exit_px, exit_j = "target", tp, j; break
            close_j = bar.close
            trend, mh, a, macro, swing_low, swing_high = _exit_indicators(
                entry, j, macro_closes, macro_ts, bar.ts)
            if not a:
                continue
            profit = (close_j - entry_px) if is_long else (entry_px - close_j)
            r = profit / plan_risk
            if trend == opp and macro == opp:          # thesis invalidated -> close now
                outcome, exit_px, exit_j = "advisor_close", close_j, j; break
            if r >= _TRAIL_R:                          # trail
                t = (close_j - _TRAIL_ATR_MULT * a) if is_long else (close_j + _TRAIL_ATR_MULT * a)
                if _tighter(is_long, cur_stop, t):
                    cur_stop = t
            elif r >= _BREAKEVEN_R:                     # protect: structure (last swing) or entry
                if be_mode == "structure":
                    sw = swing_low if is_long else swing_high
                    cand = ((sw - _STRUCT_TRAIL_BUFFER_ATR * a) if is_long
                            else (sw + _STRUCT_TRAIL_BUFFER_ATR * a)) if sw is not None else entry_px
                else:
                    cand = entry_px
                if _tighter(is_long, cur_stop, cand):
                    cur_stop = cand
            mom_against = mh is not None and ((is_long and mh < 0) or ((not is_long) and mh > 0))
            weak = (trend == opp and macro != opp) or (trend == "sideways") or mom_against
            if weak:
                if weaken_gate == "old":
                    do_tighten = profit > 0
                else:
                    do_tighten = r >= _WEAKEN_MIN_R and mom_against and abs(mh) >= _WEAKEN_MOM_ATR_FRAC * a
                if do_tighten:
                    t = (close_j - _TRAIL_ATR_MULT * a) if is_long else (close_j + _TRAIL_ATR_MULT * a)
                    if _tighter(is_long, cur_stop, t):
                        cur_stop = t
        if outcome is None:
            outcome = "timeout"
        exit_r = ((exit_px - entry_px) if is_long else (entry_px - exit_px)) / plan_risk
        raw_r = banked_r + frac * exit_r   # banked half (if scaled) + the runner's exit
        trades.append(BTTrade(
            symbol=symbol, direction=d.value, regime=prop.regime, strategy=prop.strategy,
            confidence=prop.confidence, entry_time=entry[i].ts, entry=entry_px, stop=stop0, target=tp,
            planned_rr=round(abs(tp - entry_px) / plan_risk, 2), exit_time=entry[exit_j].ts,
            exit=round(exit_px, 6), outcome=outcome, r=round(raw_r - cost_r, 4), bars_held=exit_j - i,
        ))
        block_until = exit_j + cooldown
        i = exit_j + 1
    return trades


def _pullback_scores(candles: list, st_dir: list[int], L: int = 3) -> tuple[list[float], list[bool]]:
    """Per-bar Pullback confidence (0-100) + direction, ported from the chart (causal). In a
    SuperTrend UPtrend it scores a buy-the-dip (close<EMA20 AND RSI<53 = the +35 trigger, then
    volume falling +15, bullish engulfing +10, higher-low intact +15, trend +25); the DOWNtrend
    mirror scores a sell-the-rally. Score 0 when the trigger isn't met."""
    from app.agents.indicators import _ema_full

    n = len(candles)
    scores = [0.0] * n
    longs = [True] * n
    if n < 30:
        return scores, longs
    closes = [c.close for c in candles]
    ema20 = _ema_full(closes, 20)
    # Wilder RSI series (causal), matching the chart's rsiCalc.
    rsi: list[float | None] = [None] * n
    period = 14
    if n > period:
        gains = losses = 0.0
        for i in range(1, period + 1):
            ch = closes[i] - closes[i - 1]
            gains += ch if ch > 0 else 0.0
            losses += -ch if ch < 0 else 0.0
        ag, al = gains / period, losses / period
        rsi[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        for i in range(period + 1, n):
            ch = closes[i] - closes[i - 1]
            ag = (ag * (period - 1) + (ch if ch > 0 else 0.0)) / period
            al = (al * (period - 1) + (-ch if ch < 0 else 0.0)) / period
            rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    piv_low = [False] * n
    piv_high = [False] * n
    for j in range(L, n - L):
        win = candles[j - L: j + L + 1]
        if candles[j].low == min(c.low for c in win):
            piv_low[j] = True
        if candles[j].high == max(c.high for c in win):
            piv_high[j] = True

    def _vol_avg(i: int) -> float:
        s = c = 0.0
        for k in range(max(0, i - 10), i):
            s += candles[k].volume
            c += 1
        return s / c if c else 0.0

    def _last_two(arr, upto):
        idx = []
        for j in range(upto, -1, -1):
            if arr[j]:
                idx.append(j)
                if len(idx) == 2:
                    break
        return idx

    for i in range(1, n):
        e, r, d = ema20[i], rsi[i], st_dir[i]
        if e is None or r is None or d == 0:
            continue
        c, p = candles[i], candles[i - 1]
        longs[i] = d == 1
        if d == 1:
            if not (c.close < e and r < 53):
                continue
            s = 25 + 35
            if c.volume < _vol_avg(i):
                s += 15
            if c.close > c.open and p.close < p.open and c.close >= p.open and c.open <= p.close:
                s += 10
            pv = _last_two(piv_low, i - L)
            if len(pv) == 2 and candles[pv[0]].low > candles[pv[1]].low:
                s += 15
            scores[i] = s
        else:
            if not (c.close > e and r > 46):
                continue
            s = 25 + 35
            if c.volume < _vol_avg(i):
                s += 15
            if c.close < c.open and p.close > p.open and c.close <= p.open and c.open >= p.close:
                s += 10
            pv = _last_two(piv_high, i - L)
            if len(pv) == 2 and candles[pv[0]].high < candles[pv[1]].high:
                s += 15
            scores[i] = s
    return scores, longs


def simulate_symbol_stband(broker, symbol: str, asset_class: AssetClass, timeframe: str = "1h", *,
                           bars: int = 3000, max_hold: int = 200, cooldown: int = 1,
                           cost_r: float = 0.0, adx_min: float | None = None,
                           fresh_flip: int | None = None, entry_mode: str = "band",
                           pb_threshold: float = 70.0, exit_mode: str = "trail") -> list[BTTrade]:
    """Backtest a SuperTrend strategy WITH its SuperTrend trailing stop.

    ``entry_mode``:
      "band"     — the breakout: long when SuperTrend up AND a candle closes above EMA20-high; short
                   when down AND closes below EMA20-low (buy strength).
      "pullback" — the VALUE entry: long when SuperTrend up AND the Pullback score >= pb_threshold
                   (buy-the-dip); short when down AND the score fires (sell-the-rally).

    Single timeframe. Stop starts on the SuperTrend line and TRAILS it each bar (tighten-only); exit
    on the trailed stop, a 3R backstop, or a SuperTrend flip. Causal (no look-ahead); R is measured
    from the INITIAL risk (|entry - first SuperTrend stop|).

    ``adx_min`` (optional): only take a signal when ADX at the entry bar >= this value.
    ``fresh_flip`` (optional): only enter within this many bars of the SuperTrend flip."""
    from app.agents.indicators import ST_PERIOD, _ema_full, adx, supertrend_series

    try:
        sdata = broker.get_ohlcv(symbol, timeframe, limit=bars)
        candles = list(sdata.candles) if sdata and sdata.candles else []
    except Exception:  # noqa: BLE001
        return []
    n = len(candles)
    if n < ST_PERIOD + 30:
        return []
    st = supertrend_series(candles)
    st_line, st_dir = st["line"], st["dir"]
    # Bars since the last SuperTrend flip (for the fresh-flip filter).
    bars_since_flip: list[int | None] = [None] * n
    last_flip: int | None = None
    for k in range(1, n):
        if st_dir[k] != 0 and st_dir[k - 1] != 0 and st_dir[k] != st_dir[k - 1]:
            last_flip = k
        bars_since_flip[k] = (k - last_flip) if last_flip is not None else None
    eh = _ema_full([c.high for c in candles], 20)
    el = _ema_full([c.low for c in candles], 20)
    pb_scores, pb_longs = _pullback_scores(candles, st_dir) if entry_mode == "pullback" else ([], [])
    strat_label = f"supertrend_{entry_mode}"

    trades: list[BTTrade] = []
    i = 21           # warmup: EMA20 valid from index 19, SuperTrend from ST_PERIOD
    block_until = -1
    while i < n:
        if i <= block_until:
            i += 1
            continue
        d, line_i, c = st_dir[i], st_line[i], candles[i]
        if line_i is None or d == 0:
            i += 1
            continue
        direction = None
        if entry_mode == "pullback":
            # Value entry: a high Pullback score in the SuperTrend direction, with the SuperTrend
            # line on the correct side to be the stop.
            if pb_scores[i] >= pb_threshold:
                if pb_longs[i] and line_i < c.close:
                    direction, entry, stop0, is_long = "long", c.close, line_i, True
                elif (not pb_longs[i]) and line_i > c.close:
                    direction, entry, stop0, is_long = "short", c.close, line_i, False
        else:  # band breakout
            hi_i, lo_i = eh[i], el[i]
            if hi_i is not None and lo_i is not None:
                if d == 1 and c.close > hi_i and line_i < c.close:
                    direction, entry, stop0, is_long = "long", c.close, line_i, True
                elif d == -1 and c.close < lo_i and line_i > c.close:
                    direction, entry, stop0, is_long = "short", c.close, line_i, False
        if direction is None:
            i += 1
            continue
        # Fresh-flip filter: only enter EARLY in a new trend (within N bars of the SuperTrend flip).
        if fresh_flip is not None and (bars_since_flip[i] is None or bars_since_flip[i] > fresh_flip):
            i += 1
            continue
        # Regime / chop filter: skip the signal unless the trend is strong enough (ADX >= adx_min),
        # computed causally on a trailing window.
        if adx_min is not None:
            av = adx(candles[max(0, i - 120): i + 1])
            if av is None or av["adx"] < adx_min:
                i += 1
                continue
        # Exit levels: static STRUCTURE (support/resistance) or the SuperTrend TRAIL.
        if exit_mode == "structure":
            lb = candles[max(0, i - 20):i]
            wlo = min(cd.low for cd in lb) if lb else entry
            whi = max(cd.high for cd in lb) if lb else entry
            atr_i = abs(entry - line_i) / 2.7 if line_i else 0.0   # SuperTrend line ~ 2.7*ATR from price
            buf = 0.2 * atr_i
            if is_long:
                stop0 = (wlo - buf) if wlo < entry else entry - 1.5 * atr_i
                risk = entry - stop0
                tp = whi if whi > entry else entry + 2 * risk
            else:
                stop0 = (whi + buf) if whi > entry else entry + 1.5 * atr_i
                risk = stop0 - entry
                tp = wlo if wlo < entry else entry - 2 * risk
            if risk <= 0 or abs(tp - entry) / risk < 1.5:
                i += 1
                continue
        else:  # trail
            risk = abs(entry - stop0)
            if risk <= 0:
                i += 1
                continue
            tp = entry + 3 * risk if is_long else entry - 3 * risk
        cur_stop = stop0
        end = min(i + max_hold, n - 1)
        outcome, exit_px, exit_j = "timeout", candles[end].close, end
        want_dir = 1 if is_long else -1
        for j in range(i + 1, end + 1):
            bar = candles[j]
            if exit_mode == "trail" and st_dir[j] == want_dir and st_line[j] is not None:
                # Trail the stop to the SuperTrend line while the trend agrees (tighten-only).
                if is_long and st_line[j] > cur_stop:
                    cur_stop = st_line[j]
                elif (not is_long) and st_line[j] < cur_stop:
                    cur_stop = st_line[j]
            # Intrabar exit — stop first (conservative), then target.
            if is_long:
                if bar.low <= cur_stop:
                    outcome, exit_px, exit_j = "stop", cur_stop, j; break
                if bar.high >= tp:
                    outcome, exit_px, exit_j = "target", tp, j; break
            else:
                if bar.high >= cur_stop:
                    outcome, exit_px, exit_j = "stop", cur_stop, j; break
                if bar.low <= tp:
                    outcome, exit_px, exit_j = "target", tp, j; break
            # SuperTrend flip exit (trailing mode only).
            if exit_mode == "trail" and st_dir[j] == -want_dir:
                outcome, exit_px, exit_j = "flip", bar.close, j; break
        raw_r = ((exit_px - entry) if is_long else (entry - exit_px)) / risk
        trades.append(BTTrade(
            symbol=symbol, direction=direction, regime="supertrend", strategy=strat_label,
            confidence=0.7, entry_time=candles[i].ts, entry=round(entry, 6), stop=round(stop0, 6),
            target=round(tp, 6), planned_rr=round(abs(tp - entry) / risk, 2),
            exit_time=candles[exit_j].ts, exit=round(exit_px, 6),
            outcome=outcome, r=round(raw_r - cost_r, 4), bars_held=exit_j - i,
        ))
        block_until = exit_j + cooldown
        i = exit_j + 1
    return trades


def _crossed_intrabar(order_type: str, bar, trigger: float) -> bool:
    """Did this bar reach a pending order's trigger? STOP = break beyond the level; LIMIT = pull into it."""
    if order_type == "buy_stop":
        return bar.high >= trigger
    if order_type == "sell_stop":
        return bar.low <= trigger
    if order_type == "buy_limit":
        return bar.low <= trigger
    if order_type == "sell_limit":
        return bar.high >= trigger
    return False


def _armed_outcome(symbol: str, candles: list, i: int, armed: dict, max_hold: int,
                   cost_r: float) -> BTTrade | None:
    """A pending order filled at its trigger on bar ``i`` -> walk forward to the first stop/target
    (R measured from the TRIGGER). Conservative: stop checked first on an ambiguous bar."""
    from app.backtest.engine import _exit_price

    entry, stop, target = armed["trigger"], armed["stop"], armed["tp"]
    if not (entry and stop and target):
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    direction = Direction.LONG if armed["order_type"] in ("buy_stop", "buy_limit") else Direction.SHORT
    is_long = direction == Direction.LONG
    n = len(candles)
    end = min(i + max_hold, n - 1)
    outcome: str | None = None
    exit_px = candles[end].close
    exit_j = end
    for j in range(i + 1, end + 1):
        hit = _exit_price(direction, candles[j], stop, target)
        if hit is not None:
            exit_px, outcome, exit_j = hit[0], hit[1], j
            break
    if outcome is None:
        outcome = "timeout"
    raw_r = ((exit_px - entry) if is_long else (entry - exit_px)) / risk
    return BTTrade(
        symbol=symbol, direction=direction.value, regime="armed", strategy=armed["order_type"],
        confidence=0.0, entry_time=candles[i].ts, entry=entry, stop=stop, target=target,
        planned_rr=round(abs(target - entry) / risk, 2), exit_time=candles[exit_j].ts,
        exit=round(exit_px, 6), outcome=outcome, r=round(raw_r - cost_r, 4), bars_held=exit_j - i,
    )


def simulate_armed_symbol(broker, symbol: str, asset_class: AssetClass, timeframe: str = "1h", *,
                          bars: int = 3000, context_bars: int = 600, valid_bars: int = 16,
                          max_hold: int = 96, cooldown: int = 3, cost_r: float = 0.0,
                          trend_only: bool = False):
    """Backtest the ARMED / conditional strategy (the 'wait for the break, then fire' path).

    At each bar the engine may emit a conditional (break-stop / pullback-limit / resumption-stop);
    if so it is ARMED. If price reaches the trigger within ``valid_bars`` it fills AT the trigger and
    runs to stop/target (R from the trigger); otherwise it EXPIRES. One armed setup / position at a
    time. Returns ``(filled_trades, {"armed", "filled", "expired"})`` so we can read the fill rate AND
    the edge of the trades that actually triggered."""
    from app.agents.pipeline import _timeframes_for

    tfs = _timeframes_for(timeframe)
    series: dict[str, list] = {}
    for tf in tfs:
        limit = bars if tf == timeframe else context_bars
        try:
            sdata = broker.get_ohlcv(symbol, tf, limit=limit)
            series[tf] = list(sdata.candles) if sdata and sdata.candles else []
        except Exception:  # noqa: BLE001
            series[tf] = []
    entry = series.get(timeframe, [])
    stats = {"armed": 0, "filled": 0, "expired": 0}
    if len(entry) < _WINDOW + 5:
        return [], stats

    ts_index = {tf: [c.ts for c in series[tf]] for tf in tfs}
    fund = _neutral_fundamental(symbol)
    trades: list[BTTrade] = []
    n = len(entry)
    i = _WINDOW - 1
    armed: dict | None = None
    block_until = -1
    while i < n:
        bar = entry[i]
        if armed is not None:
            if _crossed_intrabar(armed["order_type"], bar, armed["trigger"]):
                t = _armed_outcome(symbol, entry, i, armed, max_hold, cost_r)
                armed = None
                if t is not None:
                    trades.append(t)
                    stats["filled"] += 1
                    exit_i = i + t.bars_held
                    block_until = exit_i + cooldown
                    i = exit_i + 1
                    continue
            elif i >= armed["expire"]:
                stats["expired"] += 1
                armed = None
            i += 1
            continue
        if i <= block_until:
            i += 1
            continue

        t_i = bar.ts
        window: list[OHLCVSeries] = []
        ok = True
        for tf in tfs:
            if tf == timeframe:
                w = entry[max(0, i - _WINDOW + 1): i + 1]
            else:
                hi = bisect.bisect_right(ts_index[tf], t_i)
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
                                       trend_only=trend_only)
        c = prop.conditional
        if c is not None and c.trigger_price and c.stop_loss and c.take_profit:
            armed = {"order_type": c.order_type, "trigger": c.trigger_price, "stop": c.stop_loss,
                     "tp": c.take_profit, "expire": i + valid_bars}
            stats["armed"] += 1
        i += 1

    return trades, stats


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


def _conf_bucket(t: BTTrade) -> str:
    """Confidence decile label, e.g. '70-80%'. Top bucket is 90-100%."""
    lo = min(90, int((t.confidence or 0.0) * 10) * 10)
    return f"{lo}-{lo + 10}%"


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
    # By confidence decile — does a higher score actually predict a better outcome? (If 90%+ wins
    # LESS than 70-80%, the score is miscalibrated at the top and picking the max is counterproductive.)
    out += ["", "By confidence:"]
    out += ["  " + _line(k, compute_metrics(v, risk_pct=risk_pct))
            for k, v in sorted(group_by(trades, _conf_bucket).items())]
    out += ["", "By strategy:"]
    out += ["  " + _line(k, compute_metrics(v, risk_pct=risk_pct))
            for k, v in sorted(group_by(trades, lambda t: t.strategy).items())]
    out += ["", "By symbol:"]
    out += ["  " + _line(k, compute_metrics(v, risk_pct=risk_pct))
            for k, v in sorted(group_by(trades, lambda t: t.symbol).items())]
    out += ["", "Verdict: " + _verdict(m, cost_r), bar]
    return "\n".join(out)
