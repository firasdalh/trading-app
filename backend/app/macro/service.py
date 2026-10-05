"""Assembles the macro context and hands it to the rest of the app.

This is the only module the trading engine talks to. It owns the Forex Factory provider instance,
merges in post-release ``actual`` values (which the FF weekly export does not carry), applies the
configured fail-safe behaviour, and freezes a snapshot per decision so the layer can be measured
later instead of merely believed.

Nothing here returns a direction. The single decision it exposes is `news_action`: proceed, warn or
wait.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.data.forexfactory import ForexFactoryCalendarProvider, event_key
from app.data.providers import currencies_for_symbol, get_calendar_provider
from app.macro.engine import MacroWindows, NewsRisk, assess
from app.macro.positioning import (
    PositioningSnapshot,
    PositioningSource,
    UnavailablePositioningSource,
    build_context,
)
from app.models.db import EconomicEventRow, MacroConfig, PositioningSnapshotRow, TradeMacroContext

log = get_logger("macro.service")

# One provider instance for the process: it owns the cache and the rate limiter, so a per-call
# instance would defeat both.
_ff = ForexFactoryCalendarProvider()
_positioning_source: PositioningSource = UnavailablePositioningSource()


def calendar_provider() -> ForexFactoryCalendarProvider:
    return _ff


def set_positioning_source(src: PositioningSource) -> None:
    """Install a positioning feed the user is entitled to use (see app/macro/positioning.py)."""
    global _positioning_source
    _positioning_source = src
    log.info("positioning source installed", extra={"source": getattr(src, "name", "?")})


def get_macro_config(session: Session) -> MacroConfig:
    cfg = session.get(MacroConfig, 1)
    if cfg is None:
        cfg = MacroConfig(id=1)
        session.add(cfg)
        session.commit()
    return cfg


def windows_from(cfg: MacroConfig) -> MacroWindows:
    return MacroWindows(
        warning_minutes=cfg.warning_minutes,
        lock_before_minutes=cfg.lock_before_minutes,
        lock_after_minutes=cfg.lock_after_minutes,
        normalize_minutes=cfg.normalize_minutes,
        warn_score=cfg.warn_score,
        wait_score=cfg.wait_score,
    )


def _legs(symbol: str, asset_class: str | None) -> tuple[str | None, str | None]:
    """(base, quote) currencies for the news gate. A single-leg instrument has no quote."""
    ccys = currencies_for_symbol(symbol, asset_class)
    if not ccys:
        return None, None
    if len(ccys) == 1:
        return ccys[0], None
    # currencies_for_symbol preserves the order of _CCY_TO_COUNTRY, not the pair's, so recover the
    # real base/quote from the ticker when both legs are present.
    s = "".join(c for c in symbol.upper() if c.isalpha())
    core = s[:-1] if s.endswith("M") and len(s) > 4 else s
    base, quote = core[:3], core[-3:]
    if base in ccys and quote in ccys:
        return base, quote
    return ccys[0], ccys[1]


def _enrich_actuals(events, symbol: str, asset_class: str | None, lookback_hours: int = 12):
    """Fill in ``actual`` from the existing TradingView provider.

    The Forex Factory weekly export publishes forecast and previous but never the released value,
    so a post-news read built on it alone would always say "actual: unknown". TradingView's
    calendar does carry it; matching is by currency + the scheduled minute, which is precise enough
    because two releases for one currency rarely share a timestamp.
    """
    released = [e for e in events if e.actual is None]
    if not released:
        return events
    try:
        tv = get_calendar_provider()
        if getattr(tv, "name", "") == "forexfactory":
            return events            # same source, nothing to merge
        ref = tv.get_events(symbol, lookahead_hours=lookback_hours,
                            include_medium=True, asset_class=asset_class)
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort by design
        log.info("actual-value enrichment unavailable", extra={"error": str(exc)})
        return events
    by_slot = {(e.country, e.when.replace(second=0, microsecond=0)): e for e in ref}
    for e in events:
        m = by_slot.get((e.country, e.when.replace(second=0, microsecond=0)))
        if m is not None and m.actual is not None:
            e.actual = m.actual
            if e.forecast is None:
                e.forecast = m.forecast
    return events


_ARTICLES_NOTE = (
    "NEWS FEED UNAVAILABLE - Forex Factory's per-pair stories are only on forexfactory.com, which "
    "is behind a Cloudflare challenge; reading it would mean defeating a technical protection. "
    "Install a permitted news source to populate this."
)


def macro_context(
    session: Session,
    symbol: str,
    asset_class: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """The full macro/news/positioning context for one instrument.

    Never raises: any failure degrades to ``available: False`` plus a note, because a macro layer
    that can take the app down is worse than no macro layer.
    """
    now = now or datetime.now(timezone.utc)
    cfg = get_macro_config(session)
    base, quote = _legs(symbol, asset_class)
    out: dict = {
        "enabled": bool(cfg.enabled),
        "generated_at": now.isoformat(),
        "symbol": symbol,
        "base_currency": base,
        "quote_currency": quote,
        "news_articles": [],
        "news_articles_note": _ARTICLES_NOTE,
    }

    # ---- news risk -------------------------------------------------------
    risk = NewsRisk(base_currency=base, quote_currency=quote)
    if cfg.enabled and cfg.use_news_risk:
        w = windows_from(cfg)
        try:
            ccys = set(currencies_for_symbol(symbol, asset_class))
            allev = _ff.all_events()
            status = _ff.status()      # read AFTER: all_events() may have refreshed it
            if status.ok and allev:
                mine = [e for e in allev if _country_matches(e, ccys)]
                available, stale = True, _is_stale(status, cfg)
                out["source_mode"] = "live"
            else:
                # The live feed is unreadable (it answers 429 readily). Fall back to the calendar
                # already stored by sync_events rather than reporting a quiet week, which looks
                # identical to "no events" and is the most dangerous thing this layer could say.
                mine = _stored_events(session, ccys, now)
                available = bool(mine)
                stale = True
                out["source_mode"] = "cached" if mine else "unavailable"
            if cfg.use_surprise:
                mine = _enrich_actuals(mine, symbol, asset_class)
            risk = assess(mine, base_currency=base, quote_currency=quote, now=now,
                          windows=w, available=available, stale=stale)
        except Exception as exc:  # noqa: BLE001
            log.warning("macro: news risk failed", extra={"symbol": symbol, "error": str(exc)})
            risk = NewsRisk(base_currency=base, quote_currency=quote, available=False,
                            note=f"calendar error: {exc}")
    else:
        risk.note = "news risk disabled in macro config"

    out["news"] = risk.as_dict()
    out["sources"] = {"calendar": _ff.status().as_dict()}
    return _finish(session, out, risk, cfg, symbol, now)


def _country_matches(event, ccys: set[str]) -> bool:
    """CalendarEvent.country is a country code; the caller thinks in currencies."""
    from app.data.providers import _COUNTRY_TO_CCY
    return _COUNTRY_TO_CCY.get((event.country or "").upper(), "") in ccys


def _is_stale(status, cfg: MacroConfig) -> bool:
    """A calendar older than two refresh cycles is stale enough to say so."""
    if status.age_seconds is None:
        return not status.ok
    return status.age_seconds > 2 * 900


def _finish(session: Session, out: dict, risk: NewsRisk, cfg: MacroConfig,
            symbol: str, now: datetime) -> dict:
    """Add positioning, then resolve the single gate decision."""
    # ---- positioning -----------------------------------------------------
    pos_ctx = None
    if cfg.enabled and cfg.use_positioning:
        try:
            live = _positioning_source.get_positioning(symbol)
            if live is not None:
                session.add(PositioningSnapshotRow(
                    symbol=symbol.upper(),
                    long_traders_pct=live.long_traders_pct, short_traders_pct=live.short_traders_pct,
                    long_lots_pct=live.long_lots_pct, short_lots_pct=live.short_lots_pct,
                    trader_count=live.trader_count, lot_count=live.lot_count,
                    source=live.source, source_timestamp=live.source_timestamp,
                ))
                session.commit()
            rows = session.scalars(
                select(PositioningSnapshotRow)
                .where(PositioningSnapshotRow.symbol == symbol.upper())
                .order_by(PositioningSnapshotRow.fetched_at.desc())
                .limit(200)
            ).all()
            snaps = [_row_to_snapshot(r) for r in reversed(rows)]
            latest = snaps[-1] if snaps else None
            pos_ctx = build_context(latest, snaps[:-1], now=now,
                                    stale_after_sec=cfg.positioning_stale_after_sec)
        except Exception as exc:  # noqa: BLE001
            log.warning("macro: positioning failed", extra={"symbol": symbol, "error": str(exc)})
    out["positioning"] = (pos_ctx or build_context(None)).as_dict()

    # ---- the one decision ------------------------------------------------
    action, reason, blocked = _gate(risk, cfg)
    out["action"] = action
    out["gate_reason"] = reason
    out["blocked"] = blocked
    if action != "proceed":
        log.info("macro gate", extra={"symbol": symbol, "action": action,
                                      "score": risk.score, "blocked": blocked, "reason": reason})
    return out


def _row_to_snapshot(r: PositioningSnapshotRow) -> PositioningSnapshot:
    return PositioningSnapshot(
        symbol=r.symbol, long_traders_pct=r.long_traders_pct,
        short_traders_pct=r.short_traders_pct, long_lots_pct=r.long_lots_pct,
        short_lots_pct=r.short_lots_pct, trader_count=r.trader_count, lot_count=r.lot_count,
        source=r.source, source_timestamp=r.source_timestamp, fetched_at=r.fetched_at,
    )


def _gate(risk: NewsRisk, cfg: MacroConfig) -> tuple[str, str, bool]:
    """(action, reason, blocked). The ONLY decision this layer makes, and it has no direction.

    ``blocked`` is separate from ``action`` on purpose. By default the layer annotates without
    holding anything: an unmeasured filter that silently changes which trades get taken destroys
    the very comparison that would show whether it helps. Flip ``block_new_trades`` once the
    ablation says it earns its place.
    """
    if not cfg.enabled or not cfg.use_news_risk:
        return "proceed", "macro layer disabled", False

    if not risk.available:
        if cfg.fail_mode == "block_new_trades":
            return "wait", "NEWS DATA UNAVAILABLE and fail mode is block_new_trades", True
        return "warn", "NEWS DATA UNAVAILABLE - proceeding on technicals only", False

    if risk.action == "wait":
        return "wait", f"HIGH_IMPACT_NEWS_NEARBY: {risk.reason}", bool(cfg.block_new_trades)
    if risk.action == "warn":
        return "warn", f"elevated event risk: {risk.reason}", False
    return "proceed", risk.reason or "no relevant events nearby", False


def record_trade_macro(
    session: Session,
    *,
    symbol: str,
    context: dict,
    proposal_id: int | None = None,
    position_id: int | None = None,
    direction: str | None = None,
    technical_confidence: float | None = None,
) -> TradeMacroContext | None:
    """Freeze the macro state behind one decision. Best-effort: never breaks the trade path."""
    try:
        news = context.get("news") or {}
        pos = context.get("positioning") or {}
        nh = news.get("next_high") or {}
        row = TradeMacroContext(
            proposal_id=proposal_id, position_id=position_id,
            symbol=symbol.upper(), direction=direction,
            technical_confidence=technical_confidence,
            news_risk_score=news.get("score"), news_band=news.get("band"),
            news_action=context.get("action"),
            minutes_to_event=nh.get("minutes_away"),
            event_name=nh.get("title"), event_currency=nh.get("currency"),
            event_impact=nh.get("impact"), event_forecast=nh.get("forecast"),
            event_previous=nh.get("previous"), event_actual=nh.get("actual"),
            crowding_score=pos.get("crowding_score"),
            trader_imbalance=pos.get("trader_imbalance"),
            lot_imbalance=pos.get("lot_imbalance"),
            positioning_source=pos.get("source"),
            blocked_by_news=bool(context.get("blocked")),
            payload=context,
        )
        session.add(row)
        session.commit()
        return row
    except Exception as exc:  # noqa: BLE001
        log.warning("macro: journal write failed", extra={"symbol": symbol, "error": str(exc)})
        session.rollback()
        return None


def sync_events(session: Session) -> dict:
    """Persist the fetched week into ``economic_events``, de-duped by ``event_key``.

    Storage is what makes the layer survive its own data source. The feed answers 429 readily, and
    without a local copy a rate limit would blank the calendar - which, with fail_mode set to
    block_new_trades, would quietly halt the whole desk over a throttled HTTP request.
    """
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "error": None}
    try:
        events = _ff.all_events()
        stats["fetched"] = len(events)
        for e in events:
            key = event_key(e.label, _ccy_of(e), e.when)
            row = session.scalar(select(EconomicEventRow).where(EconomicEventRow.event_key == key))
            released = e.when < datetime.now(timezone.utc)
            if row is None:
                session.add(EconomicEventRow(
                    event_key=key, title=e.label, currency=_ccy_of(e), country=e.country,
                    impact=e.importance, when_utc=e.when, forecast=e.forecast,
                    previous=e.previous, actual=e.actual,
                    status="released" if released else "scheduled",
                    source=_ff.name, source_url=_ff.url,
                ))
                stats["inserted"] += 1
            else:
                # A forecast can be revised during the week; keep the row current without
                # duplicating it.
                row.forecast, row.previous = e.forecast, e.previous
                if e.actual is not None:
                    row.actual = e.actual
                row.status = "released" if released else "scheduled"
                row.fetched_at = datetime.now(timezone.utc)
                stats["updated"] += 1
        cfg = get_macro_config(session)
        cfg.last_sync_at = datetime.now(timezone.utc)
        cfg.last_sync_note = f"{stats['inserted']} new / {stats['updated']} updated"
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        stats["error"] = str(exc)
        log.warning("macro: event sync failed", extra={"error": str(exc)})
    return stats


def _ccy_of(e) -> str:
    from app.data.providers import _COUNTRY_TO_CCY
    c = (e.country or "").upper()
    return _COUNTRY_TO_CCY.get(c, c)


def _stored_events(session: Session, ccys: set[str], now: datetime, hours: int = 48):
    """The locally stored calendar - the fallback when the live feed is unreadable."""
    from datetime import timedelta

    from app.data.providers import CalendarEvent
    lo, hi = now - timedelta(hours=hours), now + timedelta(hours=hours)
    rows = session.scalars(
        select(EconomicEventRow)
        .where(EconomicEventRow.when_utc >= lo, EconomicEventRow.when_utc <= hi)
        .order_by(EconomicEventRow.when_utc)
    ).all()
    return [
        CalendarEvent(label=r.title, when=_aware(r.when_utc), importance=r.impact,
                      country=r.country or r.currency, forecast=r.forecast,
                      previous=r.previous, actual=r.actual)
        for r in rows if (r.currency or "").upper() in ccys
    ]


def _aware(dtv: datetime) -> datetime:
    """SQLite has no timezone type, so a stored UTC timestamp reads back NAIVE.

    Comparing that against a tz-aware `now` raises, which was swallowed by the layer's own
    try/except and surfaced as a permanent "NEWS DATA UNAVAILABLE" — the fallback path silently
    failing in exactly the situation it exists for. Re-attach UTC at the boundary.
    """
    return dtv.replace(tzinfo=timezone.utc) if dtv.tzinfo is None else dtv.astimezone(timezone.utc)
