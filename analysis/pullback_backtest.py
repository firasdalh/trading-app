"""Enhancement test (A): pullback/armed re-entry vs chasing at market.

For every actionable setup that ALSO carries a conditional (i.e. the engine flagged a better-priced
'wait for the break / pullback to value' entry), we compare two ways to trade it:
  - MARKET (chase):   enter at the signal-bar close, follow to the market stop/target.
  - PULLBACK (wait):  wait for the conditional's trigger within a validity window; if it fills, enter
                      there with the conditional's (tighter) stop/target; if price closes back through
                      the conditional stop first -> invalidated (no trade); if the trigger never comes
                      -> expired (no trade = a MISSED setup).
Reports win%/expectancy for each, the pullback FILL RATE, and the MISSED-WINNER cost (what the expired
setups would have done at market) — plus an in-sample/out-of-sample split so it isn't curve-fit.

Real broker data via broker_map. Run with uvicorn stopped:
    PYTHONPATH=backend python analysis/pullback_backtest.py
"""
from __future__ import annotations

import bisect
import sys
from pathlib import Path

from sqlalchemy import select

from app.agents.orchestrator import _deterministic_decision
from app.agents.pipeline import _timeframes_for
from app.agents.technical import run_technical
from app.backtest.engine import _exit_price
from app.backtest.simulator import _WINDOW, _neutral_fundamental, _simulate_trade, split_by_time
from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass, Direction
from app.models.schemas import OHLCVSeries

BARS = 1500
CONTEXT_BARS = 600
MAX_HOLD = 96
COOLDOWN = 3
COST_R = 0.05
TRIGGER_WINDOW = 24     # bars the conditional has to trigger before it expires (~1 day on 1h)
HOLDOUT = 0.30
OUT = Path(__file__).with_name("pullback_backtest.md")


def _sim_conditional(candles, i, cond):
    """Wait for the conditional to trigger within TRIGGER_WINDOW, then follow to its stop/target.
    Returns (status, r): status in {triggered, expired, invalidated, badlevels}."""
    if cond.stop_loss is None or cond.take_profit is None:
        return ("badlevels", None)
    trig, ot = cond.trigger_price, cond.order_type
    is_long = ot.startswith("buy")
    fill_j = None
    end_w = min(i + 1 + TRIGGER_WINDOW, len(candles))
    for j in range(i + 1, end_w):
        bar = candles[j]
        # pre-trigger invalidation: a confirmed close back through the stop = thesis broke first.
        if (is_long and bar.close <= cond.stop_loss) or ((not is_long) and bar.close >= cond.stop_loss):
            return ("invalidated", None)
        if ot == "buy_stop":
            hit = bar.high >= trig
        elif ot == "sell_stop":
            hit = bar.low <= trig
        elif ot == "buy_limit":
            hit = bar.low <= trig
        else:  # sell_limit
            hit = bar.high >= trig
        if hit:
            fill_j = j; break
    if fill_j is None:
        return ("expired", None)
    entry, stop, target = trig, cond.stop_loss, cond.take_profit
    risk = abs(entry - stop)
    if risk <= 0:
        return ("badlevels", None)
    d = Direction.LONG if is_long else Direction.SHORT
    end = min(fill_j + MAX_HOLD, len(candles) - 1)
    outcome, exit_px = None, candles[end].close
    for k in range(fill_j + 1, end + 1):
        hit = _exit_price(d, candles[k], stop, target)
        if hit is not None:
            exit_px, outcome = hit[0], hit[1]; break
    if outcome is None:
        outcome = "timeout"
    r = ((exit_px - entry) if is_long else (entry - exit_px)) / risk - COST_R
    return ("triggered", round(r, 4))


def run_symbol(broker, symbol, ac, tf):
    tfs = _timeframes_for(tf)
    series = {}
    for t in tfs:
        limit = BARS if t == tf else CONTEXT_BARS
        try:
            sd = broker.get_ohlcv(symbol, t, limit=limit)
            series[t] = list(sd.candles) if sd and sd.candles else []
        except Exception:  # noqa: BLE001
            series[t] = []
    entry = series.get(tf, [])
    if len(entry) < _WINDOW + 5:
        return []
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(symbol)
    n = len(entry)
    rows = []   # (entry_time, has_cond, r_market, cond_status, r_cond)
    i = _WINDOW - 1
    while i < n:
        t_i = entry[i].ts
        window, ok = [], True
        for t in tfs:
            w = (entry[max(0, i - _WINDOW + 1): i + 1] if t == tf
                 else series[t][max(0, bisect.bisect_right(ts_index[t], t_i) - _WINDOW):
                                bisect.bisect_right(ts_index[t], t_i)])
            if not w:
                ok = False; break
            window.append(OHLCVSeries(symbol=symbol, timeframe=t, candles=w))
        if not ok:
            i += 1; continue
        tech = run_technical(symbol, window, use_llm=False)
        prop = _deterministic_decision(symbol, ac, tf, tech, fund, now=t_i, trend_only=True)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1; continue
        mkt = _simulate_trade(symbol, entry, i, prop, max_hold=MAX_HOLD, cost_r=COST_R)
        if mkt is None:
            i += 1; continue
        if prop.conditional is not None:
            status, r_cond = _sim_conditional(entry, i, prop.conditional)
        else:
            status, r_cond = ("none", None)
        rows.append((entry[i].ts, prop.conditional is not None, mkt.r, status, r_cond))
        i = i + max(1, mkt.bars_held) + COOLDOWN
    return rows


