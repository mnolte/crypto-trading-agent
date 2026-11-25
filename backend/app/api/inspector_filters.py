"""Shared filter, cursor and source helpers for the LLM inspector."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db import AMSTERDAM, DecisionRow, LlmCallRow, OutcomeRow, utcnow

DEFAULT_SOURCES = ("live", "text", "ask")
SOURCES = (*DEFAULT_SOURCES, "shadow", "replay")


@dataclass
class InspectorFilters:
    period: str = "24h"
    start: datetime | None = None
    end: datetime | None = None
    status: str | None = None
    sources: list[str] | None = None
    pair: str | None = None
    action: str | None = None
    reason: str | None = None
    model: str | None = None
    prompt: str | None = None
    verdict: str | None = None
    outcome: str | None = None
    attention: str | None = None
    cycle: int | None = None
    issues: bool = False
    skipped: bool = False


def _local_day(value: datetime, end: bool = False) -> datetime:
    local = value.astimezone(AMSTERDAM)
    boundary = datetime.combine(local.date(), time.max if end else time.min, tzinfo=AMSTERDAM)
    return boundary.astimezone(UTC).replace(tzinfo=None)


def parse_period(
    period: str, start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime | None]:
    now = utcnow()
    if period == "all":
        return None, None
    if period == "custom":
        if start is not None:
            start = _local_day(start)
        if end is not None:
            end = _local_day(end, end=True)
        return start, end
    delta = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}.get(
        period
    )
    return (now - delta, now) if delta is not None else (now - timedelta(hours=24), now)


def split_values(values: list[str] | str | None) -> list[str] | None:
    if values is None:
        return None
    raw = values if isinstance(values, list) else [values]
    out = [part.strip() for value in raw for part in value.split(",") if part.strip()]
    return out or None


def source_expression(sources: list[str] | None):
    source_list = sources or list(DEFAULT_SOURCES)
    conds = []
    if "live" in source_list:
        conds.append(LlmCallRow.feature == "decision")
    if "shadow" in source_list:
        conds.append(LlmCallRow.feature.like("shadow:%"))
    if "replay" in source_list:
        conds.append(LlmCallRow.feature == "replay")
    if "text" in source_list:
        conds.append(LlmCallRow.feature.in_(("briefing", "review")))
    if "ask" in source_list:
        conds.append(LlmCallRow.feature == "ask")
    return or_(*conds) if conds else LlmCallRow.id == "__none__"


def source_name(feature: str | None) -> str | None:
    if feature == "decision":
        return "live"
    if feature and feature.startswith("shadow:"):
        return "shadow"
    if feature == "replay":
        return "replay"
    if feature in ("briefing", "review"):
        return "text"
    if feature == "ask":
        return "ask"
    return None


def conditions(f: InspectorFilters, *, skip: str | None = None) -> list[Any]:
    conds: list[Any] = []
    if f.start is not None:
        conds.append(LlmCallRow.at >= f.start)
    if f.end is not None:
        conds.append(LlmCallRow.at <= f.end)
    if skip != "status" and f.status:
        if f.status in ("success", "warning", "error"):
            conds.append(LlmCallRow.outcome_status == f.status)
        else:
            conds.append(LlmCallRow.status == f.status)
    if skip != "source":
        conds.append(source_expression(f.sources))
    if skip != "pair" and f.pair:
        conds.append(LlmCallRow.pair == f.pair)
    if skip != "model" and f.model:
        conds.append(LlmCallRow.model == f.model)
    if skip != "prompt" and f.prompt:
        conds.append(LlmCallRow.prompt_version == f.prompt)
    if skip != "attention" and f.attention:
        conds.append(LlmCallRow.attention == f.attention)
    if skip != "verdict" and f.verdict:
        if f.verdict == "unreviewed":
            conds.append(LlmCallRow.verdict.is_(None))
        else:
            conds.append(LlmCallRow.verdict == f.verdict)
    if f.cycle is not None:
        conds.append(LlmCallRow.cycle == f.cycle)
    if f.issues:
        conds.append(LlmCallRow.outcome_status.in_(("warning", "error")))
    if skip != "reason" and f.reason:
        if ":" in f.reason:
            code, detail = f.reason.split(":", 1)
            needle = f'%"code":"{code}","detail":"{detail}"%'
        else:
            needle = f'%"code":"{f.reason}"%'
        conds.append(LlmCallRow.reasons.like(needle))
    if skip != "action" and f.action:
        decision_ids = select(DecisionRow.id).where(DecisionRow.action == f.action)
        action_conds = [LlmCallRow.decision_id.in_(decision_ids)]
        if f.action == "HOLD":
            action_conds.append(
                and_(LlmCallRow.decision_id.is_(None), LlmCallRow.status == "input_blocked")
            )
        conds.append(or_(*action_conds))
    if skip != "outcome" and f.outcome:
        labels = select(OutcomeRow.decision_id).where(
            OutcomeRow.horizon == "72h", OutcomeRow.label == f.outcome
        )
        if f.outcome in ("accepted", "rejected"):
            # Backward compatibility: the old endpoint used outcome as a
            # post-check status, not as the 72h outcome label.
            conds.append(
                LlmCallRow.status == ("ok" if f.outcome == "accepted" else "invalid_output")
            )
        else:
            conds.append(LlmCallRow.decision_id.in_(labels))
    return conds


def encode_cursor(at: datetime, row_id: str) -> str:
    raw = json.dumps([at.isoformat(), row_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        at_raw, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(at_raw).replace(tzinfo=None), str(row_id)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


def cursor_condition(cursor: str, sort: str):
    at, row_id = decode_cursor(cursor)
    if sort == "oldest":
        return or_(LlmCallRow.at > at, and_(LlmCallRow.at == at, LlmCallRow.id > row_id))
    return or_(LlmCallRow.at < at, and_(LlmCallRow.at == at, LlmCallRow.id < row_id))


def skips_conditions(f: InspectorFilters) -> list[Any] | None:
    """Decision-row filters for cycles without a call, or None when folded rows cannot match."""
    if not f.skipped:
        return None
    blocking = (f.status, f.reason, f.model, f.prompt, f.verdict, f.outcome, f.attention, f.issues)
    if any(blocking) or (f.sources and "live" not in f.sources):
        return None
    conds: list[Any] = [DecisionRow.prefilter.is_(False)]
    if f.start is not None:
        conds.append(DecisionRow.at >= f.start)
    if f.end is not None:
        conds.append(DecisionRow.at <= f.end)
    if f.pair:
        conds.append(DecisionRow.pair == f.pair)
    if f.action:
        conds.append(DecisionRow.action == f.action)
    if f.cycle is not None:
        conds.append(DecisionRow.cycle == f.cycle)
    return conds


def skip_cursor_condition(cursor: str, sort: str):
    at, row_id = decode_cursor(cursor)
    if sort == "oldest":
        return or_(DecisionRow.at > at, and_(DecisionRow.at == at, DecisionRow.id > row_id))
    return or_(DecisionRow.at < at, and_(DecisionRow.at == at, DecisionRow.id < row_id))


def outcome_labels(session: Session, decision_ids: list[str]) -> dict[str, str]:
    if not decision_ids:
        return {}
    rows = session.execute(
        select(OutcomeRow.decision_id, OutcomeRow.label).where(
            OutcomeRow.decision_id.in_(decision_ids), OutcomeRow.horizon == "72h"
        )
    ).all()
    return dict(rows)
