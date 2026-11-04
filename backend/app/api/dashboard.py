"""Dashboard endpoints: the Needs you list and the cycles card."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import AppState, get_state
from app.db import utcnow
from app.features.cycles import build_cycles_recent
from app.features.needs import load_needs
from app.models import CyclesRecent, Needs

router = APIRouter(prefix="/api")


@router.get("/needs", response_model=Needs)
def get_needs(state: Annotated[AppState, Depends(get_state)]) -> Needs:
    """The Needs you list plus the health line. GETs need no token."""
    session = state.session_factory()
    try:
        engine = state.agent_engine
        now = engine.clock() if engine is not None else utcnow()
        return load_needs(state, session, now)
    finally:
        session.close()


@router.get("/cycles/recent", response_model=CyclesRecent)
def get_cycles_recent(
    state: Annotated[AppState, Depends(get_state)],
    hours: Annotated[int, Query(ge=1, le=72)] = 24,
) -> CyclesRecent:
    """The rolling timeline: one slot per quarter hour, the scheduled
    jobs, the ran/missed summary and the grouped decisions."""
    session = state.session_factory()
    try:
        engine = state.agent_engine
        now = engine.clock() if engine is not None else utcnow()
        return build_cycles_recent(state, session, now, hours)
    finally:
        session.close()
