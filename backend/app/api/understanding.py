"""Understanding and safety endpoints.

Entry pauses (per-coin and the ``"*"`` all-coins pause), the glossary, the
live-readiness checklist, briefings and the settings history. Pauses block
new entries only — exits and the model's SELL decisions keep working.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from app import repo
from app.api.deps import AppState, get_state, require_token
from app.api.routes import broadcast_snapshot
from app.db import BriefingRow, EntryPauseRow, utcnow
from app.features import glossary as glossary_mod
from app.features import readiness as readiness_mod
from app.models import (
    BriefingListOut,
    BriefingOut,
    EntryPause,
    EntryPauseIn,
    GlossaryOut,
    GlossaryTermOut,
    ReadinessOut,
    SettingsHistoryItem,
    SettingsHistoryOut,
)

router = APIRouter(prefix="/api")


def _pause_out(row: EntryPauseRow) -> EntryPause:
    return EntryPause(pair=row.pair, until=repo.aware_utc(row.until) if row.until else None)


def _set_pause(state: AppState, pair: str, body: EntryPauseIn) -> EntryPause:
    now = utcnow()
    # Naive datetimes are UTC by the repo's convention; comparing a naive
    # value against the aware `now` would raise TypeError (500).
    until = repo.aware_utc(body.until) if body.until is not None else None
    if until is not None and until <= now:
        raise HTTPException(status_code=422, detail="until must be in the future")
    session = state.session_factory()
    try:
        row = repo.set_entry_pause(session, pair, until, at=now)
        label = "all coins" if pair == "*" else pair
        when = "until resumed" if until is None else f"until {until.isoformat()}"
        repo.add_activity(session, "pause", f"entries paused on {label} {when}", at=now)
        session.commit()
        return _pause_out(row)
    finally:
        session.close()


def _clear_pause(state: AppState, pair: str) -> None:
    session = state.session_factory()
    try:
        repo.clear_entry_pause(session, pair)
        label = "all coins" if pair == "*" else pair
        repo.add_activity(session, "pause", f"entries resumed on {label}")
        session.commit()
    finally:
        session.close()


@router.put("/agent/entry-pause", dependencies=[Depends(require_token)])
async def pause_all_entries(
    body: EntryPauseIn, state: Annotated[AppState, Depends(get_state)]
) -> EntryPause:
    out = _set_pause(state, "*", body)
    await broadcast_snapshot(state)
    return out


@router.delete("/agent/entry-pause", dependencies=[Depends(require_token)])
async def resume_all_entries(state: Annotated[AppState, Depends(get_state)]) -> dict[str, bool]:
    _clear_pause(state, "*")
    await broadcast_snapshot(state)
    return {"ok": True}


@router.put("/markets/{market}/pause", dependencies=[Depends(require_token)])
async def pause_market(
    market: str, body: EntryPauseIn, state: Annotated[AppState, Depends(get_state)]
) -> EntryPause:
    session = state.session_factory()
    try:
        known = market != "*" and repo.get_market_row(session, market) is not None
    finally:
        session.close()
    if not known:
        # The market must be a known row; "*" belongs to the global endpoint.
        raise HTTPException(status_code=404, detail="unknown market")
    out = _set_pause(state, market, body)
    await broadcast_snapshot(state)
    return out


@router.delete("/markets/{market}/pause", dependencies=[Depends(require_token)])
async def resume_market(
    market: str, state: Annotated[AppState, Depends(get_state)]
) -> dict[str, bool]:
    _clear_pause(state, market)
    await broadcast_snapshot(state)
    return {"ok": True}


@router.get("/glossary")
def get_glossary(state: Annotated[AppState, Depends(get_state)]) -> GlossaryOut:
    terms = glossary_mod.load_glossary()
    return GlossaryOut(
        terms=[GlossaryTermOut(term=k, definition=v) for k, v in sorted(terms.items())],
        plain_language=state.settings.learning.plain_language,
    )


@router.get("/readiness")
def get_readiness(state: Annotated[AppState, Depends(get_state)]) -> ReadinessOut:
    session = state.session_factory()
    try:
        account = repo.current_account(session)
        equity = (
            state.broker.equity() if state.broker is not None else Decimal(account.start_balance)
        )
        return readiness_mod.compute_readiness(
            session, now=utcnow(), account=account, equity=equity, settings=state.settings
        )
    finally:
        session.close()


def _briefing_out(row: BriefingRow) -> BriefingOut:
    return BriefingOut(
        id=row.id,
        at=repo.aware_utc(row.at),
        type="weekly" if row.type == "weekly" else "daily",
        facts=json.loads(row.facts),
        text=row.text,
        source="model" if row.source == "model" else "template",
        reason=row.reason,
        llm_call_id=row.llm_call_id,
    )


@router.get("/briefings")
def list_briefings(
    state: Annotated[AppState, Depends(get_state)],
    type: Literal["daily", "weekly"] | None = None,
    limit: int = 30,
) -> BriefingListOut:
    session = state.session_factory()
    try:
        return BriefingListOut(
            items=[_briefing_out(r) for r in repo.list_briefings(session, type, limit)]
        )
    finally:
        session.close()


@router.get("/briefings/{briefing_id}")
def get_briefing(briefing_id: int, state: Annotated[AppState, Depends(get_state)]) -> BriefingOut:
    session = state.session_factory()
    try:
        row = repo.get_briefing(session, briefing_id)
        if row is None:
            raise HTTPException(status_code=404, detail="briefing not found")
        return _briefing_out(row)
    finally:
        session.close()


@router.get("/settings/history")
def get_settings_history(
    state: Annotated[AppState, Depends(get_state)], limit: int = 200
) -> SettingsHistoryOut:
    session = state.session_factory()
    try:
        return SettingsHistoryOut(
            items=[
                SettingsHistoryItem(
                    at=repo.aware_utc(row.at), key=row.key, old=row.old, new=row.new
                )
                for row in repo.list_settings_history(session, limit=limit)
            ]
        )
    finally:
        session.close()
