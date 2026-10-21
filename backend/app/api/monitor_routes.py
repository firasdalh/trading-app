"""Watchlist monitor route - the whole watchlist's 1h / 4h / 1d read-out in one call.

Read-only (see app/agents/market_monitor.py): it fetches candles and computes indicators; it never
opens, sizes or blocks a trade.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.market_monitor import (
    MONITOR_TFS,
    cached_table,
    notes_for,
    read_pair,
)
from app.brokers.registry import get_broker_for
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass

log = get_logger("api.monitor")
router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# News lookups outlive the request that started them (see build()), so the pool is module-level.
# 4 workers keeps the burst against the public calendar polite.
_NEWS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="monitor-news")
_NEWS_WAIT_S = 2.5


def _pairs(session: Session) -> list[tuple[str, str]]:
    """Every watched (symbol, asset_class) once, in watchlist order. A pair watched on two timeframes
    is one row here - the monitor shows every timeframe anyway."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for it in session.scalars(select(WatchItem).order_by(WatchItem.id)).all():
        key = (it.symbol.upper(), it.asset_class)
        if key not in seen:
            seen.add(key)
            out.append((it.symbol, it.asset_class))
    return out


@router.get("")
def monitor(
    refresh: bool = Query(False, description="rebuild now instead of serving the last table "
                                             "(candles still come from the short-lived cache)"),
    session: Session = Depends(get_session),
) -> dict:
    """``{generated_at, timeframes, rows}`` - one row per watched pair. See market_monitor for what
    each field means. A pair whose data could not be read still gets a row, with ``errors``."""
    from app.api.watchlist_routes import _events_soon

    pairs = _pairs(session)
    settings = get_or_create_settings(session)
    broker_map = settings.broker_map

    def build() -> list[dict]:
        # The calendar is a web request per country group whenever its hourly cache is cold: measured
        # 12.7s for this watchlist one after another, and still ~10s in parallel. The table must not
        # wait for that. Lookups start in a background pool WHILE the candle reads run here, the table
        # waits at most _NEWS_WAIT_S for them, and any that are still running carry on and warm the
        # calendar cache - so the next refresh has them instantly. Until then the row says
        # news_pending rather than pretending there is no news.
        news = {sym: _NEWS_POOL.submit(_events_soon, sym) for sym, _ in pairs}
        rows: list[dict] = []
        for symbol, ac in pairs:
            try:
                broker = get_broker_for(AssetClass(ac), broker_map)
            except Exception as exc:  # noqa: BLE001
                # The full row shape, with market_open UNKNOWN (None) - a missing key read as falsy in
                # the UI and showed "closed" for a pair whose real problem is that no broker serves it.
                rows.append({"symbol": symbol, "asset_class": ac, "tfs": {tf: None for tf in MONITOR_TFS},
                             "bias": "mixed", "price": None, "change_pct": None, "range_used": None,
                             "market_open": None, "errors": [f"no broker for this pair: {exc}"],
                             "notes": []})
                continue
            rows.append(read_pair(broker, symbol, ac))
        wait(list(news.values()), timeout=_NEWS_WAIT_S)
        for row in rows:
            fut = news[row["symbol"]]
            row["news_pending"] = not fut.done()
            try:
                row["events_soon"] = fut.result(timeout=0) if fut.done() else None
            except Exception:  # noqa: BLE001 - the calendar is advisory; a failure reads as no news
                row["events_soon"] = None
            row["notes"] = notes_for(row) if any(row.get("tfs", {}).values()) else []
        return rows

    # Refresh = "a table finished after I clicked": it waits for a build already in flight rather
    # than discarding it and starting another.
    rows, at = cached_table(tuple(pairs), build, fresh_after=time.time() if refresh else None)
    return {
        "generated_at": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "timeframes": list(MONITOR_TFS),
        "rows": rows,
    }
