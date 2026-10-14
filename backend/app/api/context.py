from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app import repo
from app.api.deps import AppState, get_state

router = APIRouter(prefix="/api")


class ContextOut(BaseModel):
    id: int
    source: Literal["okx", "deribit"]
    metric: str
    symbol: Literal["BTC", "ETH"]
    ts: datetime
    value: float
    meta: dict[str, object]
    fetched_at: datetime


@router.get("/context")
def context_rows(
    state: Annotated[AppState, Depends(get_state)],
    metric: str | None = None,
    symbol: Literal["BTC", "ETH"] | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> list[ContextOut]:
    session = state.session_factory()
    try:
        rows = repo.list_market_context(session, metric=metric, symbol=symbol, start=from_, end=to)
        return [
            ContextOut(
                id=row.id,
                source=row.source,
                metric=row.metric,
                symbol=row.symbol,
                ts=repo.aware_utc(row.ts),
                value=row.value,
                meta=json.loads(row.meta),
                fetched_at=repo.aware_utc(row.fetched_at),
            )
            for row in rows
        ]
    finally:
        session.close()
