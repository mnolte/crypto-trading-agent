"""REST endpoints.

Only the skeleton endpoints live here for now; each later PR adds the
endpoints for its own features.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import repo
from app.agent.broker.paper import AMSTERDAM, PaperBroker
from app.agent.engine import Engine, KilledRefusal
from app.api.deps import (
    AppState,
    build_market_client,
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
    sources: dict[str, str] = {}
    for status in state.sources.statuses():
        if not status.enabled:
            sources[status.spec.id] = "disabled"
        elif status.runtime.last_error is not None:
            sources[status.spec.id] = "failing"
        else:
            sources[status.spec.id] = "ok"
    return HealthOut(
        status=state.status,
        cycle=state.cycle,
        last_cycle_at=None,
        sources=sources,
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
    old_source = state.settings.data_source
    state.settings = settings
    state.settings_version = version
    state.sources.set_disabled(settings.disabled_sources)
    if settings.data_source != old_source:
        old_client = state.market_client
        state.market_client = build_market_client(settings, state.sources)
        await old_client.aclose()
    await broadcast_snapshot(state)
    return state.settings


# ------------------------------------------------------------------ engine --


def _engine(state: AppState) -> Engine:
    if state.agent_engine is None:
        raise HTTPException(status_code=503, detail="engine not started")
    return state.agent_engine


@router.post("/agent/pause", dependencies=[Depends(require_token)])
async def agent_pause(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    _engine(state).pause()
    state.status = state.agent_engine.status  # type: ignore[union-attr]
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/agent/resume", dependencies=[Depends(require_token)])
async def agent_resume(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    try:
        _engine(state).resume()
    except KilledRefusal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    state.status = state.agent_engine.status  # type: ignore[union-attr]
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/agent/run-now", dependencies=[Depends(require_token)])
async def agent_run_now(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    started = await _engine(state).run_now()
    if not started:
        raise HTTPException(status_code=409, detail="a cycle is already running")
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/agent/flatten", dependencies=[Depends(require_token)])
async def agent_flatten(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    await _engine(state).flatten()
    state.status = state.agent_engine.status  # type: ignore[union-attr]
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/agent/acknowledge-kill", dependencies=[Depends(require_token)])
async def agent_acknowledge_kill(
    state: Annotated[AppState, Depends(get_state)],
) -> Snapshot:
    engine = _engine(state)
    if engine.status != "killed":
        raise HTTPException(status_code=409, detail="agent is not Killed")
    await engine.acknowledge_kill()
    state.status = engine.status
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/positions/{pair}/close", dependencies=[Depends(require_token)])
async def position_close(pair: str, state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    closed = await _engine(state).close_position(pair)
    if not closed:
        raise HTTPException(status_code=404, detail="no open position on this pair")
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/paper/reset", dependencies=[Depends(require_token)])
async def paper_reset(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    """Start a new paper account; decisions, calls, verdicts and outcomes
    stay tagged with the account they belonged to. Clears Killed too."""
    session = state.session_factory()
    try:
        account = repo.reset_account(
            session,
            state.settings.paper.start_balance_eur,
            day_start_date=datetime.now(AMSTERDAM).date().isoformat(),
        )
        broker = PaperBroker(account, state.settings.paper)
        state.broker = broker
        state.account_id = account.id
        if state.agent_engine is not None:
            state.agent_engine.broker = broker
            state.agent_engine.kill = None
            state.agent_engine.status = "paused"
        repo.add_activity(session, "system", f"paper account reset to #{account.id}")
        session.commit()
    finally:
        session.close()
    await broadcast_snapshot(state)
    return build_snapshot(state)


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
