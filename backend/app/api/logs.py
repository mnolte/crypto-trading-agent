"""The Logs API: read, search, trace and review the log store.

- ``GET /api/logs`` — lines, newest first, with level/component/event/
  pair/cycle/request/error filters, a message-text search (FTS5) and
  keyset pagination (``after`` = ``"<ts>|<id>"``).
- ``GET /api/logs/summary`` — counts per level for the last 24 h, the
  last ERROR time, db size and queue/fallback state.
- ``GET /api/logs/problems`` — recurring WARNING+ lines grouped by
  fingerprint, with count, state and the registry's what-to-do.
- ``POST /api/logs/problems/{fingerprint}/review`` — mark a problem
  expected or fixed; a note is required.
- ``GET /api/logs/events`` — the event registry (meaning, what-to-do).
- ``GET /api/logs/export.jsonl?day=YYYY-MM-DD`` — every line for a day.
- ``POST /api/logs/client`` — the frontend reports errors/warnings; at
  most ten a minute.
- ``GET /api/logs/{id}`` and ``GET /api/logs/{id}/around`` — one line
  and the twenty lines around it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import logevents
from app.api.deps import AppState, require_token
from app.logstore.record import COLUMNS

log = logging.getLogger("app.api.logs")

router = APIRouter(prefix="/api/logs", tags=["logs"])

_PAGE = 200
_AROUND = 20
_EXPORT_LIMIT = 100_000
_CLIENT_LIMIT_PER_MIN = 10


class LogLineOut(BaseModel):
    model_config = {"extra": "ignore"}
    id: int
    ts: str | None = None
    level: str | None = None
    levelno: int | None = None
    logger: str | None = None
    event: str | None = None
    message: str | None = None
    process: str | None = None
    pid: int | None = None
    task: str | None = None
    cycle_id: str | None = None
    job: str | None = None
    pair: str | None = None
    decision_id: str | None = None
    trade_id: str | None = None
    order_id: str | None = None
    shadow_id: str | None = None
    backtest_id: str | None = None
    request_id: str | None = None
    duration_ms: float | None = None
    error_id: str | None = None
    exc_type: str | None = None
    exc_message: str | None = None
    traceback: str | None = None
    data: str | None = None
    fingerprint: str | None = None
    version: str | None = None
    meaning: str | None = None
    what_to_do: str | None = None


class LogsListOut(BaseModel):
    lines: list[LogLineOut]
    next: str | None = None


class LogProblemOut(BaseModel):
    model_config = {"extra": "ignore"}
    fingerprint: str
    level: str | None = None
    levelno: int | None = None
    logger: str | None = None
    event: str | None = None
    example: str | None = None
    count: int | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    pairs: list[str] = []
    state: str | None = None
    note: str | None = None
    reviewed_at: str | None = None
    count_at_review: int | None = None
    meaning: str | None = None
    what_to_do: str | None = None


class ProblemsOut(BaseModel):
    problems: list[LogProblemOut]


class LogSummaryOut(BaseModel):
    counts_24h: dict[str, int]
    last_error_at: str | None = None
    total_lines: int
    db_bytes: int
    broken: bool
    dropped: dict[str, int]


class LogEventOut(BaseModel):
    code: str
    level: str
    component: str
    meaning: str
    what_to_do: str | None = None


class EventsOut(BaseModel):
    events: list[LogEventOut]


class AroundOut(BaseModel):
    lines: list[LogLineOut]
    focus: int


class OkOut(BaseModel):
    ok: bool


def _store(request: Request) -> Any:
    state: AppState = request.app.state.app_state
    return state.log_store


def _row_dict(row: sqlite3.Row) -> LogLineOut:
    line = {c: row[c] for c in (*COLUMNS, "id")}
    event = logevents.EVENTS.get(line.get("event") or "")
    if event is not None:
        line["meaning"] = event.meaning
        line["what_to_do"] = event.what_to_do
    return LogLineOut(**line)


@router.get("")
def list_logs(
    request: Request,
    level: str | None = None,
    component: str | None = None,
    event: str | None = None,
    pair: str | None = None,
    cycle_id: str | None = None,
    request_id: str | None = None,
    error_id: str | None = None,
    task: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    after: str | None = None,
    limit: int = Query(default=_PAGE, ge=1, le=1000),
) -> LogsListOut:
    store = _store(request)
    clauses: list[str] = []
    params: list[Any] = []
    if level:
        names = [x.strip().upper() for x in level.split(",") if x.strip()]
        if names:
            clauses.append("level IN (" + ",".join("?" * len(names)) + ")")
            params.extend(names)
    for col, val in (
        # The logger column holds the component name (agent.scheduler, ...).
        ("logger", component),
        ("event", event),
        ("pair", pair),
        ("cycle_id", cycle_id),
        ("request_id", request_id),
        ("error_id", error_id),
        ("task", task),
    ):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    if until:
        clauses.append("ts <= ?")
        params.append(until)
    if q:
        clauses.append(
            "id IN (SELECT rowid FROM log_fts WHERE log_fts MATCH ?)"
        )
        params.append('"' + q.replace('"', '""') + '"')
    if after:
        try:
            ts0, id0 = after.rsplit("|", 1)
            clauses.append("(ts < ? OR (ts = ? AND id < ?))")
            params.extend([ts0, ts0, int(id0)])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="bad after cursor") from None
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        with store.connect_readonly() as conn:
            rows = list(
                conn.execute(
                    f"SELECT * FROM log_lines {where} "
                    "ORDER BY ts DESC, id DESC LIMIT ?",
                    (*params, limit + 1),
                )
            )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="log store read failed") from exc
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = f"{last['ts']}|{last['id']}"
    return LogsListOut(lines=[_row_dict(r) for r in rows], next=next_cursor)


@router.get("/summary")
def summary(request: Request) -> LogSummaryOut:
    store = _store(request)
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    with store.connect_readonly() as conn:
        counts = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT level, COUNT(*) FROM log_lines WHERE ts >= ? GROUP BY level",
                (since,),
            )
        }
        last_error = conn.execute(
            "SELECT MAX(ts) FROM log_lines WHERE levelno >= ?", (logging.ERROR,)
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM log_lines").fetchone()[0]
    return LogSummaryOut(
        counts_24h=counts,
        last_error_at=last_error,
        total_lines=total,
        db_bytes=store._file_size(),
        broken=store._broken,
        dropped=dict(store.queue.dropped),
    )


@router.get("/problems")
def problems(request: Request, state: str | None = None) -> ProblemsOut:
    store = _store(request)
    where = "WHERE state = ?" if state else ""
    params = (state,) if state else ()
    with store.connect_readonly() as conn:
        rows = list(
            conn.execute(
                "SELECT * FROM log_problems " + where + " ORDER BY last_seen DESC",
                params,
            )
        )
    out = []
    for r in rows:
        p = dict(r)
        p["pairs"] = json.loads(p.get("pairs") or "[]")
        event = logevents.EVENTS.get(p.get("event") or "")
        if event is not None:
            p["meaning"] = event.meaning
            p["what_to_do"] = event.what_to_do
        out.append(LogProblemOut(**p))
    return ProblemsOut(problems=out)


class ReviewBody(BaseModel):
    state: str
    note: str = ""


@router.post("/problems/{fingerprint}/review", dependencies=[Depends(require_token)])
def review_problem(
    fingerprint: str, body: ReviewBody, request: Request
) -> OkOut:
    store = _store(request)
    try:
        ok = store.review_problem(
            fingerprint, body.state, body.note, datetime.now(UTC)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="no problem with that fingerprint")
    return OkOut(ok=True)


@router.get("/events")
def events() -> EventsOut:
    return EventsOut(
        events=[LogEventOut(**e.as_dict()) for e in logevents.EVENTS.values()]
    )


@router.get("/export.jsonl")
def export_jsonl(request: Request, day: str | None = None) -> StreamingResponse:
    store = _store(request)
    if day is None:
        day = datetime.now(UTC).date().isoformat()
    start, end = day + "T00:00:00", day + "T23:59:59.999999"

    def gen() -> Iterator[str]:
        conn = store.connect_readonly()
        try:
            for row in conn.execute(
                "SELECT * FROM log_lines WHERE ts >= ? AND ts <= ? "
                "ORDER BY ts, id LIMIT ?",
                (start, end, _EXPORT_LIMIT),
            ):
                yield json.dumps({c: row[c] for c in (*COLUMNS, "id")}) + "\n"
        finally:
            conn.close()

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="logs-{day}.jsonl"'
        },
    )


class ClientReport(BaseModel):
    level: str = "error"
    message: str
    component: str | None = None
    data: dict[str, Any] | None = None


_client_hits: deque[float] = deque(maxlen=_CLIENT_LIMIT_PER_MIN)


@router.post("/client", dependencies=[Depends(require_token)])
def client_report(body: ClientReport, request: Request) -> OkOut:
    """The frontend reports errors and warnings here — at most ten a
    minute, then 429 with Retry-After."""
    now = time.monotonic()
    while _client_hits and now - _client_hits[0] > 60:
        _client_hits.popleft()
    if len(_client_hits) >= _CLIENT_LIMIT_PER_MIN:
        raise HTTPException(
            status_code=429,
            detail="Too many reports — try again in a minute.",
            headers={"Retry-After": "60"},
        )
    _client_hits.append(now)
    levelno = logging.ERROR if body.level.lower() != "warning" else logging.WARNING
    logging.getLogger("frontend.client").log(
        levelno,
        "client: %s", body.message[:2000],
        extra={
            "event": "frontend.error",
            "component": "frontend",
            "data": {"component": body.component, **(body.data or {})},
        },
    )
    return OkOut(ok=True)


@router.get("/{line_id}")
def get_line(line_id: int, request: Request) -> LogLineOut:
    store = _store(request)
    with store.connect_readonly() as conn:
        row = conn.execute(
            "SELECT * FROM log_lines WHERE id = ?", (line_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no log line with that id")
    return _row_dict(row)


@router.get("/{line_id}/around")
def around_line(line_id: int, request: Request) -> AroundOut:
    store = _store(request)
    with store.connect_readonly() as conn:
        focus = conn.execute(
            "SELECT * FROM log_lines WHERE id = ?", (line_id,)
        ).fetchone()
        if focus is None:
            raise HTTPException(status_code=404, detail="no log line with that id")
        before = list(
            conn.execute(
                "SELECT * FROM log_lines WHERE id < ? ORDER BY id DESC LIMIT ?",
                (line_id, _AROUND),
            )
        )
        after = list(
            conn.execute(
                "SELECT * FROM log_lines WHERE id > ? ORDER BY id LIMIT ?",
                (line_id, _AROUND),
            )
        )
    rows = [*list(reversed(before)), focus, *after]
    return AroundOut(lines=[_row_dict(r) for r in rows], focus=line_id)
