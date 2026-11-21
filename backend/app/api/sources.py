"""Source registry and markets endpoints.

``GET /api/sources`` is built straight from the registry descriptors, so
adding a source needs no new UI. ``/api/secrets`` stores keys in the OS
keyring — the API only ever reports "set" and the last 4 characters.
``/api/markets`` exposes the eligibility check, the failed rule, and the
active / warming-up state.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app import repo, settings_service
from app.agent.market.universe import (
    IneligibleMarketError,
    UnknownMarketError,
    activate_market,
    deactivate_market,
    warming_up,
)
from app.api import errors as api_errors
from app.api.deps import AppState, broadcast_settings, get_state, require_token
from app.api.routes import broadcast_snapshot
from app.config import SecretStoreError, delete_secret, set_secret
from app.db import DecisionRow, MarketRow, utcnow
from app.models import MoneyDecimal, Regime
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
    confirm: bool = False


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
    # Markets card extras — filled for active markets only.
    price: MoneyDecimal | None = None
    chg_24h_pct: MoneyDecimal | None = None
    regime: Regime | None = None
    rules_state: Literal["passed", "held", "failed", "paused", "warming", "none"] | None = None
    rules_detail: str | None = None
    last_action_action: str | None = None  # e.g. "BUY", "SELL", "HOLD", "SKIP"
    last_action_at: datetime | None = None


_FAILED_RE = re.compile(r"failed:\s*(.+?)\s*\.?\s*$")


def _rules_detail(reasoning: str) -> str:
    """The failed-condition tail of a pre-filter reasoning string."""
    match = _FAILED_RE.search(reasoning)
    if match:
        return match.group(1)
    return reasoning[:80]


def _market_out(
    state: AppState,
    row: MarketRow,
    session: Session | None = None,
    *,
    latest: dict[str, DecisionRow] | None = None,
    last_actions: dict[str, DecisionRow] | None = None,
) -> MarketOut:
    close_after = session is None
    session = session or state.session_factory()
    try:
        warming = warming_up(session, row.market, state.settings.market)
        paused = repo.active_entry_pause(session, row.market, utcnow()) is not None
    finally:
        if close_after:
            session.close()
    price = chg = None
    regime: Regime | None = None
    rules_state: Literal["passed", "held", "failed", "paused", "warming", "none"] | None = None
    rules_detail: str | None = None
    action_row: DecisionRow | None = None
    if row.active:
        engine = state.agent_engine
        if engine is not None:
            info = engine.prices.get(row.market)
            if info is not None:
                price, chg = info.last, info.chg_24h_pct
        drow = (latest or {}).get(row.market)
        if drow is not None:
            try:
                regime = Regime.model_validate_json(drow.regime)
            except (ValueError, TypeError):
                regime = None
        holding = state.broker is not None and row.market in state.broker.positions
        if warming:
            rules_state = "warming"
        elif paused:
            rules_state = "paused"
        elif drow is None:
            rules_state = "none"
        elif not drow.prefilter:
            rules_state = "failed"
            rules_detail = _rules_detail(drow.reasoning)
        elif holding:
            rules_state = "held"
        else:
            rules_state = "passed"
        action_row = (last_actions or {}).get(row.market)
    return MarketOut(
        market=row.market,
        eligible=row.eligible,
        failed_rule=row.failed_rule,
        active=row.active,
        warming_up=warming,
        paused=paused,
        status=row.status,
        volume_30d_eur=row.volume_30d_eur,
        spread_pct=row.spread_pct,
        daily_candles=row.daily_candles,
        checked_at=row.checked_at,
        price=price,
        chg_24h_pct=chg,
        regime=regime,
        rules_state=rules_state,
        rules_detail=rules_detail,
        last_action_action=action_row.action if action_row is not None else None,
        last_action_at=repo.aware_utc(action_row.at) if action_row is not None else None,
    )


class MarketUpdateIn(BaseModel):
    active: bool
    confirm: bool = False


class MarketActionIn(BaseModel):
    """POST /api/markets/{market}/activate|deactivate body (item 025)."""

    confirm: bool = False


# ------------------------------------------------------------------ sources --


@router.get("/sources")
def list_sources(state: Annotated[AppState, Depends(get_state)]) -> list[SourceOut]:
    statuses = state.sources.statuses()
    session = state.session_factory()
    try:
        for status in statuses:
            if status.spec.id in ("okx", "deribit"):
                status.runtime.last_success_at = repo.newest_market_context(session, status.spec.id)
    finally:
        session.close()
    return [_source_out(status) for status in statuses]


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
        try:
            version = settings_service.save(
                session,
                settings,
                reason=f"Turned {'on' if body.enabled else 'off'} {source_id}",
                source="action",
                confirm_looser=body.confirm,
            )
        except settings_service.LooserNeedsConfirm as exc:
            return api_errors.api_error(  # type: ignore[return-value]
                409,
                "This change loosens a limit — confirm to save it.",
                code="looser_needs_confirm",
                extra={"changes": exc.changes},
            )
    finally:
        session.close()
    state.settings = settings
    state.settings_version = version
    state.sources.set_disabled(settings.disabled_sources)
    await broadcast_snapshot(state)
    await broadcast_settings(state)
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
    try:
        set_secret(source_id, body.key.strip())
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/secrets/{source_id}", dependencies=[Depends(require_token)])
def remove_secret(
    source_id: str, state: Annotated[AppState, Depends(get_state)]
) -> dict[str, bool]:
    spec = get_spec(source_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown source")
    if spec.key == "none":
        raise HTTPException(status_code=422, detail="source does not use an API key")
    try:
        delete_secret(source_id)
    except SecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        account_id = state.broker.account_id if state.broker is not None else None
        latest = (
            repo.latest_decisions(session, account_id, [r.market for r in rows])
            if account_id is not None
            else {}
        )
        last_actions: dict[str, DecisionRow] = {}
        if account_id is not None:
            for drow in repo.list_decision_rows(
                session,
                account_id=account_id,
                after=utcnow() - timedelta(hours=24),
                limit=None,
            ):
                # Rules no-calls are not actions — the newest called row wins.
                if drow.prefilter and drow.pair not in last_actions:
                    last_actions[drow.pair] = drow
        return [
            _market_out(state, row, session, latest=latest, last_actions=last_actions)
            for row in rows
        ]
    finally:
        session.close()


async def _set_market_active(
    state: AppState, market: str, active: bool, confirm: bool
) -> MarketOut | JSONResponse:
    """Activate or deactivate a market and save the settings version.

    Returns the market payload, or the 409 response when the change is
    looser and ``confirm`` is false.
    """
    session = state.session_factory()
    settings = state.settings
    try:
        if active:
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
        try:
            version = settings_service.save(
                session,
                settings,
                reason=f"{'Activated' if active else 'Deactivated'} {market}",
                source="action",
                confirm_looser=confirm,
            )
        except settings_service.LooserNeedsConfirm as exc:
            return api_errors.api_error(  # type: ignore[return-value]
                409,
                "This change loosens a limit — confirm to save it.",
                code="looser_needs_confirm",
                extra={"changes": exc.changes},
            )
    finally:
        session.close()
    state.settings = settings
    state.settings_version = version
    await broadcast_snapshot(state)
    await broadcast_settings(state)
    return _market_out(state, row)


@router.put("/markets/{market}", dependencies=[Depends(require_token)])
async def update_market(
    market: str, body: MarketUpdateIn, state: Annotated[AppState, Depends(get_state)]
) -> MarketOut:
    return await _set_market_active(state, market, body.active, body.confirm)  # type: ignore[return-value]


@router.post("/markets/{market}/activate", dependencies=[Depends(require_token)])
async def activate_market_route(
    market: str, body: MarketActionIn, state: Annotated[AppState, Depends(get_state)]
) -> MarketOut:
    return await _set_market_active(state, market, True, body.confirm)  # type: ignore[return-value]


@router.post("/markets/{market}/deactivate", dependencies=[Depends(require_token)])
async def deactivate_market_route(
    market: str, body: MarketActionIn, state: Annotated[AppState, Depends(get_state)]
) -> MarketOut:
    return await _set_market_active(state, market, False, body.confirm)  # type: ignore[return-value]
