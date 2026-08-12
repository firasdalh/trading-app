"""Market-data + broker routes (Milestone 2).

These prove the broker layer works: read a quote, read candles, read the account/positions,
and place a paper order. The test-order endpoint is intentionally locked down — it refuses
to run against a non-paper broker and respects the kill-switch.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import get_or_create_settings, kill_switch_active
from app.models.enums import AssetClass
from app.models.schemas import (
    AccountState,
    OHLCVSeries,
    OrderRequest,
    OrderResult,
    PositionView,
    Quote,
)

log = get_logger("api.market")
router = APIRouter(prefix="/api", tags=["broker"])


def _broker_for(asset_class: AssetClass, session: Session):
    settings = get_or_create_settings(session)
    return get_broker_for(asset_class, settings.broker_map)


@router.get("/market/quote", response_model=Quote)
def quote(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> Quote:
    broker = _broker_for(asset_class, session)
    return broker.get_quote(symbol)


@router.get("/market/ohlcv", response_model=OHLCVSeries)
def ohlcv(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    timeframe: str = Query("1h"),
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> OHLCVSeries:
    broker = _broker_for(asset_class, session)
    return broker.get_ohlcv(symbol, timeframe, limit)


@router.get("/market/levels")
def market_levels(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> dict:
    """Support/resistance levels across timeframes (1h, 4h, 1d) so a lower-TF chart can also show the
    stronger higher-TF levels. Levels are recent swing pivots nearest the current price. Returns
    ``{price, levels: {tf: [{price, kind}]}}``."""
    from app.agents.indicators import pivot_levels
    from app.data.ohlcv_cache import get_ohlcv_cached

    broker = _broker_for(asset_class, session)
    levels: dict[str, list[dict]] = {}
    ref: float | None = None
    for tf in ("1h", "4h", "1d"):
        try:
            candles = get_ohlcv_cached(broker, symbol, tf, 200).candles
        except Exception as exc:  # noqa: BLE001
            log.warning("levels fetch failed", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
            continue
        if not candles or len(candles) < 10:
            continue
        if ref is None:
            ref = candles[-1].close
        levels[tf] = pivot_levels(candles, ref)
    return {"symbol": symbol, "price": ref, "levels": levels}


@router.get("/market/context")
def market_context(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    timeframe: str = Query("1h"),
    session: Session = Depends(get_session),
) -> dict:
    """Plain-language 'where is price on the map + do RSI/volume/ATR confirm?' read (INFO only — it
    does not gate trades). Reads multi-TF S/R, the regression channel, and HH/HL structure, then gives
    a short-term + medium-term lean and a level to watch — for the user's Mode-A approve/reject call.

    ``timeframe`` is the chart being read: every candle-derived reading is computed on it, so the
    analysis matches the chart in front of you instead of always describing the 1h."""
    from app.agents.context import build_context

    ctx = build_context(session, symbol, asset_class, timeframe)
    if ctx is None:
        raise HTTPException(status_code=503, detail="market context unavailable (no data)")
    return ctx


@router.get("/market/events")
def market_events(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    hours: int = Query(24, ge=1, le=72),
    session: Session = Depends(get_session),
) -> dict:
    """Economic-calendar events that move THIS instrument, from now out to ``hours`` ahead.

    Which countries matter is derived from the symbol (EURUSD -> EU + US, JP225 -> JP, oil -> US),
    so the list is what could move the chart in front of you rather than a generic world calendar.

    Read-only and advisory: the engine already blacks out trading around high-impact events via the
    fundamental agent's stand-aside windows. This endpoint only surfaces the same data so you can
    see it coming.
    """
    from app.data.providers import get_calendar_provider

    now = datetime.now(timezone.utc)
    try:
        events = get_calendar_provider().get_events(
            symbol, lookahead_hours=hours, include_medium=True, asset_class=asset_class.value)
    except Exception as exc:  # noqa: BLE001 — the calendar is advisory; never break the chart over it
        return {"symbol": symbol, "events": [], "error": str(exc)}

    out = []
    for e in events:
        when = e.when if e.when.tzinfo else e.when.replace(tzinfo=timezone.utc)
        mins = (when - now).total_seconds() / 60.0
        # Keep a short lookback: an event that fired 20 minutes ago is still why price is moving.
        if mins < -60 or mins > hours * 60:
            continue
        out.append({
            "label": e.label, "when": when.isoformat(), "importance": e.importance,
            "country": e.country, "minutes_away": round(mins),
            "forecast": e.forecast, "previous": e.previous, "actual": e.actual,
        })
    out.sort(key=lambda x: x["minutes_away"])
    return {"symbol": symbol, "events": out}


@router.get("/market/keylevels")
def market_key_levels(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> dict:
    """Daily reference levels for the chart: prior day / prior week high-low, today's open and
    yesterday's close.

    These come from DAILY candles, which the chart itself never loads — a 5m chart's 400 bars don't
    reach back a week. The engine already scores against the prior day/week levels in its htf_level
    filter, so drawing them makes visible what is already shaping its decisions.
    """
    from app.agents.indicators import reference_levels
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.data.ohlcv_cache import get_ohlcv_cached

    settings = get_or_create_settings(session)
    broker = get_broker_for(asset_class, settings.broker_map)
    try:
        daily = get_ohlcv_cached(broker, symbol, "1d", 60)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"key levels unavailable: {exc}") from exc
    candles = list(daily.candles) if daily else []
    if not candles:
        raise HTTPException(status_code=503, detail="key levels unavailable (no daily data)")

    out = dict(reference_levels(candles))
    # The last daily bar is the CURRENT (still forming) day, so its open is today's open and the
    # bar before it holds yesterday's close.
    out["today_open"] = round(candles[-1].open, 6)
    if len(candles) >= 2:
        out["prior_close"] = round(candles[-2].close, 6)
    return out


@router.get("/market/scenarios")
def market_scenarios(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    timeframe: str = Query("1h"),
    force: bool = Query(False, description="bypass the 15-minute cache and spend tokens on a fresh run"),
    session: Session = Depends(get_session),
) -> dict:
    """AI SCENARIO read (INFO only): the LLM reasons out TWO ranked, scored forward scenarios anchored
    to the deterministic map (real S/R, structure, momentum) — for the user's Mode-A call. It does NOT
    gate trades. Degrades to the deterministic map scenarios when no LLM is configured. The probabilities
    are the model's judgement (they vary run-to-run) — a lean, not a measurement."""
    from app.agents.scenarios import ai_scenarios

    out = ai_scenarios(session, symbol, asset_class, timeframe, force)
    if out is None:
        raise HTTPException(status_code=503, detail="scenarios unavailable (no data)")
    return out


