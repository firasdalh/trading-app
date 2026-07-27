"""Focused backtest for the RSI-Over strategy — used to validate the two new pro confirmations
(rejection candle + fade-at-level) before trusting them live.

It replays each symbol bar-by-bar and, at every bar, calls the REAL ``_rsi_over_decision`` (so there's
no logic drift from the live path) with the base settings (EMA10 confirm + trend filter). For every
entry it also records whether a rejection candle was present and whether price was AT a key opposing
level. Then it compares four subsets:

    BASE            — every EMA10-confirmed RSI-extreme fade
    + REJECTION     — the base entries that ALSO had a rejection candle
    + AT LEVEL      — the base entries that were ALSO at a key S/R level
    + BOTH          — the confluence

If the filtered subsets show a higher win rate / expectancy than BASE, the confirmations add edge.

Approximation (stated honestly): the live trend filter uses the real higher-timeframe read; here it's
proxied by the entry-TF EMA50/EMA200 stack. It's applied identically to every subset, so it can't bias
the COMPARISON — only the absolute numbers. Deterministic, no LLM.

Run:  python -m app.backtest.rsi_over_sim               # enabled watchlist
      python -m app.backtest.rsi_over_sim --symbols XAUUSDm,EURUSDm --timeframe 1h --bars 1500
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.agents.indicators import (
    adx,
    atr,
    divergence,
    ema,
    failed_break,
    macd_signals,
    rejection_candle,
    rsi,
    swing_levels,
)
from app.agents.orchestrator import _nearest_fade_level, _rsi_over_decision
from app.backtest.simulator import BTTrade, _pf, compute_metrics
from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass, Direction
from app.models.schemas import TradeProposal

_WARMUP = 200  # bars needed to warm up EMA200 (the trend proxy)
_AT_LEVEL_ATR = 0.6


class _TF0:
    def __init__(self, support: list[float], resistance: list[float]):
        self.support_levels = support
        self.resistance_levels = resistance


def _build_ind(w: list) -> dict:
    """The entry-TF indicator dict the decision needs, computed causally on the trailing window."""
    closes = [c.close for c in w]
    ind: dict = {"last_close": closes[-1], "rsi14": rsi(closes), "ema10": ema(closes, 10),
                 "ema50": ema(closes, 50), "ema200": ema(closes, 200), "atr14": atr(w)}
    rec = w[-12:]
    ind["recent_high"] = max(c.high for c in rec)
    ind["recent_low"] = min(c.low for c in rec)
    ms = macd_signals(w)
    if ms:
        ind["macd_cross"] = ms["cross"]
        ind["macd_div_bull"] = ms["div_bull"]
        ind["macd_div_bear"] = ms["div_bear"]
    ind.update(divergence(w))            # div_bull / div_bear (RSI divergence)
    ind.update(rejection_candle(w))      # rej_bull / rej_bear
    ind.update(failed_break(w))          # fbreak_bull / fbreak_bear
    a = adx(w)
    if a:
        ind["adx"] = a["adx"]
    return ind


def _trend_proxy(ind: dict) -> str:
    """A same-TF stand-in for the higher-TF trend. Only calls a trend when it's REAL (ADX >= 22) so
    the fade is refused into a genuine trend but allowed in choppy/ranging tape (where the mean-
    reversion edge lives) — mirroring how the live higher-TF filter behaves. Sideways otherwise."""
    adx_v, e50, e200 = ind.get("adx"), ind.get("ema50"), ind.get("ema200")
    if not (adx_v and e50 and e200) or adx_v < 22:
        return "sideways"
    return "up" if e50 > e200 else "down"


def simulate_rsi_over(broker, symbol: str, asset_class: AssetClass, timeframe: str = "1h", *,
                      bars: int = 1500, max_hold: int = 96, cooldown: int = 3,
                      cost_r: float = 0.0) -> list[dict]:
    """Replay RSI-Over over ``bars`` of history. Returns a list of
    ``{"trade": BTTrade, "had_rej": bool, "at_level": bool}`` — one per base entry."""
    from app.backtest.engine import _exit_price

    try:
        s = broker.get_ohlcv(symbol, timeframe, limit=bars)
        candles = list(s.candles) if s and s.candles else []
    except Exception:  # noqa: BLE001 - a data gap shouldn't abort the run
        candles = []
    if len(candles) < _WARMUP + 5:
        return []

    out: list[dict] = []
    n = len(candles)
    i = _WARMUP - 1
    block_until = -1
    while i < n:
        if i <= block_until:
            i += 1
            continue
        w = candles[max(0, i - _WARMUP + 1): i + 1]
        ind = _build_ind(w)
        # Levels for the at-level filter = PRIOR structure (exclude the last 3 bars so the level isn't
        # just the current extreme itself), over a 40-bar lookback — a real wall price is approaching.
        sup, res = swing_levels(w[:-3], 40)
        tf0 = _TF0([sup] if sup else [], [res] if res else [])
        macro = _trend_proxy(ind)
        base = TradeProposal(symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                             direction=Direction.NO_TRADE, confidence=0.0)
        # BASE = every trend-filtered RSI extreme (confirm=False), so we can measure the marginal lift
        # of EACH confirmation (EMA10 / rejection / at-level) as a filter over the same population.
        prop = _rsi_over_decision(base, ind, tf0, symbol, confirm=False, trend_filter=True,
                                  macro=macro, htf=macro, htf_name=timeframe)
        if prop.direction not in (Direction.LONG, Direction.SHORT) or not prop.entry or not prop.stop_loss:
            i += 1
            continue

        is_long = prop.direction == Direction.LONG
        px = ind["last_close"]
        ema10 = ind.get("ema10")
        atr_v = ind.get("atr14") or 0.0
        had_ema = ema10 is not None and ((is_long and px > ema10) or (not is_long and px < ema10))
        had_rej = ((ind.get("rej_bull") if is_long else ind.get("rej_bear")) or 0.0) == 1.0
        had_fbreak = ((ind.get("fbreak_bull") if is_long else ind.get("fbreak_bear")) or 0.0) == 1.0
        lvl = _nearest_fade_level(is_long, px, tf0)
        at_lvl = lvl is not None and atr_v > 0 and abs(lvl - px) <= _AT_LEVEL_ATR * atr_v

        entry, stop, target = prop.entry, prop.stop_loss, prop.take_profit
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue
        end = min(i + max_hold, n - 1)
        outcome, exit_px, exit_j = "timeout", candles[end].close, end
        for j in range(i + 1, end + 1):
            hit = _exit_price(prop.direction, candles[j], stop, target)
            if hit is not None:
                exit_px, outcome, exit_j = hit[0], hit[1], j
                break
        raw_r = ((exit_px - entry) if is_long else (entry - exit_px)) / risk
        out.append({
            "trade": BTTrade(
                symbol=symbol, direction=prop.direction.value, regime="rsi_over", strategy="rsi_over",
                confidence=0.7, entry_time=candles[i].ts, entry=round(entry, 6), stop=round(stop, 6),
                target=round(target, 6) if target else entry,
                planned_rr=round(abs((target or entry) - entry) / risk, 2),
                exit_time=candles[exit_j].ts, exit=round(exit_px, 6), outcome=outcome,
                r=round(raw_r - cost_r, 4), bars_held=exit_j - i),
            "had_ema": bool(had_ema), "had_rej": bool(had_rej), "had_fbreak": bool(had_fbreak),
            "at_level": bool(at_lvl),
        })
        block_until = exit_j + cooldown
        i = exit_j + 1
    return out


def compare(entries: list[dict]) -> str:
    # NOTE: the EMA10 close-through can't be tested in a per-bar model (it lands a bar or two AFTER the
    # extreme, once RSI has already left the zone), so it's omitted here. This measures the price-action
    # confirmations that DO form on the extreme bar: the rejection candle and the at-level filter.
    variants = [
        ("RAW extreme (trend-filtered)", [e["trade"] for e in entries]),
        ("+ rejection candle", [e["trade"] for e in entries if e["had_rej"]]),
        ("+ failed break / trap", [e["trade"] for e in entries if e["had_fbreak"]]),
        ("+ rejection OR failed break", [e["trade"] for e in entries if e["had_rej"] or e["had_fbreak"]]),
        ("+ at level", [e["trade"] for e in entries if e["at_level"]]),
    ]
    lines = ["=" * 74,
             "RSI-OVER — do the pro confirmations improve the base fade?",
             "=" * 74,
             f"{'variant':<26}{'n':>5}{'win%':>8}{'expR':>9}{'PF':>7}{'totR':>8}",
             "-" * 74]
    for label, ts in variants:
        if not ts:
            lines.append(f"{label:<26}{0:>5}   (no trades)")
            continue
        m = compute_metrics(ts)
        lines.append(f"{label:<26}{m.n:>5}{m.win_rate * 100:>7.1f}%{m.expectancy_r:>+9.3f}"
                     f"{_pf(m):>7}{m.total_r:>+8.1f}")
    lines.append("=" * 74)
    lines.append("(Higher win%/expR on a filtered subset than BASE = the confirmation adds edge. "
                 "Trend filter is EMA50/200-proxied here; applied to all rows equally.)")
    return "\n".join(lines)


def _targets(session, args) -> list[tuple[str, str, str]]:
    if args.symbols:
        wl = {w.symbol.upper(): w for w in session.scalars(select(WatchItem)).all()}
        out = []
        for sym in (s.strip() for s in args.symbols.split(",") if s.strip()):
            w = wl.get(sym.upper())
            out.append((sym, w.asset_class if w else "forex", args.timeframe or (w.timeframe if w else "1h")))
        return out
    items = session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    return [(w.symbol, w.asset_class, args.timeframe or w.timeframe) for w in items]


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the RSI-Over rejection-candle + at-level filters.")
    ap.add_argument("--symbols", default="", help="comma-separated; default = enabled watchlist")
    ap.add_argument("--timeframe", default="", help="override entry timeframe")
    ap.add_argument("--bars", type=int, default=1500)
    ap.add_argument("--cost-r", type=float, default=0.0)
    args = ap.parse_args()

    session = SessionLocal()
    try:
        settings = get_or_create_settings(session)
        targets = _targets(session, args)
        if not targets:
            print("No symbols. The watchlist is empty — pass --symbols SYM1,SYM2.")
            return
        entries: list[dict] = []
        for sym, ac, tf in targets:
            try:
                broker = get_broker_for(AssetClass(ac), settings.broker_map)
                print(f"… {sym} {tf}…", flush=True)
                e = simulate_rsi_over(broker, sym, AssetClass(ac), tf, bars=args.bars, cost_r=args.cost_r)
                print(f"   {sym}: {len(e)} RSI-Over entries", flush=True)
                entries.extend(e)
            except Exception as exc:  # noqa: BLE001 - one bad symbol shouldn't kill the run
                print(f"   ! {sym} failed: {exc}", flush=True)
        print()
        print(compare(entries))
    finally:
        session.close()


if __name__ == "__main__":
    main()
