"""Run the backtest from the command line:

    python -m app.backtest                         # the enabled watchlist, each item's own TF
    python -m app.backtest --symbols USOILm,EURUSDm --timeframe 1h --bars 1500
    python -m app.backtest --cost-r 0.05           # haircut ~spread/commission per trade

Measures the DETERMINISTIC engine only (no LLM, no news). See simulator.py for the caveats.
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.backtest.simulator import format_report, simulate_symbol
from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass


def _targets(session, args) -> list[tuple[str, str, str]]:
    """(symbol, asset_class, timeframe) list — from --symbols (asset class looked up in the
    watchlist, default forex) or the enabled watchlist."""
    if args.symbols:
        wl = {w.symbol.upper(): w for w in session.scalars(select(WatchItem)).all()}
        out = []
        for sym in (s.strip() for s in args.symbols.split(",") if s.strip()):
            w = wl.get(sym.upper())
            ac = w.asset_class if w else "forex"
            tf = args.timeframe or (w.timeframe if w else "1h")
            out.append((sym, ac, tf))
        return out
    items = session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    return [(w.symbol, w.asset_class, args.timeframe or w.timeframe) for w in items]


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the deterministic decision engine.")
    ap.add_argument("--symbols", default="", help="comma-separated; default = enabled watchlist")
    ap.add_argument("--timeframe", default="", help="override entry timeframe (e.g. 1h)")
    ap.add_argument("--bars", type=int, default=1500, help="entry-TF history length to replay")
    ap.add_argument("--max-hold", type=int, default=96, help="time-stop in bars")
    ap.add_argument("--cooldown", type=int, default=3, help="bars to wait after a trade closes")
    ap.add_argument("--cost-r", type=float, default=0.0, help="cost per trade in R (0 = gross)")
    ap.add_argument("--risk-pct", type=float, default=0.01, help="per-trade risk for %%-equity DD")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        settings = get_or_create_settings(session)
        targets = _targets(session, args)
        if not targets:
            print("No symbols. The watchlist is empty — pass --symbols SYM1,SYM2.")
            return

        all_trades = []
        for sym, ac, tf in targets:
            try:
                broker = get_broker_for(AssetClass(ac), settings.broker_map)
                print(f"… {sym} {tf} (last {args.bars} bars)…", flush=True)
                tr = simulate_symbol(broker, sym, AssetClass(ac), tf, bars=args.bars,
                                     max_hold=args.max_hold, cooldown=args.cooldown,
                                     cost_r=args.cost_r)
                print(f"   {sym}: {len(tr)} trades", flush=True)
                all_trades.extend(tr)
            except Exception as exc:  # noqa: BLE001 - one bad symbol shouldn't kill the run
                print(f"   ! {sym} failed: {exc}", flush=True)

        print()
        print(format_report(all_trades, title="BACKTEST — deterministic engine",
                            risk_pct=args.risk_pct, cost_r=args.cost_r))
    finally:
        session.close()


if __name__ == "__main__":
    main()
