"""REST endpoints.

Only the skeleton endpoints live here for now; each later PR adds the
endpoints for its own features.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app import repo
from app.api.deps import (
    AppState,
    build_snapshot,
    get_session,
    get_state,
    require_token,
)
from app.models import Settings, Snapshot

router = APIRouter(prefix="/api")


class SessionOut(BaseModel):
    """The X-Local-Token for the served UI.

    No CORS headers are sent, so other sites can call this but cannot read
    the response.
    """

    token: str


class HealthOut(BaseModel):
    status: str
    cycle: int
    last_cycle_at: datetime | None
    sources: dict[str, str]
    errors_24h: int


@router.get("/session")
def get_session_token(state: Annotated[AppState, Depends(get_state)]) -> SessionOut:
    return SessionOut(token=state.local_token)


@router.get("/health")
def health(state: Annotated[AppState, Depends(get_state)]) -> HealthOut:
    return HealthOut(
        status=state.status,
        cycle=state.cycle,
        last_cycle_at=None,
        sources={state.settings.data_source: "ok"},
        errors_24h=0,
    )


@router.get("/snapshot")
def snapshot(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    return build_snapshot(state)


@router.get("/settings")
def read_settings(state: Annotated[AppState, Depends(get_state)]) -> Settings:
    return state.settings


@router.put("/settings", dependencies=[Depends(require_token)])
async def write_settings(request: Request, settings: Settings) -> Settings:
    state = get_state(request)
    session = get_session(request)
    try:
        version = repo.save_settings(session, settings)
    finally:
        session.close()
    state.settings = settings
    state.settings_version = version
    await broadcast_snapshot(state)
    return state.settings


async def broadcast_snapshot(state: AppState) -> None:
    """Push the full snapshot to every connected WebSocket client."""
    if not state.ws_clients:
        return
    payload = '{"type":"snapshot","data":' + build_snapshot(state).model_dump_json() + "}"
    for ws in list(state.ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            state.ws_clients.discard(ws)
