"""Watchlist + autonomous-scanner routes (M+).

The scanner periodically analyzes these pairs and (in Modes B/C) auto-executes risk-approved
setups; in Mode A it queues proposals for approval. Auto-close at stop/target is handled by
the Monitor.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.scanner import get_or_create_scan_config, run_scan
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass
from app.models.schemas import ConditionalSuggestion

log = get_logger("api.watchlist")

# How many of the strongest ACTIONABLE candidates get the (costlier) LLM review in a deep scan.
# Bounds the LLM calls per scan; watch/forming rows aren't openable, so they stay deterministic.
_DEEP_MAX = 6


def _events_soon(symbol: str, hours: int = 8) -> str | None:
    """A SOFT heads-up of upcoming MEDIUM/high-impact events (e.g. a Fed speech) for the symbol —
    DISPLAY ONLY. It never gates a trade; the hard stand-aside remains high-impact only."""
    from datetime import datetime, timezone

    from app.data.providers import get_calendar_provider

    now = datetime.now(timezone.utc)
    try:
        evs = get_calendar_provider().get_events(symbol, lookahead_hours=hours, include_medium=True)
    except Exception:  # noqa: BLE001 - a calendar hiccup must never break the scan
        return None
    upcoming = []
    for e in evs:
        when = e.when if e.when.tzinfo else e.when.replace(tzinfo=timezone.utc)
        mins = int((when - now).total_seconds() / 60)
        if 0 <= mins <= hours * 60:
            upcoming.append((mins, e))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    fmt = lambda m: f"~{m}m" if m < 90 else f"~{round(m / 60, 1)}h"  # noqa: E731
    return " · ".join(f"{e.label} {fmt(m)} ({e.importance})" for m, e in upcoming[:3])
router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchItemRequest(BaseModel):
    symbol: str
    asset_class: AssetClass = AssetClass.FOREX
    timeframe: str = "1h"


class WatchItemView(BaseModel):
    id: int
    symbol: str
    asset_class: str
    timeframe: str
    enabled: bool
    recommended: bool = False


class ScanConfigRequest(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = Field(None, ge=20, le=3600)


class WatchlistResponse(BaseModel):
    items: list[WatchItemView]
    scan_enabled: bool
    interval_seconds: int
    last_scan_at: str | None = None


def _iso_utc(dt) -> str | None:
    """Stamp naive (SQLite) timestamps as UTC so the browser doesn't read them as local time."""
    if dt is None:
        return None
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _response(session: Session) -> WatchlistResponse:
    cfg = get_or_create_scan_config(session)
    items = session.scalars(select(WatchItem).order_by(WatchItem.id)).all()
    return WatchlistResponse(
        items=[WatchItemView(id=i.id, symbol=i.symbol, asset_class=i.asset_class,
                             timeframe=i.timeframe, enabled=i.enabled,
                             recommended=bool(getattr(i, "recommended", False))) for i in items],
        scan_enabled=cfg.enabled,
        interval_seconds=cfg.interval_seconds,
        last_scan_at=_iso_utc(cfg.last_scan_at),
    )


@router.get("", response_model=WatchlistResponse)
def get_watchlist(session: Session = Depends(get_session)) -> WatchlistResponse:
    return _response(session)


@router.post("", response_model=WatchlistResponse)
def add_item(req: WatchItemRequest, session: Session = Depends(get_session)) -> WatchlistResponse:
    from sqlalchemy import func

    symbol = req.symbol.strip()  # preserve broker casing (e.g. XAUUSDm)
    exists = session.scalar(
        select(WatchItem).where(func.lower(WatchItem.symbol) == symbol.lower(),
                                WatchItem.asset_class == req.asset_class.value,
                                WatchItem.timeframe == req.timeframe)
    )
    if not exists:
        session.add(WatchItem(symbol=symbol, asset_class=req.asset_class.value,
                              timeframe=req.timeframe, enabled=True))
        session.commit()
        log.info("watch item added", extra={"symbol": symbol})
    return _response(session)


