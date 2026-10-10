"""WebSocket snapshot stream.

Two message types go out over this socket: ``{"type": "snapshot"}`` after each
change, and ``{"type": "alert"}`` for alerts (alerts arrive in a later PR).
Only connections whose Origin is the app itself are accepted, because
browsers do not apply CORS to WebSockets.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketDisconnected

from app.api.deps import AppState, build_snapshot, origin_allowed

router = APIRouter()


@router.websocket("/ws")
async def snapshot_stream(websocket: WebSocket) -> None:
    state: AppState = websocket.app.state.app_state
    if not origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    state.ws_clients.add(websocket)
    try:
        await websocket.send_text(
            '{"type":"snapshot","data":' + build_snapshot(state).model_dump_json() + "}"
        )
        while True:
            # The stream is server->client only; receiving just detects close.
            await websocket.receive()
    except (WebSocketDisconnect, WebSocketDisconnected):
        pass
    finally:
        state.ws_clients.discard(websocket)
