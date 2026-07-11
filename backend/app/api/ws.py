"""WebSocket handlers for live updates.

`/ws/quotes` streams the latest price for a symbol every couple of seconds (sourced from the
active broker/data provider — synthetic offline, real when keys are set). Kept deliberately
simple and defensive: any error closes the socket cleanly rather than crashing the server.

Agent/event streaming can be layered on the same pattern in later milestones.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.brokers.registry import get_broker_for
from app.core.database import session_scope
from app.core.logging import get_logger
from app.core.state import get_or_create_settings
from app.models.enums import AssetClass

log = get_logger("api.ws")
router = APIRouter()

_INTERVAL_SECONDS = 2.0


@router.websocket("/ws/quotes")
async def quotes_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    symbol = websocket.query_params.get("symbol", "AAPL")
    asset_class_raw = websocket.query_params.get("asset_class", "stock")
    try:
        asset_class = AssetClass(asset_class_raw)
    except ValueError:
        asset_class = AssetClass.STOCK

    with session_scope() as session:
        broker_map = get_or_create_settings(session).broker_map
    broker = get_broker_for(asset_class, broker_map)

    try:
        while True:
            # Build the payload separately from the send: a BROKER error becomes an "error" message
            # (socket stays alive), but the SEND itself is done once — a client disconnect during the
            # send raises WebSocketDisconnect and unwinds to the clean handler below (no double-send,
            # which was the "Cannot call send once a close message has been sent" RuntimeError).
            try:
                quote = broker.get_quote(symbol)
                payload = {"type": "quote", "symbol": quote.symbol,
                           "price": quote.price, "ts": quote.ts.isoformat()}
            except Exception as exc:  # noqa: BLE001 - transient broker/data error; keep streaming
                payload = {"type": "error", "message": str(exc)}
            await websocket.send_json(payload)
            await asyncio.sleep(_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        log.info("quotes ws disconnected", extra={"symbol": symbol})
    except Exception as exc:  # noqa: BLE001
        log.warning("quotes ws closed on error", extra={"symbol": symbol, "error": str(exc)})
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closing/closed; nothing to do
            pass