@router.delete("/{item_id}", response_model=WatchlistResponse)
def remove_item(item_id: int, session: Session = Depends(get_session)) -> WatchlistResponse:
    item = session.get(WatchItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="watch item not found")
    session.delete(item)
    session.commit()
    return _response(session)


@router.post("/scan-config", response_model=WatchlistResponse)
def set_scan_config(req: ScanConfigRequest, session: Session = Depends(get_session)) -> WatchlistResponse:
    cfg = get_or_create_scan_config(session)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.interval_seconds is not None:
        cfg.interval_seconds = req.interval_seconds
    session.commit()
    log.warning("scan config updated", extra={"enabled": cfg.enabled, "interval": cfg.interval_seconds})
    return _response(session)


@router.post("/scan-now")
def scan_now(session: Session = Depends(get_session)) -> dict:
    """Run one scan immediately (regardless of the interval)."""
    return run_scan(session)


class OpportunityView(BaseModel):
    symbol: str
    asset_class: str
    timeframe: str
    direction: str
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    rr: float | None = None
    confidence: float
    watch: bool
    rationale: str
    risk_approved: bool
    risk_decision: str | None = None
    risk_reason: str | None = None
    already_open: bool = False
    conditional: ConditionalSuggestion | None = None  # break-entry to arm if blocked by structure
    lots: float | None = None       # the size that would be opened (Risk Manager, in lots)
    risk_usd: float | None = None   # $ risk at that size
    reward_usd: float | None = None # $ reward to target (risk × R)
    regime: str | None = None       # market regime read (trending / ranging / volatile / moderate)
    strategy: str | None = None     # strategy it permits (trend / mean_reversion / stand_aside)
    events_soon: str | None = None  # SOFT heads-up: upcoming medium/high-impact events (display only)


