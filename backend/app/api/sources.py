"""Source registry and markets endpoints.

``GET /api/sources`` is built straight from the registry descriptors, so
adding a source needs no new UI. ``/api/secrets`` stores keys in the OS
keyring — the API only ever reports "set" and the last 4 characters.
``/api/markets`` exposes the eligibility check, the failed rule, and the
active / warming-up state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import repo
from app.agent.market.universe import (
    IneligibleMarketError,
    UnknownMarketError,
    activate_market,
    deactivate_market,
    warming_up,
)
from app.api.deps import AppState, get_state, require_token
from app.api.routes import broadcast_snapshot
from app.config import delete_secret, set_secret
from app.db import MarketRow
from app.sources import SourceStatus, get_spec, test_source_key

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ schemas --


class SourceOut(BaseModel):
    id: str
    name: str
    purpose: str
    key_requirement: Literal["none", "optional", "required"]
    key_set: bool
    key_last4: str | None
    enabled: bool
    disabled_reason: str | None
    freshness_limit_s: int | None
    rate_limit: str | None
    rate_limit_remaining: int | None
    last_success_at: datetime | None
    last_error: str | None
    failures: int


def _source_out(status: SourceStatus) -> SourceOut:
    spec, rt = status.spec, status.runtime
    return SourceOut(
        id=spec.id,
        name=spec.name,
        purpose=spec.purpose,
        key_requirement=spec.key,
        key_set=status.key_set,
        key_last4=status.key_last4,
        enabled=status.enabled,
        disabled_reason=status.disabled_reason,
        freshness_limit_s=spec.freshness_limit_s,
        rate_limit=spec.rate_limit,
        rate_limit_remaining=rt.rate_limit_remaining,
        last_success_at=rt.last_success_at,
        last_error=rt.last_error,
        failures=rt.failures,
    )


class SourceUpdateIn(BaseModel):
    enabled: bool


class SecretIn(BaseModel):
    key: str


class SecretTestOut(BaseModel):
    ok: bool
    error: str | None = None


class MarketOut(BaseModel):
    market: str
    eligible: bool
    failed_rule: str | None
    active: bool
    warming_up: bool
    paused: bool
    status: str
    volume_30d_eur: float | None
    spread_pct: float | None
    daily_candles: int | None
    checked_at: datetime | None


def _market_out(state: AppState, row: MarketRow) -> MarketOut:
    session = state.session_factory()
    try:
        warming = warming_up(session, row.market, state.settings.market)
    finally:
        session.close()
    return MarketOut(
        market=row.market,
        eligible=row.eligible,
        failed_rule=row.failed_rule,
        active=row.active,
        warming_up=warming,
        paused=False,  # per-coin pauses land with the entry-pause PR
        status=row.status,
        volume_30d_eur=row.volume_30d_eur,
        spread_pct=row.spread_pct,
        daily_candles=row.daily_candles,
        checked_at=row.checked_at,
    )


class MarketUpdateIn(BaseModel):
    active: bool


# ------------------------------------------------------------------ sources --


@router.get("/sources")
def list_sources(state: Annotated[AppState, Depends(get_state)]) -> list[SourceOut]:
    return [_source_out(s) for s in state.sources.statuses()]


@router.put("/sources/{source_id}", dependencies=[Depends(require_token)])
async def update_source(
    source_id: str, body: SourceUpdateIn, state: Annotated[AppState, Depends(get_state)]
) -> SourceOut:
    if get_spec(source_id) is None:
        raise HTTPException(status_code=404, detail="unknown source")
    settings = state.settings
    disabled = set(settings.disabled_sources)
    if body.enabled:
        disabled.discard(source_id)
    else:
        disabled.add(source_id)
    session = state.session_factory()
    try:
        settings.disabled_sources = sorted(disabled)
        version = repo.save_settings(session, settings)
    finally:
        session.close()
    state.settings = settings
    state.settings_version = version
    state.sources.set_disabled(settings.disabled_sources)
    await broadcast_snapshot(state)
    return _source_out(state.sources.status(source_id))


# ------------------------------------------------------------------ secrets --


@router.put("/secrets/{source_id}", dependencies=[Depends(require_token)])
def put_secret(
    source_id: str, body: SecretIn, state: Annotated[AppState, Depends(get_state)]
) -> dict[str, bool]:
    spec = get_spec(source_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown source")
    if spec.key == "none":
        raise HTTPException(status_code=422, detail="source does not use an API key")
    if not body.key.strip():
        raise HTTPException(status_code=422, detail="key must not be empty")
    set_secret(source_id, body.key.strip())
    return {"ok": True}


@router.delete("/secrets/{source_id}", dependencies=[Depends(require_token)])
def remove_secret(
    source_id: str, state: Annotated[AppState, Depends(get_state)]
) -> dict[str, bool]:
    if get_spec(source_id) is None:
        raise HTTPException(status_code=404, detail="unknown source")
    delete_secret(source_id)
    return {"ok": True}


@router.post("/secrets/{source_id}/test", dependencies=[Depends(require_token)])
async def test_secret(
    source_id: str, state: Annotated[AppState, Depends(get_state)]
) -> SecretTestOut:
    if get_spec(source_id) is None:
        raise HTTPException(status_code=404, detail="unknown source")
    ok, error = await test_source_key(source_id)
    return SecretTestOut(ok=ok, error=error)


# ------------------------------------------------------------------ markets --


@router.get("/markets")
def list_markets(state: Annotated[AppState, Depends(get_state)]) -> list[MarketOut]:
    session = state.session_factory()
    try:
        rows = repo.list_market_rows(session)
    finally:
        session.close()
    return [_market_out(state, row) for row in rows]


@router.put("/markets/{market}", dependencies=[Depends(require_token)])
async def update_market(
    market: str, body: MarketUpdateIn, state: Annotated[AppState, Depends(get_state)]
) -> MarketOut:
    session = state.session_factory()
    settings = state.settings
    try:
        if body.active:
            try:
                row = await activate_market(state.market_client, session, market, settings.market)
            except UnknownMarketError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except IneligibleMarketError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=exc.failed_rule or "not eligible",
                ) from exc
            if market not in settings.active_markets:
                settings.active_markets = [*settings.active_markets, market]
        else:
            deactivated = deactivate_market(session, market)
            if deactivated is None:
                raise HTTPException(status_code=404, detail="unknown market")
            row = deactivated
            settings.active_markets = [m for m in settings.active_markets if m != market]
        version = repo.save_settings(session, settings)
    finally:
        session.close()
    state.settings = settings
    state.settings_version = version
    await broadcast_snapshot(state)
    return _market_out(state, row)
