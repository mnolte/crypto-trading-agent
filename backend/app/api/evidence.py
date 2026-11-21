"""PR 8 "Evidence" endpoints: Insights, What-if, shadows, Ask, stats.

All writes need the local token. What-if runs happen inline — a local
replay over stored candles — so POST returns the finished job.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo, settings_service
from app.agent.prompts import PromptNotFoundError, load_prompt
from app.api.deps import AppState, broadcast_settings, get_state, require_token
from app.db import DecisionRow, OutcomeRow, WhatifRow, utcnow
from app.features import ask as ask_mod
from app.features import shadows as shadows_mod
from app.features.insights import DIMENSIONS, compute_insights
from app.features.whatif import (
    WhatIfSpecError,
    apply_overrides,
    run_whatif,
)
from app.models import (
    AskConversationListOut,
    AskConversationOut,
    AskIn,
    AskOut,
    InsightsOut,
    OutcomeStatsOut,
    OutcomeStatsRow,
    ShadowIn,
    ShadowOut,
    ShadowPromoteIn,
    WhatIfApplyIn,
    WhatIfJobOut,
    WhatIfResult,
    WhatIfSaveIn,
    WhatIfSpecIn,
)

router = APIRouter(prefix="/api")


def _session(state: AppState) -> Session:
    return state.session_factory()


def _whatif_out(row: WhatifRow) -> WhatIfJobOut:
    return WhatIfJobOut(
        id=row.id,
        status=row.status,
        progress=row.progress,
        name=row.name,
        saved=row.saved,
        created_at=repo.aware_utc(row.created_at),
        updated_at=repo.aware_utc(row.updated_at),
        spec=WhatIfSpecIn.model_validate(json.loads(row.spec)),
        error=row.error,
        result=WhatIfResult.model_validate_json(row.result) if row.result else None,
    )


# --------------------------------------------------------------- insights --


@router.get("/insights", response_model=InsightsOut)
def get_insights(
    state: Annotated[AppState, Depends(get_state)],
    dimension: str = "pair",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    decider: str = "all",
    prompt_version: str | None = None,
) -> InsightsOut:
    if dimension not in DIMENSIONS:
        raise HTTPException(status_code=422, detail=f"unknown dimension {dimension!r}")
    session = _session(state)
    try:
        if decider not in ("all", "openai", "rules") and not (
            decider.startswith("shadow:")
            and repo.get_shadow(session, decider.split(":", 1)[1]) is not None
        ):
            raise HTTPException(status_code=422, detail=f"unknown decider {decider!r}")
        return compute_insights(
            session,
            now=utcnow(),
            settings=state.settings,
            dimension=dimension,  # type: ignore[arg-type]
            start=start,
            end=end,
            decider=decider,
            prompt_version=prompt_version,
        )
    finally:
        session.close()


# ---------------------------------------------------------------- what-if --


@router.get("/whatif", response_model=dict[str, Any])
def list_whatif(state: Annotated[AppState, Depends(get_state)]) -> dict[str, Any]:
    session = _session(state)
    try:
        return {"items": [_whatif_out(r) for r in repo.list_whatifs(session)]}
    finally:
        session.close()


@router.post("/whatif", response_model=WhatIfJobOut, dependencies=[Depends(require_token)])
async def create_whatif(
    body: WhatIfSpecIn, state: Annotated[AppState, Depends(get_state)]
) -> WhatIfJobOut:
    session = _session(state)
    try:
        # Reject bad overrides up front so a typo doesn't silently no-op.
        try:
            apply_overrides(state.settings, body.overrides)
        except WhatIfSpecError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if repo.aware_utc(body.end) <= repo.aware_utc(body.start):
            raise HTTPException(status_code=422, detail="'to' must be after 'from'")
        row = repo.add_whatif(
            session,
            WhatifRow(
                id=uuid.uuid4().hex[:12],
                name=body.name,
                spec=body.model_dump_json(by_alias=True),
                status="queued",
                progress=0.0,
                saved=body.name is not None,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
        )
        session.commit()
        await run_whatif(session, row, state.market_rules)
        return _whatif_out(row)
    finally:
        session.close()


@router.get("/whatif/{scenario_id}", response_model=WhatIfJobOut)
def get_whatif(scenario_id: str, state: Annotated[AppState, Depends(get_state)]) -> WhatIfJobOut:
    session = _session(state)
    try:
        row = repo.get_whatif(session, scenario_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such scenario")
        return _whatif_out(row)
    finally:
        session.close()


@router.post(
    "/whatif/{scenario_id}/save", response_model=WhatIfJobOut, dependencies=[Depends(require_token)]
)
def save_whatif(
    scenario_id: str,
    body: WhatIfSaveIn,
    state: Annotated[AppState, Depends(get_state)],
) -> WhatIfJobOut:
    session = _session(state)
    try:
        row = repo.get_whatif(session, scenario_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such scenario")
        row.saved = True
        if body.name:
            row.name = body.name
        elif not row.name:
            row.name = f"Scenario {repo.aware_utc(row.created_at):%d %b}"
        row.updated_at = utcnow()
        session.commit()
        return _whatif_out(row)
    finally:
        session.close()


@router.post("/whatif/{scenario_id}/apply", dependencies=[Depends(require_token)])
async def apply_whatif(
    scenario_id: str,
    body: WhatIfApplyIn,
    state: Annotated[AppState, Depends(get_state)],
) -> dict[str, Any]:
    session = _session(state)
    try:
        row = repo.get_whatif(session, scenario_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such scenario")
        spec = WhatIfSpecIn.model_validate(json.loads(row.spec))
        overrides = {k: v for k, v in spec.overrides.items() if k != "no_weekend_entries"}
        if not overrides:
            raise HTTPException(status_code=422, detail="nothing to apply")
        new_settings = apply_overrides(state.settings, overrides)
        # WhatIfApplyIn.confirm is the "it is me" step, so looser changes
        # apply without a second confirmation.
        state.settings_version = settings_service.save(
            session,
            new_settings,
            reason=f"Applied What-if scenario '{row.name or row.id}'",
            source="whatif",
            confirm_looser=True,
        )
        state.settings = new_settings
        repo.add_activity(
            session,
            "settings",
            f"What-if scenario {row.id} applied to settings",
            at=utcnow(),
        )
        session.commit()
        await broadcast_settings(state)
        return {"ok": True, "settings_version": state.settings_version}
    finally:
        session.close()


# ---------------------------------------------------------------- shadows --


@router.get("/shadows", response_model=dict)
def list_shadows(state: Annotated[AppState, Depends(get_state)]) -> dict[str, Any]:
    session = _session(state)
    try:
        account = repo.current_account(session)
        return {
            "items": [
                shadows_mod.shadow_out(session, r, account.id, utcnow())
                for r in repo.list_shadows(session)
            ],
            "max_active": shadows_mod.MAX_ACTIVE,
        }
    finally:
        session.close()


@router.post(
    "/shadows", response_model=ShadowOut, status_code=201, dependencies=[Depends(require_token)]
)
def create_shadow(body: ShadowIn, state: Annotated[AppState, Depends(get_state)]) -> ShadowOut:
    session = _session(state)
    try:
        try:
            apply_overrides(state.settings, body.overrides)
        except WhatIfSpecError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            load_prompt(body.prompt_version)
        except PromptNotFoundError as exc:
            raise HTTPException(
                status_code=422, detail=f"prompt {body.prompt_version} not found"
            ) from exc
        account = repo.current_account(session)
        equity = state.broker.equity() if state.broker else Decimal(account.cash)
        try:
            row = shadows_mod.create_shadow(
                session,
                live_account=account,
                live_equity=equity,
                name=body.name,
                model=body.model,
                prompt_version=body.prompt_version,
                overrides=body.overrides,
                daily_call_limit=body.daily_call_limit,
                now=utcnow(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        session.commit()
        return shadows_mod.shadow_out(session, row, account.id, utcnow())
    finally:
        session.close()


@router.delete("/shadows/{shadow_id}", dependencies=[Depends(require_token)])
def stop_shadow(shadow_id: str, state: Annotated[AppState, Depends(get_state)]) -> dict[str, Any]:
    session = _session(state)
    try:
        row = repo.get_shadow(session, shadow_id)
        if row is None or row.status != "running":
            raise HTTPException(status_code=404, detail="no such running shadow")
        shadows_mod.stop_shadow(session, row, utcnow())
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.post("/shadows/{shadow_id}/promote", dependencies=[Depends(require_token)])
async def promote_shadow(
    shadow_id: str,
    body: ShadowPromoteIn,
    state: Annotated[AppState, Depends(get_state)],
) -> dict[str, Any]:
    session = _session(state)
    try:
        row = repo.get_shadow(session, shadow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such shadow")
        new_settings = state.settings.model_copy(deep=True)
        new_settings.model.model_id = row.model
        new_settings.model.prompt_version = row.prompt_version
        # ShadowPromoteIn.confirm is the "it is me" step.
        state.settings_version = settings_service.save(
            session,
            new_settings,
            reason=f"Promoted {row.name}",
            source="promote",
            confirm_looser=True,
        )
        state.settings = new_settings
        shadows_mod.stop_shadow(session, row, utcnow())
        repo.add_activity(
            session,
            "settings",
            f"{row.name} promoted: live now runs model {row.model}, prompt {row.prompt_version}",
            at=utcnow(),
        )
        session.commit()
        await broadcast_settings(state)
        return {"ok": True, "settings_version": state.settings_version}
    finally:
        session.close()


# -------------------------------------------------------------------- ask --


@router.post("/ask", response_model=AskOut, dependencies=[Depends(require_token)])
async def ask(body: AskIn, state: Annotated[AppState, Depends(get_state)]) -> AskOut:
    session = _session(state)
    try:
        account = repo.current_account(session)
        convo = None
        if body.conversation_id:
            convo = repo.get_conversation(session, body.conversation_id)
            if convo is None:
                raise HTTPException(status_code=404, detail="no such conversation")
        try:
            answer, convo, _call_ids = await ask_mod.answer_question(
                session,
                now=utcnow(),
                account_id=account.id,
                broker=state.broker,
                settings=state.settings,
                question=body.question,
                conversation=convo,
                context=body.context.model_dump() if body.context else None,
            )
        except ask_mod.AskRefusal as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        session.commit()
        asked = repo.ask_questions_today(session, utcnow())
        limit = state.settings.learning.ask_daily_limit
        return AskOut(
            conversation_id=convo.id,
            answer=answer,
            questions_today=asked,
            daily_limit=limit,
            remaining=max(0, limit - asked),
        )
    finally:
        session.close()


@router.get("/ask/conversations", response_model=AskConversationListOut)
def list_conversations(state: Annotated[AppState, Depends(get_state)]) -> AskConversationListOut:
    session = _session(state)
    try:
        return AskConversationListOut(
            items=[ask_mod.conversation_summary(r) for r in repo.list_conversations(session)],
            questions_today=repo.ask_questions_today(session, utcnow()),
            daily_limit=state.settings.learning.ask_daily_limit,
        )
    finally:
        session.close()


@router.get("/ask/conversations/{conversation_id}", response_model=AskConversationOut)
def get_conversation(
    conversation_id: str, state: Annotated[AppState, Depends(get_state)]
) -> AskConversationOut:
    session = _session(state)
    try:
        row = repo.get_conversation(session, conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        return ask_mod.conversation_out(row)
    finally:
        session.close()


# --------------------------------------------------------- outcomes stats --


@router.get("/outcomes/stats", response_model=OutcomeStatsOut)
def outcomes_stats(
    state: Annotated[AppState, Depends(get_state)],
    group: str = "decider,model,prompt_version",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
) -> OutcomeStatsOut:
    """The scorecard grouped by the requested keys, optionally including
    ``shadow`` — shadows appear in the scorecard like any other decider."""
    session = _session(state)
    try:
        groups = [g.strip() for g in group.split(",") if g.strip()]
        conds = [OutcomeRow.horizon == 72]
        if start is not None:
            conds.append(DecisionRow.at >= repo.aware_utc(start).replace(tzinfo=None))
        if end is not None:
            conds.append(DecisionRow.at <= repo.aware_utc(end).replace(tzinfo=None))
        rows = session.execute(
            select(DecisionRow, OutcomeRow)
            .join(OutcomeRow, OutcomeRow.decision_id == DecisionRow.id)
            .where(*conds)
        ).all()
        shadow_names = {s.id: s.name for s in repo.list_shadows(session)}

        def key(d: DecisionRow) -> tuple[str | None, ...]:
            parts: list[str | None] = []
            for g in groups:
                if g == "decider":
                    parts.append(d.decider)
                elif g == "model":
                    parts.append(d.model)
                elif g == "prompt_version":
                    parts.append(d.prompt_version)
                elif g == "shadow":
                    parts.append(d.shadow_id)
                else:
                    parts.append(None)
            return tuple(parts)

        grouped: dict[tuple[str | None, ...], list[Any]] = {}
        for d, o in rows:
            grouped.setdefault(key(d), []).append((d, o))
        items: list[OutcomeStatsRow] = []
        for k, values in grouped.items():
            buys = [o for d, o in values if d.action == "BUY"]
            wins = sum(o.label == "win" for o in buys)
            realised = [Decimal(o.realised_r) for _, o in values if o.realised_r is not None]
            shadow_val = None
            if "shadow" in groups:
                sid = k[groups.index("shadow")]
                shadow_val = shadow_names.get(sid, sid) if sid else "live"
            items.append(
                OutcomeStatsRow(
                    decider=k[groups.index("decider")] if "decider" in groups else "all",
                    model=k[groups.index("model")] if "model" in groups else None,
                    prompt_version=(
                        k[groups.index("prompt_version")] if "prompt_version" in groups else None
                    ),
                    shadow=shadow_val,
                    decisions=len(values),
                    buy_win_rate_pct=round(wins / len(buys) * 100, 1) if buys else None,
                    average_r=(float(sum(realised) / len(realised)) if realised else None),
                    missed=sum(o.label == "missed" for _, o in values),
                    avoided=sum(o.label == "avoided" for _, o in values),
                )
            )
        return OutcomeStatsOut(items=items)
    finally:
        session.close()