@router.get("/broker/account", response_model=AccountState)
def account(
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> AccountState:
    broker = _broker_for(asset_class, session)
    return broker.get_account()


@router.get("/broker/positions", response_model=list[PositionView])
def positions(
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> list[PositionView]:
    broker = _broker_for(asset_class, session)
    return broker.get_open_positions()


@router.get("/market/symbols")
def symbols(
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> dict:
    """Available tradable symbols for the active broker + asset class (for the UI dropdown).

    Empty list means the broker doesn't enumerate symbols (e.g. sim) — the UI falls back to
    free-text entry.
    """
    broker = _broker_for(asset_class, session)
    try:
        syms = broker.list_symbols(asset_class)
    except Exception as exc:  # noqa: BLE001
        log.warning("list_symbols failed", extra={"broker": broker.name, "error": str(exc)})
        syms = []
    try:
        descriptions = broker.describe_symbols(asset_class)
    except Exception as exc:  # noqa: BLE001
        log.warning("describe_symbols failed", extra={"broker": broker.name, "error": str(exc)})
        descriptions = {}
    return {"broker": broker.name, "asset_class": asset_class.value,
            "symbols": syms, "descriptions": descriptions}


@router.get("/broker/info")
def broker_info(
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> dict:
    broker = _broker_for(asset_class, session)
    return {"name": broker.name, "is_paper": broker.is_paper, "asset_class": asset_class.value}


@router.post("/broker/test-order", response_model=OrderResult)
def test_order(
    request: OrderRequest,
    session: Session = Depends(get_session),
) -> OrderResult:
    """DEV/QA: place a single order to prove paper execution works.

    Hard guards: refuses if the kill-switch is active or the resolved broker is NOT paper.
    This bypasses the agent/risk pipeline by design — it is only for verifying the broker
    plumbing, never for real trading.
    """
    if kill_switch_active(session):
        raise HTTPException(status_code=423, detail="Kill-switch active — no orders accepted")
    broker = _broker_for(request.asset_class, session)
    if not broker.is_paper:
        raise HTTPException(
            status_code=403,
            detail="test-order is paper-only; resolved broker is live",
        )
    log.warning("test-order (paper) submitted", extra={"symbol": request.symbol, "qty": request.qty})
    return broker.submit_order(request)
