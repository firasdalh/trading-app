"""Market-data + broker routes (Milestone 2).

These prove the broker layer works: read a quote, read candles, read the account/positions,
and place a paper order. The test-order endpoint is intentionally locked down — it refuses
to run against a non-paper broker and respects the kill-switch.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.brokers.registry import get_broker_for
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import get_or_create_settings, kill_switch_active
from app.models.enums import AssetClass, OrderSide, OrderType
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
    session: Session = Depends(get_session),
) -> dict:
    """Plain-language 'where is price on the map + do RSI/volume/ATR confirm?' read (INFO only — it
    does not gate trades). Reads multi-TF S/R, the regression channel, and HH/HL structure, then gives
    a short-term + medium-term lean and a level to watch — for the user's Mode-A approve/reject call."""
    from app.agents.context import build_context

    ctx = build_context(session, symbol, asset_class)
    if ctx is None:
        raise HTTPException(status_code=503, detail="market context unavailable (no data)")
    return ctx


@router.get("/market/scenarios")
def market_scenarios(
    symbol: str = Query(...),
    asset_class: AssetClass = Query(AssetClass.STOCK),
    session: Session = Depends(get_session),
) -> dict:
    """AI SCENARIO read (INFO only): the LLM reasons out TWO ranked, scored forward scenarios anchored
    to the deterministic map (real S/R, structure, momentum) — for the user's Mode-A call. It does NOT
    gate trades. Degrades to the deterministic map scenarios when no LLM is configured. The probabilities
    are the model's judgement (they vary run-to-run) — a lean, not a measurement."""
    from app.agents.scenarios import ai_scenarios

    out = ai_scenarios(session, symbol, asset_class)
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