@router.get("/opportunities", response_model=list[OpportunityView])
def opportunities(
    deep: bool = Query(True, description="Re-judge the actionable candidates with the LLM reviewer "
                                         "so the headline setups match 'Run analysis'."),
    timeframe: str | None = Query(None, description="Scan every pair on THIS timeframe (e.g. 15m/4h) "
                                                    "instead of its own; blank = each pair's own."),
    session: Session = Depends(get_session),
) -> list[OpportunityView]:
    """Scan every enabled watchlist pair and return the setups ranked best-first — actionable &
    risk-approved on top, then forming ('watch'), then the rest. Read-only: nothing is opened.

    Two passes: every pair is ranked by the cheap deterministic engine, then (when ``deep``) the
    few ACTIONABLE candidates are re-run through the LLM reviewer so the setups you'd actually open
    show the SAME verdict as 'Run analysis' / the Open button — the LLM can veto a deterministic
    chase. Forming/'watch' rows stay deterministic; they aren't openable, so no quota is spent."""
    from app.agents.llm import llm_available
    from app.agents.pipeline import preview_symbol
    from app.agents.scanner import expire_stale_proposals
    from app.risk.service import ScanCache, live_broker_positions

    expire_stale_proposals(session)  # so the scan reflects a clean pending list
    # One open-book fetch for the whole scan: serves both the already-open check and (primed into
    # the ScanCache) every per-symbol risk assessment below, instead of one broker call per symbol.
    live = live_broker_positions(session)
    open_syms = {p.symbol.upper() for p in live}
    cache = ScanCache(open_book=[(p.symbol, p.direction) for p in live])
    items = session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    tf_override = timeframe if isinstance(timeframe, str) and timeframe else None

    # AI-DECIDER scan: when the "AI decides" toggle is ON, the AI evaluates EVERY pair directly (one
    # LLM call each — on-demand only, "Scan watchlist" isn't polled) so it can surface setups the
    # deterministic trend-only filter would reject. Otherwise the cheap deterministic engine ranks the
    # list and the LLM only re-judges the few actionable candidates (pass 2). The AI decider (analyst ->
    # brief -> AI judges open/arm/skip) is wired inside preview_symbol; here we just run it per-pair.
    settings = get_or_create_settings(session)
    ai_active = (settings.ai_review_enabled and not settings.st_band_mode and llm_available())

    def _view(it: WatchItem, prop, dec, tf: str) -> OpportunityView:
        rr = None
        if prop.entry and prop.stop_loss and prop.take_profit:
            risk = abs(prop.entry - prop.stop_loss)
            rr = round(abs(prop.take_profit - prop.entry) / risk, 2) if risk else None
        # The size the Risk Manager would open, and the $ risk/reward at it (already computed in
        # assess — no extra broker call). reward = risk × R.
        lots = round(dec.approved_qty, 2) if dec.approved_qty else None
        risk_usd = round(dec.risk_amount, 2) if dec.risk_amount else None
        reward_usd = round(risk_usd * rr, 2) if (risk_usd and rr) else None
        return OpportunityView(
            symbol=it.symbol, asset_class=it.asset_class, timeframe=tf,
            direction=prop.direction.value, entry=prop.entry, stop_loss=prop.stop_loss,
            take_profit=prop.take_profit, rr=rr, confidence=prop.confidence, watch=prop.watch,
            rationale=prop.rationale, risk_approved=dec.approved, risk_decision=dec.decision.value,
            risk_reason=dec.reason, already_open=it.symbol.upper() in open_syms,
            conditional=prop.conditional, lots=lots, risk_usd=risk_usd, reward_usd=reward_usd,
            regime=prop.regime, strategy=prop.strategy, events_soon=_events_soon(it.symbol),
        )

    # Pass 1 — score every pair. AI-led: the AI decides each (use_llm=True). Else: the cheap
    # deterministic engine ranks the list (no LLM quota).
    out: list[OpportunityView] = []
    meta: list[WatchItem] = []  # WatchItem parallel to `out`, so pass 2 can re-run a row by index
    for it in items:
        tf = tf_override or it.timeframe   # global override, or the pair's own
        try:
            # AI active: deterministic reads (read_llm=False) + AI decision (use_llm=True) -> 1 AI call
            # per pair, not 3. Else: fully deterministic, cheap.
            prop, dec = preview_symbol(session, it.symbol, AssetClass(it.asset_class), tf,
                                       use_llm=ai_active, read_llm=False if ai_active else None, cache=cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("opportunity preview failed", extra={"symbol": it.symbol, "error": str(exc)})
            continue
        out.append(_view(it, prop, dec, tf))
        meta.append(it)

    # Pass 2 — only when NOT AI-led (AI-led already decided each pair with the LLM in pass 1). Here the
    # deterministic engine ranked the list, so re-judge the ACTIONABLE candidates with the LLM
    # reviewer, capped at the strongest _DEEP_MAX so a manual scan stays fast. A veto turns the row
    # into NO_TRADE with confidence 0, so it drops out of the actionable tier exactly like Run analysis.
    if deep and not ai_active and llm_available():
        idxs = [i for i, o in enumerate(out)
                if o.direction in ("long", "short") and o.risk_approved and not o.already_open]
        idxs.sort(key=lambda i: -out[i].confidence)
        for i in idxs[:_DEEP_MAX]:
            it = meta[i]
            tf = tf_override or it.timeframe
            try:
                prop, dec = preview_symbol(session, it.symbol, AssetClass(it.asset_class),
                                           tf, use_llm=True, cache=cache)
            except Exception as exc:  # noqa: BLE001
                log.warning("opportunity LLM review failed",
                            extra={"symbol": it.symbol, "error": str(exc)})
                continue
            out[i] = _view(it, prop, dec, tf)

    def rank(o: OpportunityView):
        actionable = o.direction in ("long", "short")
        if actionable and o.risk_approved and not o.already_open:
            tier = 0  # open it now
        elif actionable:
            tier = 1  # a real setup with levels, but blocked (risk/cooldown/paused) or already open
        elif o.watch:
            tier = 2  # forming — not ready yet
        else:
            tier = 3  # no trade
        return (tier, -o.confidence, -(o.rr or 0))

    out.sort(key=rank)
    return out