def _stats(rs):
    rs = [r for r in rs if r is not None]
    if not rs:
        return (0, 0.0, 0.0, 0.0)
    w = sum(1 for r in rs if r > 0)
    return (len(rs), 100 * w / len(rs), sum(rs) / len(rs), sum(rs))


def _line(tag, rs):
    n, wr, ex, tot = _stats(rs)
    return f"| {tag} | {n} | {wr:.1f}% | {ex:+.3f} | {tot:+.1f} |"


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    syms = [(it.symbol, it.asset_class, it.timeframe)
            for it in s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()]
    s.close()

    ALL = []
    for sym, ac, tf in syms:
        try:
            r = run_symbol(get_broker_for(AssetClass(ac), bmap), sym, AssetClass(ac), tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}"); continue
        ALL += r
        print(f"  {sym}: {len(r)} setups ({sum(1 for x in r if x[1])} with a conditional)")
        sys.stdout.flush()

    # Streams.
    market_all = [x[2] for x in ALL]                                   # baseline: chase everything
    withc = [x for x in ALL if x[1]]                                   # setups carrying a conditional
    market_sub = [x[2] for x in withc]                                 # chase, on those setups
    pull_filled = [x[4] for x in withc if x[3] == "triggered"]         # pullback fills
    triggered = sum(1 for x in withc if x[3] == "triggered")
    expired = sum(1 for x in withc if x[3] == "expired")
    invalid = sum(1 for x in withc if x[3] == "invalidated")
    missed_market = [x[2] for x in withc if x[3] in ("expired", "invalidated")]  # what waiting missed

    # "PULLBACK STRATEGY" = take the conditional when one exists (only the fills count), else market.
    pull_strategy = pull_filled + [x[2] for x in ALL if not x[1]]

    L = ["# Enhancement (A) — pullback/armed re-entry vs chasing at market", ""]
    L.append(f"{len(ALL)} actionable setups; **{len(withc)}** carried a conditional (better-priced "
             "pullback/break entry). Only those are the head-to-head; at-value setups are taken at "
             "market either way.")
    L += ["", "## Head-to-head on the setups that carry a conditional", "",
          "| entry method | trades | win% | expectancy R | total R |", "|---|---|---|---|---|",
          _line("MARKET (chase)", market_sub),
          _line("PULLBACK (fills only)", pull_filled)]
    fill_rate = 100 * triggered / len(withc) if withc else 0
    L += ["", f"- **Fill rate:** {triggered}/{len(withc)} triggered ({fill_rate:.0f}%); "
          f"{expired} expired, {invalid} invalidated (never taken).",
          f"- **Cost of waiting (missed setups):** the {expired+invalid} that never filled would have "
          f"done **{_stats(missed_market)[2]:+.3f}R** at market ({_stats(missed_market)[1]:.0f}% win) — "
          "that's the winners you skip by waiting."]

    L += ["", "## Whole-system: chase-everything vs pullback-where-available", "",
          "| strategy | trades | win% | expectancy R | total R |", "|---|---|---|---|---|",
          _line("MARKET (all at market)", market_all),
          _line("PULLBACK (cond when available)", pull_strategy)]

    # Walk-forward on the head-to-head (needs entry_time; rebuild lightweight BTTrade-likes).
    class _T:  # minimal for split_by_time
        def __init__(self, t, r): self.entry_time, self.r = t, r
    mk = [_T(x[0], x[2]) for x in withc]
    pl = [_T(x[0], x[4]) for x in withc if x[3] == "triggered"]
    mk_is, mk_oos = split_by_time(mk, HOLDOUT)
    pl_is, pl_oos = split_by_time(pl, HOLDOUT)
    L += ["", f"## In-sample vs out-of-sample (last {int(HOLDOUT*100)}% held out) — head-to-head", "",
          "| window | MARKET win% / exp | PULLBACK win% / exp |", "|---|---|---|"]
    for label, mkw, plw in [("in-sample", mk_is, pl_is), ("OUT-OF-SAMPLE", mk_oos, pl_oos)]:
        _, mwr, mex, _2 = _stats([t.r for t in mkw])
        _, pwr, pex, _3 = _stats([t.r for t in plw])
        L.append(f"| {label} | {mwr:.0f}% / {mex:+.3f} | {pwr:.0f}% / {pex:+.3f} |")

    # Verdict.
    _, mwr, mex, _2 = _stats(market_sub)
    _, pwr, pex, _3 = _stats(pull_filled)
    L += ["", "## Verdict", ""]
    better = pex > mex
    L.append(f"- On the conditional-carrying setups, pullback expectancy **{pex:+.3f}R** vs market "
             f"**{mex:+.3f}R**, win% **{pwr:.0f}%** vs **{mwr:.0f}%** -> "
             + ("**pullback wins per trade.**" if better else "**market (chase) is as good or better.**"))
    L.append(f"- But pullback only fills {fill_rate:.0f}% of the time; the rest expire. Net effect on "
             "the WHOLE system is the 'chase-everything vs pullback-where-available' table above — "
             "that's the number that matters for the account.")
    L.append("- Decide on the WHOLE-SYSTEM total R and the OOS row, not the per-trade edge alone: a "
             "higher per-fill win rate that skips too many winners can lower total return.")
    L.append("")
    L.append("_Deterministic engine only; costs 0.05R flat; trigger window 24 bars; conservative "
             "intrabar fills. Small samples per split — read directionally._")
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
