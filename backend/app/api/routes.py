"""REST endpoints.

Only the skeleton endpoints live here for now; each later PR adds the
endpoints for its own features.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app import repo, settings_service
from app.agent.broker.paper import PaperBroker
from app.agent.engine import Engine, KilledRefusal, PendingRefusal
from app.agent.market.base import MarketDataError
from app.api import errors as api_errors
from app.api.deps import (
    AppState,
    broadcast_settings,
    build_market_client,
    build_snapshot,
    get_session,
    get_state,
    require_token,
)
from app.db import AMSTERDAM, ErrorRow, utcnow
from app.models import (
    ErrorOut,
    ResolveErrorIn,
    Settings,
    SettingsMetaOut,
    SettingsPreviewOut,
    SettingsSaveIn,
    SettingsSaveOut,
    Snapshot,
)

router = APIRouter(prefix="/api")


class SessionOut(BaseModel):
    """The X-Local-Token for the served UI.

    No CORS headers are sent, so other sites can call this but cannot read
    the response.
    """

    token: str


class MaintenancePruneOut(BaseModel):
    cutoff: datetime
    decisions_pruned: int
    llm_calls_pruned: int


class HealthOut(BaseModel):
    status: str
    cycle: int
    last_cycle_at: datetime | None
    sources: dict[str, str]
    source_age_s: dict[str, int | None]
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
    session = state.session_factory()
    try:
        now = utcnow()
        errors_24h = repo.error_count_since(session, datetime.now(AMSTERDAM) - timedelta(hours=24))
        source_age_s = {
            source: (
                max(0, int((now - newest).total_seconds()))
                if (newest := repo.newest_market_context(session, source)) is not None
                else None
            )
            for source in ("okx", "deribit")
        }
    finally:
        session.close()
    engine = state.agent_engine
    return HealthOut(
        status=engine.status if engine is not None else state.status,
        cycle=engine.cycle if engine is not None else state.cycle,
        last_cycle_at=engine.last_cycle_at if engine is not None else None,
        sources=sources,
        source_age_s=source_age_s,
        errors_24h=errors_24h,
    )


@router.get("/errors")
def errors(
    state: Annotated[AppState, Depends(get_state)], resolved: bool = False
) -> list[ErrorOut]:
    session = state.session_factory()
    try:
        rows = session.scalars(
            select(ErrorRow)
            .where(
                ErrorRow.resolved_at.is_not(None) if resolved else ErrorRow.resolved_at.is_(None)
            )
            .order_by(ErrorRow.last_at.desc())
        ).all()
        return [
            ErrorOut(
                id=row.id,
                first_at=repo.aware_utc(row.first_at),
                last_at=repo.aware_utc(row.last_at),
                pair=row.pair,
                text=row.text,
                affected_decision_or_fill=row.affected_decision_or_fill,
                resolved_at=repo.aware_utc(row.resolved_at) if row.resolved_at else None,
                resolution_note=row.resolution_note,
            )
            for row in rows
        ]
    finally:
        session.close()


@router.put("/errors/{error_id}/resolve", dependencies=[Depends(require_token)])
def resolve_error(
    error_id: int,
    body: ResolveErrorIn,
    state: Annotated[AppState, Depends(get_state)],
) -> ErrorOut:
    if not body.note.strip():
        raise HTTPException(status_code=422, detail="a resolution note is required")
    session = state.session_factory()
    try:
        row = session.get(ErrorRow, error_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown error")
        row.resolved_at = utcnow()
        row.resolution_note = body.note.strip()
        session.commit()
        return ErrorOut(
            id=row.id,
            first_at=repo.aware_utc(row.first_at),
            last_at=repo.aware_utc(row.last_at),
            pair=row.pair,
            text=row.text,
            affected_decision_or_fill=row.affected_decision_or_fill,
            resolved_at=repo.aware_utc(row.resolved_at),
            resolution_note=row.resolution_note,
        )
    finally:
        session.close()


@router.post("/maintenance/prune", dependencies=[Depends(require_token)])
def maintenance_prune(state: Annotated[AppState, Depends(get_state)]) -> MaintenancePruneOut:
    """Clear raw LLM payloads older than 90 days; parsed records stay."""
    cutoff = utcnow() - timedelta(days=90)
    session = state.session_factory()
    try:
        decisions, calls = repo.prune_raw_llm_payloads(session, cutoff)
        session.commit()
        return MaintenancePruneOut(
            cutoff=cutoff,
            decisions_pruned=decisions,
            llm_calls_pruned=calls,
        )
    finally:
        session.close()


@router.get("/snapshot")
def snapshot(state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    return build_snapshot(state)


@router.get("/settings")
def read_settings(state: Annotated[AppState, Depends(get_state)]) -> Settings:
    return state.settings


@router.get("/settings/meta", response_model=SettingsMetaOut)
def settings_meta(request: Request) -> SettingsMetaOut:
    state = get_state(request)
    return settings_service.settings_meta(state.settings_version)


@router.post("/settings/preview", response_model=SettingsPreviewOut)
def settings_preview(request: Request, draft: dict[str, Any]) -> SettingsPreviewOut:
    """Money, tags and errors for an unsaved draft; saves nothing."""
    state = get_state(request)
    session = get_session(request)
    try:
        broker = state.broker
        if broker is not None:
            equity = broker.equity()
            day_start = broker.day_start_equity
            peak = broker.peak_equity
        else:
            start = state.settings.paper.start_balance_eur
            equity = day_start = peak = start
        return settings_service.preview(
            session,
            draft,
            equity=equity,
            day_start_equity=day_start,
            peak_equity=peak,
        )
    finally:
        session.close()


@router.put("/settings", dependencies=[Depends(require_token)])
async def write_settings(request: Request, body: SettingsSaveIn) -> SettingsSaveOut:
    state = get_state(request)
    session = get_session(request)
    try:
        try:
            version = settings_service.save(
                session,
                body.settings,
                reason=body.reason,
                source="screen",
                confirm_looser=body.confirm_looser,
            )
        except settings_service.ReasonInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except settings_service.LooserNeedsConfirm as exc:
            return api_errors.api_error(  # type: ignore[return-value]
                409,
                "This change loosens a limit — confirm to save it.",
                code="looser_needs_confirm",
                extra={"changes": exc.changes},
            )
    finally:
        session.close()
    old_source = state.settings.data_source
    state.settings = body.settings
    state.settings_version = version
    state.sources.set_disabled(body.settings.disabled_sources)
    if body.settings.data_source != old_source:
        old_client = state.market_client
        state.market_client = build_market_client(body.settings, state.sources)
        if state.agent_engine is not None:
            state.agent_engine.client = state.market_client
            if state.agent_engine.openai_decider is not None:
                state.agent_engine.openai_decider.client = state.market_client  # type: ignore[attr-defined]
            if state.agent_engine.shadows is not None:
                state.agent_engine.shadows.set_client(state.market_client)
        await old_client.aclose()
    await broadcast_snapshot(state)
    await broadcast_settings(state)
    return SettingsSaveOut(version=version, settings=state.settings)


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
    try:
        started = await _engine(state).run_now()
    except KilledRefusal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.post("/decisions/{decision_id}/approve", dependencies=[Depends(require_token)])
async def approve_decision(
    decision_id: str, state: Annotated[AppState, Depends(get_state)]
) -> Snapshot:
    try:
        await _engine(state).approve_pending(decision_id)
    except PendingRefusal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/decisions/{decision_id}/veto", dependencies=[Depends(require_token)])
async def veto_decision(
    decision_id: str, state: Annotated[AppState, Depends(get_state)]
) -> Snapshot:
    try:
        await _engine(state).veto_pending(decision_id)
    except PendingRefusal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await broadcast_snapshot(state)
    return build_snapshot(state)


@router.post("/positions/{pair}/close", dependencies=[Depends(require_token)])
async def position_close(pair: str, state: Annotated[AppState, Depends(get_state)]) -> Snapshot:
    try:
        closed = await _engine(state).close_position(pair)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
            state.agent_engine.pending.clear()
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
        except Exception:  # noqa: BLE001 - a closed socket shows as any error
            state.ws_clients.discard(ws)
