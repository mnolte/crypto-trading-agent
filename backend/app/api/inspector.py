"""The LLM inspector: every model call, auditable.

``GET /api/llm-calls`` lists calls newest-first and, with ``skipped=true``,
folds in the decision rows where the Rules pre-filter never sent one. Each
call opens with the exact request bytes, the input checks, the raw
response, the output checks, retries, tokens, latency and cost; replays
re-send the stored request and link back to the original; a verdict marks
a call correct or wrong for the JSONL export.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import asc, desc, func, select
from sqlalchemy.orm import Session

from app import repo
from app.agent import budget, reasons
from app.agent.brief import BriefMeta
from app.agent.brief_sections import sections as brief_sections
from app.agent.brief_v2 import brief_version_of
from app.agent.openai_client import OpenAIResponsesClient
from app.agent.prompts import PromptNotFoundError, load_prompt
from app.agent.replay import (
    evaluate_replay,
    meta_from_brief,
    replay_request,
)
from app.api.deps import AppState, get_state, require_token
from app.api.inspector_filters import (
    InspectorFilters,
    conditions,
    cursor_condition,
    encode_cursor,
    outcome_labels,
    parse_period,
    skip_cursor_condition,
    skips_conditions,
    source_name,
    split_values,
)
from app.config import get_secret
from app.db import AMSTERDAM, DecisionRow, LlmCallRow, OutcomeRow, utcnow
from app.features.briefings import text_numbers_ok
from app.models import (
    CheckOut,
    CostsOut,
    LlmCallAttemptOut,
    LlmCallDetail,
    LlmCallListItem,
    LlmCallListOut,
    LlmCallReasonOut,
    LlmCallReplayIn,
    LlmCallSummaryOut,
    LlmCallTopReasonOut,
    LlmCallVerdictIn,
    OutcomeScorecardOut,
    OutcomeScorecardRow,
    ReplayInfo,
)

router = APIRouter()

# Calls that share the daily call budget are decided in agent/budget.py.
# Features bucketed for /api/costs: decisions, ask, briefings, shadows, replays.


def _session(state: AppState) -> Session:
    return state.session_factory()


def _account_id(state: AppState) -> int:
    return state.broker.account_id if state.broker is not None else state.account_id


def _checks(raw: str | None) -> list[CheckOut]:
    out: list[CheckOut] = []
    for item in json.loads(raw or "[]"):
        out.append(CheckOut.model_validate(item))
    return out


def _issue_count(row: LlmCallRow) -> int:
    return sum(
        1
        for c in _checks(row.input_checks) + _checks(row.output_checks)
        if c.status in ("warn", "fail")
    )


def _parsed_action(row: LlmCallRow) -> str | None:
    if not row.parsed:
        return None
    try:
        action = json.loads(row.parsed).get("action")
    except (json.JSONDecodeError, AttributeError):
        return None
    return action if action in ("BUY", "SELL", "HOLD") else None


def _call_item(row: LlmCallRow, decision_action: str | None = None) -> LlmCallListItem:
    final_action: str | None = _parsed_action(row)
    if decision_action is not None:
        final_action = decision_action
    elif row.decision_id is None and row.status == "input_blocked":
        final_action = "HOLD"
    return LlmCallListItem(
        kind="call",
        id=row.id,
        at=repo.aware_utc(row.at),
        pair=row.pair,
        feature=row.feature,
        model=row.model,
        prompt_version=row.prompt_version,
        status=row.status,
        action=_parsed_action(row),
        final_action=final_action,
        issues=_issue_count(row),
        verdict=row.verdict,
        decision_id=row.decision_id,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost_eur=Decimal(row.cost_eur) if row.cost_eur else None,
        latency_ms=row.latency_ms,
        source=source_name(row.feature),
        outcome_status=row.outcome_status,
        reasons=_reason_items(row.reasons),
        attention=row.attention,
    )


def _reason_items(raw: str | None) -> list[LlmCallReasonOut]:
    items: list[LlmCallReasonOut] = []
    for item in json.loads(raw or "[]"):
        meta = reasons.REASONS.get(item.get("code"), {})
        items.append(
            LlmCallReasonOut(
                code=item.get("code", ""),
                label=meta.label if meta else item.get("code", ""),
                detail=item.get("detail"),
            )
        )
    return items


def _skipped_item(row: Any) -> LlmCallListItem:
    """A decision row where the pre-filter passed nothing to the model."""
    return LlmCallListItem(
        kind="skipped",
        id=row.id,
        at=repo.aware_utc(row.at),
        pair=row.pair,
        feature=None,
        model=None,
        prompt_version=None,
        status=None,
        action=row.action,
        final_action=row.action,
        issues=0,
        verdict=None,
        decision_id=row.id,
        source="live",
        outcome_status="success",
        attention="none",
    )


def _request_parts(row: LlmCallRow) -> tuple[str | None, str | None, str | None]:
    """(system_prompt, brief, output_schema) extracted from the stored
    request — for the input/output panes."""
    if not row.request_json:
        return None, None, None
    try:
        body = json.loads(row.request_json)
    except json.JSONDecodeError:
        return None, None, None
    text = body.get("text") or {}
    schema = (text.get("format") or {}).get("schema")
    instructions = body.get("instructions")
    brief = body.get("input")
    if brief is not None and not isinstance(brief, str):
        brief = json.dumps(brief)  # Ask calls store a message list
    return (
        instructions if isinstance(instructions, str) else None,
        brief,
        json.dumps(schema) if schema is not None else None,
    )


def _attempts(raw: str | None) -> list[LlmCallAttemptOut]:
    out: list[LlmCallAttemptOut] = []
    for a in json.loads(raw or "[]"):
        out.append(LlmCallAttemptOut.model_validate(a))
    return out


def _explanations(row: LlmCallRow, session: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    stored = reasons.parse_reasons(row.reasons)
    cfg = repo.load_settings(session).inspector.attention
    attention_why = reasons.attention_why(session, row, stored, cfg)
    base_links = []
    if row.decision_id:
        base_links.append({"label": "Decision", "href": f"/?decision={row.decision_id}"})
    if row.cycle is not None:
        base_links.append({"label": "Cycle in Logs", "href": f"/logs?cycle_id=c-{row.cycle:04d}"})
    for item in stored:
        code = str(item.get("code") or "")
        meta = reasons.REASONS.get(code)
        links = [{"label": "Calls with this reason", "href": f"/inspector?reason={code}&period=7d"}]
        links.extend(base_links)
        out.append(
            {
                "code": code,
                "label": meta.label if meta else code,
                "meaning": meta.meaning if meta else "",
                "likely_cause": meta.likely_cause if meta else "",
                "why_here": reasons.why_here(item, row),
                "what_to_do": meta.what_to_do if meta else "",
                "attention": row.attention or "none",
                "attention_why": attention_why,
                "links": links,
            }
        )
    return out


def _prompt_hash_match(row: LlmCallRow, system_prompt: str | None) -> bool | None:
    if not system_prompt or not row.prompt_version:
        return None
    try:
        prompt = load_prompt(row.prompt_version)
    except PromptNotFoundError:
        return None
    return (
        hashlib.sha256(prompt.body.encode()).hexdigest()
        == hashlib.sha256(system_prompt.encode()).hexdigest()
    )


def _call_detail(row: LlmCallRow, session: Session) -> LlmCallDetail:
    system_prompt, brief, schema = _request_parts(row)
    replays = repo.list_replays(session, row.id)
    orig_action = _parsed_action(row)
    upgraded: str | None = None
    if row.replay_of:
        orig = repo.get_llm_call(session, row.replay_of)
        if orig is not None and orig.request_json and brief:
            try:
                orig_brief = json.loads(orig.request_json).get("input")
                orig_brief = json.loads(orig_brief) if isinstance(orig_brief, str) else {}
                have, want = brief_version_of(orig_brief), brief_version_of(json.loads(brief))
                if want > have:
                    upgraded = f"v{have} → v{want}"
            except (json.JSONDecodeError, TypeError):
                pass
    decision_action = None
    if row.decision_id:
        decision = repo.get_decision_row(session, row.decision_id)
        if decision is not None:
            decision_action = decision.action
    parsed: dict[str, Any] | None = None
    if row.parsed:
        try:
            parsed = json.loads(row.parsed)
        except json.JSONDecodeError:
            parsed = {"_unparseable": row.parsed}
    result_sentence = {
        "ok": "The model answered and its output passed the checks.",
        "input_blocked": "The input checks stopped the call before it was sent.",
        "invalid_output": "The model answered, but its output failed the checks.",
        "error": "The OpenAI call failed.",
        "timeout": "The OpenAI call timed out.",
    }.get(row.status, "The call ended with this status.")
    final_sentence = (
        f"The final action was {decision_action or _parsed_action(row) or 'HOLD'}."
        if row.feature == "decision"
        else "This call does not decide a trade."
    )
    return LlmCallDetail(
        id=row.id,
        at=repo.aware_utc(row.at),
        cycle=row.cycle,
        pair=row.pair,
        decision_id=row.decision_id,
        feature=row.feature,
        replay_of=row.replay_of,
        model=row.model,
        prompt_version=row.prompt_version,
        status=row.status,
        latency_ms=row.latency_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost_usd=Decimal(row.cost_usd) if row.cost_usd else None,
        cost_eur=Decimal(row.cost_eur) if row.cost_eur else None,
        request_id=row.request_id,
        request_json=row.request_json,
        response_raw=row.response_raw,
        system_prompt=system_prompt,
        brief=brief,
        output_schema=schema,
        parsed=parsed,
        input_checks=_checks(row.input_checks),
        output_checks=_checks(row.output_checks),
        attempts=_attempts(row.attempts),
        replays=[
            ReplayInfo(
                id=r.id,
                at=repo.aware_utc(r.at),
                model=r.model,
                prompt_version=r.prompt_version,
                action=_parsed_action(r),
                differs=_parsed_action(r) != orig_action,
            )
            for r in replays
        ],
        decision_action=decision_action,
        verdict=row.verdict,
        verdict_note=row.verdict_note,
        verdict_at=repo.aware_utc(row.verdict_at) if row.verdict_at else None,
        brief_upgraded=upgraded,
        outcome_status=row.outcome_status or "success",
        reasons=_reason_items(row.reasons),
        attention=row.attention or "none",
        explained_reasons=_explanations(row, session),
        result_sentence=result_sentence,
        final_action_sentence=final_sentence,
        sections=brief_sections(row.request_json) if row.request_json else [],
        prompt_hash_match=_prompt_hash_match(row, system_prompt),
        payload_cleared=row.request_json is None and row.response_raw is None,
        newer_id=None,
        older_id=None,
        outside_filters=False,
    )


# --------------------------------------------------------------------- list


@router.get("/api/llm-calls", response_model=LlmCallListOut)
def list_calls(
    request: Request,
    pair: str | None = None,
    status: str | None = None,
    issues: bool = False,
    verdict: Literal["correct", "wrong", "unreviewed"] | None = None,
    outcome: str | None = None,
    skipped: bool = False,
    before: datetime | None = None,
    period: str = "24h",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    source: Annotated[list[str] | None, Query()] = None,
    action: Literal["BUY", "SELL", "HOLD"] | None = None,
    reason: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    attention: Literal["act", "watch", "none"] | None = None,
    cycle: int | None = None,
    cursor: str | None = None,
    sort: Literal["newest", "oldest", "new", "old"] = "newest",
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> LlmCallListOut:
    """Filtered call history with stable keyset pagination. Rows whose cycle
    never reached the model fold in only when ``skipped=true``."""
    state = get_state(request)
    session = _session(state)
    try:
        settings = state.settings
        page_size = limit or settings.inspector.page_size
        start_at, end_at = parse_period(period, from_, to)
        filters = InspectorFilters(
            period=period,
            start=start_at,
            end=end_at,
            status=status,
            sources=split_values(source),
            pair=pair,
            action=action,
            reason=reason,
            model=model,
            prompt=prompt,
            verdict=verdict,
            outcome=outcome,
            attention=attention,
            cycle=cycle,
            issues=issues,
            skipped=skipped,
        )
        base_conds = conditions(filters)
        conds = list(base_conds)
        if before is not None:
            conds.append(LlmCallRow.at < before.replace(tzinfo=None))
        if cursor:
            try:
                conds.append(cursor_condition(cursor, sort))
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid cursor") from None
        sort = "oldest" if sort == "old" else "newest" if sort == "new" else sort
        ordering = (
            (asc(LlmCallRow.at), asc(LlmCallRow.id))
            if sort == "oldest"
            else (desc(LlmCallRow.at), desc(LlmCallRow.id))
        )
        rows = session.scalars(
            select(LlmCallRow).where(*conds).order_by(*ordering).limit(page_size + 1)
        ).all()
        items = []
        actions = repo.decision_actions(session, [r.decision_id for r in rows if r.decision_id])
        labels = outcome_labels(session, [r.decision_id for r in rows if r.decision_id])
        for row in rows:
            item = _call_item(row, actions.get(row.decision_id or ""))
            item.outcome_label = labels.get(row.decision_id or "")
            item.fold_key = reasons.fold_key(row, item.final_action)
            items.append(item)

        skip_conds = skips_conditions(filters)
        skip_rows = []
        if skip_conds is not None:
            if before is not None:
                skip_conds.append(DecisionRow.at < before.replace(tzinfo=None))
            if cursor:
                skip_conds.append(skip_cursor_condition(cursor, sort))
            skip_ordering = (
                (asc(DecisionRow.at), asc(DecisionRow.id))
                if sort == "oldest"
                else (desc(DecisionRow.at), desc(DecisionRow.id))
            )
            skip_rows = session.scalars(
                select(DecisionRow).where(*skip_conds).order_by(*skip_ordering).limit(page_size + 1)
            ).all()
            items.extend(_skipped_item(row) for row in skip_rows)

        reverse = sort == "newest"
        items.sort(key=lambda item: (item.at.replace(tzinfo=None), item.id), reverse=reverse)
        has_more = len(items) > page_size
        items = items[:page_size]
        next_cursor = (
            encode_cursor(items[-1].at.replace(tzinfo=None), items[-1].id)
            if has_more and items
            else None
        )
        total = session.scalar(select(func.count()).select_from(LlmCallRow).where(*base_conds)) or 0
        total_skip_conds = skips_conditions(filters)
        if total_skip_conds is not None:
            if before is not None:
                total_skip_conds.append(DecisionRow.at < before.replace(tzinfo=None))
            total += (
                session.scalar(
                    select(func.count()).select_from(DecisionRow).where(*total_skip_conds)
                )
                or 0
            )
        return LlmCallListOut(items=items, next_cursor=next_cursor, total=total)
    finally:
        session.close()


# -------------------------------------------------------------------- detail


@router.get("/api/llm-calls/summary", response_model=LlmCallSummaryOut)
def summarize_calls(
    request: Request,
    period: str = "24h",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    source: Annotated[list[str] | None, Query()] = None,
    pair: str | None = None,
    status: str | None = None,
    action: str | None = None,
    reason: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    verdict: str | None = None,
    outcome: str | None = None,
    attention: str | None = None,
    cycle: int | None = None,
    issues: bool = False,
) -> LlmCallSummaryOut:
    """Counts, cost, latency, buckets and filter-option counts for the same filters."""
    state = get_state(request)
    session = _session(state)
    try:
        start_at, end_at = parse_period(period, from_, to)
        f = InspectorFilters(
            period=period,
            start=start_at,
            end=end_at,
            status=status,
            sources=split_values(source),
            pair=pair,
            action=action,
            reason=reason,
            model=model,
            prompt=prompt,
            verdict=verdict,
            outcome=outcome,
            attention=attention,
            cycle=cycle,
            issues=issues,
        )
        rows = session.scalars(select(LlmCallRow).where(*conditions(f))).all()
        total = len(rows)
        counts = {
            "success": sum(r.outcome_status == "success" for r in rows),
            "warning": sum(r.outcome_status == "warning" for r in rows),
            "error": sum(r.outcome_status == "error" for r in rows),
        }
        shares = {key: (value / total if total else 0.0) for key, value in counts.items()}
        attention_counts = {
            key: sum((r.attention or "none") == key for r in rows)
            for key in ("act", "watch", "none")
        }
        skip_conds = [DecisionRow.prefilter.is_(False)]
        if start_at is not None:
            skip_conds.append(DecisionRow.at >= start_at)
        if end_at is not None:
            skip_conds.append(DecisionRow.at <= end_at)
        if pair:
            skip_conds.append(DecisionRow.pair == pair)
        cycles_without_call = (
            session.scalar(select(func.count()).select_from(DecisionRow).where(*skip_conds)) or 0
        )
        cost: dict[str, Decimal] = {
            "live": Decimal(0),
            "shadow": Decimal(0),
            "replay": Decimal(0),
            "text": Decimal(0),
            "ask": Decimal(0),
        }
        for row in rows:
            src = source_name(row.feature) or "live"
            cost[src] += Decimal(row.cost_eur or "0")
        latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)
        median = (
            int((latencies[len(latencies) // 2] + latencies[(len(latencies) - 1) // 2]) / 2)
            if latencies
            else None
        )
        p95 = latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None
        buckets: dict[str, dict[str, object]] = {}
        for row in rows:
            local = (row.at if row.at.tzinfo else row.at.replace(tzinfo=UTC)).astimezone(AMSTERDAM)
            if period == "24h":
                key = local.replace(minute=0, second=0, microsecond=0).isoformat()
            elif period in ("7d", "30d", "custom"):
                key = local.date().isoformat()
            else:
                iso = local.isocalendar()
                key = f"{iso.year}-W{iso.week:02d}"
            bucket = buckets.setdefault(key, {"at": key, "success": 0, "warning": 0, "error": 0})
            bucket[row.outcome_status or "success"] = (
                int(bucket[row.outcome_status or "success"]) + 1
            )

        reason_counts: Counter[tuple[str, str | None]] = Counter()
        examples: dict[tuple[str, str | None], LlmCallRow] = {}
        attention_by_reason: dict[tuple[str, str | None], str] = {}
        for row in sorted(rows, key=lambda r: r.at, reverse=True):
            for item in json.loads(row.reasons or "[]"):
                key = (item.get("code", ""), item.get("detail"))
                reason_counts[key] += 1
                examples.setdefault(key, row)
                attention_by_reason[key] = row.attention or "none"
        top = []
        for (code, detail), count in reason_counts.most_common(6):
            example = examples[(code, detail)]
            meta = reasons.REASONS.get(code, {})
            top.append(
                LlmCallTopReasonOut(
                    code=code,
                    label=meta.get("label", code),
                    detail=detail,
                    count=count,
                    example=(
                        f"{example.at.isoformat()} · {example.pair} · "
                        f"{_parsed_action(example) or 'HOLD'}"
                    ),
                    attention=attention_by_reason[(code, detail)],
                )
            )

        def counts_for(dimension: str) -> dict[str, int]:
            vals: Counter[str] = Counter()
            rows_dim = session.scalars(
                select(LlmCallRow).where(*conditions(f, skip=dimension))
            ).all()
            for row in rows_dim:
                if dimension == "status":
                    vals[row.outcome_status or "success"] += 1
                elif dimension == "source":
                    vals[source_name(row.feature) or "other"] += 1
                elif dimension == "pair":
                    vals[row.pair] += 1
                elif dimension == "model":
                    vals[row.model] += 1
                elif dimension == "prompt":
                    vals[row.prompt_version] += 1
                elif dimension == "verdict":
                    vals[row.verdict or "unreviewed"] += 1
                elif dimension == "attention":
                    vals[row.attention or "none"] += 1
                elif dimension == "reason":
                    for item in json.loads(row.reasons or "[]"):
                        vals[item.get("code", "")] += 1
                elif dimension == "action":
                    vals[
                        _parsed_action(row)
                        or ("HOLD" if row.status == "input_blocked" else "unknown")
                    ] += 1
                elif dimension == "outcome":
                    vals["reviewed" if row.decision_id else "none"] += 1
            return dict(vals)

        return LlmCallSummaryOut(
            calls=total,
            cycles_without_call=cycles_without_call,
            counts=counts,
            shares=shares,
            attention_counts=attention_counts,
            cost_eur=cost,
            latency_ms={"median": median, "p95": p95},
            buckets=[buckets[key] for key in sorted(buckets)],
            option_counts={
                name: counts_for(name)
                for name in (
                    "status",
                    "source",
                    "pair",
                    "action",
                    "reason",
                    "model",
                    "prompt",
                    "verdict",
                    "outcome",
                    "attention",
                )
            },
            top_reasons=top,
        )
    finally:
        session.close()


@router.get("/api/llm-calls/export.csv")
def export_calls_csv(request: Request) -> Response:
    """Metadata-only CSV export; prompts and raw payloads are never included."""
    state = get_state(request)
    session = _session(state)
    try:
        rows = session.scalars(select(LlmCallRow).order_by(desc(LlmCallRow.at)).limit(10000)).all()
        labels = outcome_labels(session, [r.decision_id for r in rows if r.decision_id])
        lines = [
            "id,at,cycle,pair,source,feature,model,prompt_version,status,outcome_status,reasons,attention,action,final_action,verdict,outcome_label,input_tokens,output_tokens,cost_eur,latency_ms"
        ]
        for row in rows:
            reason_text = ";".join(
                f"{item.get('code')}:{item.get('detail') or ''}"
                for item in json.loads(row.reasons or "[]")
            )
            values = [
                row.id,
                repo.aware_utc(row.at).isoformat(),
                row.cycle,
                row.pair,
                source_name(row.feature),
                row.feature,
                row.model,
                row.prompt_version,
                row.status,
                row.outcome_status,
                reason_text,
                row.attention,
                _parsed_action(row),
                _parsed_action(row),
                row.verdict,
                labels.get(row.decision_id or ""),
                row.input_tokens,
                row.output_tokens,
                row.cost_eur,
                row.latency_ms,
            ]
            escaped = []
            for value in values:
                text = "" if value is None else str(value)
                escaped.append(
                    '"' + text.replace('"', '""') + '"' if any(c in text for c in '",\n') else text
                )
            lines.append(",".join(escaped))
        return Response(
            content="\n".join(lines) + "\n",
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="llm-calls.csv"'},
        )
    finally:
        session.close()


@router.get("/api/llm-calls/export.jsonl")
def export_calls(
    request: Request,
    verdict: Literal["correct", "wrong", "unreviewed"] | None = None,
    outcome: str | None = None,
) -> Response:
    """Labelled calls as JSONL with their computed forward outcomes."""
    state = get_state(request)
    session = _session(state)
    try:
        account_id = _account_id(state)
        rows = repo.list_llm_calls(
            session,
            account_id,
            verdict=verdict,
            outcome=outcome,
            limit=200,
        )
        if verdict is None:
            # The export is for labelled calls; no filter given means all
            # calls with a verdict.
            rows = [r for r in rows if r.verdict is not None]
        lines = []
        for row in rows:
            _, brief, _ = _request_parts(row)
            outcomes = (
                session.scalars(
                    select(OutcomeRow)
                    .where(OutcomeRow.decision_id == row.decision_id)
                    .order_by(OutcomeRow.horizon)
                ).all()
                if row.decision_id
                else []
            )
            lines.append(
                json.dumps(
                    {
                        "id": row.id,
                        "at": repo.aware_utc(row.at).isoformat(),
                        "pair": row.pair,
                        "model": row.model,
                        "prompt_version": row.prompt_version,
                        "feature": row.feature,
                        "verdict": row.verdict,
                        "verdict_note": row.verdict_note,
                        "outcome": [
                            {
                                "horizon": item.horizon,
                                "label": item.label,
                                "forward_return_pct": item.forward_return_pct,
                                "first_touch": item.first_touch,
                            }
                            for item in outcomes
                        ],
                        "brief": json.loads(brief) if brief else None,
                        "parsed": json.loads(row.parsed) if row.parsed else None,
                    }
                )
            )
        return Response(
            content="\n".join(lines) + ("\n" if lines else ""),
            media_type="application/jsonl",
            headers={
                "Content-Disposition": 'attachment; filename="llm-calls.jsonl"',
            },
        )
    finally:
        session.close()


def _filters_from_query(request: Request) -> InspectorFilters:
    qp = request.query_params
    start_raw = qp.get("from")
    end_raw = qp.get("to")
    start = datetime.fromisoformat(start_raw).replace(tzinfo=None) if start_raw else None
    end = datetime.fromisoformat(end_raw).replace(tzinfo=None) if end_raw else None
    start, end = parse_period(qp.get("period", "24h"), start, end)
    cycle_raw = qp.get("cycle")
    return InspectorFilters(
        period=qp.get("period", "24h"),
        start=start,
        end=end,
        status=qp.get("status"),
        sources=split_values(qp.getlist("source") or qp.get("source")),
        pair=qp.get("pair"),
        action=qp.get("action"),
        reason=qp.get("reason"),
        model=qp.get("model"),
        prompt=qp.get("prompt"),
        verdict=qp.get("verdict"),
        outcome=qp.get("outcome"),
        attention=qp.get("attention"),
        cycle=int(cycle_raw) if cycle_raw else None,
        issues=qp.get("issues") == "true",
    )


@router.get("/api/llm-calls/{call_id}", response_model=LlmCallDetail)
def get_call(request: Request, call_id: str) -> LlmCallDetail:
    state = get_state(request)
    session = _session(state)
    try:
        row = repo.get_llm_call(session, call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown call")
        detail = _call_detail(row, session)
        filters = _filters_from_query(request)
        conds = conditions(filters)
        matches = session.scalar(select(LlmCallRow.id).where(*conds, LlmCallRow.id == row.id))
        detail.outside_filters = matches is None
        newer_cond = (LlmCallRow.at > row.at) | (
            (LlmCallRow.at == row.at) & (LlmCallRow.id > row.id)
        )
        older_cond = (LlmCallRow.at < row.at) | (
            (LlmCallRow.at == row.at) & (LlmCallRow.id < row.id)
        )
        detail.newer_id = session.scalar(
            select(LlmCallRow.id)
            .where(*conds, newer_cond)
            .order_by(asc(LlmCallRow.at), asc(LlmCallRow.id))
            .limit(1)
        )
        detail.older_id = session.scalar(
            select(LlmCallRow.id)
            .where(*conds, older_cond)
            .order_by(desc(LlmCallRow.at), desc(LlmCallRow.id))
            .limit(1)
        )
        return detail
    finally:
        session.close()


@router.get("/api/llm-calls/{call_id}/download")
def download_call(request: Request, call_id: str) -> Response:
    """The whole call record as one JSON file."""
    state = get_state(request)
    session = _session(state)
    try:
        row = repo.get_llm_call(session, call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown call")
        detail = _call_detail(row, session)
        return Response(
            content=detail.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="llm-call-{call_id}.json"'},
        )
    finally:
        session.close()


# ------------------------------------------------------------------- replay


def _meta_from_brief(brief: dict[str, Any]) -> BriefMeta:
    return meta_from_brief(brief)


@router.post("/api/llm-calls/{call_id}/replay", dependencies=[Depends(require_token)])
async def replay_call(request: Request, call_id: str, body: LlmCallReplayIn) -> LlmCallDetail:
    """Re-send the stored request, optionally on another model or prompt
    version. Replays count against the daily budget and pause at 80% of the
    monthly cap. A target prompt with a newer brief version gets the stored
    brief upgraded first — from candles closed by the call's own time."""
    state = get_state(request)
    session = _session(state)
    try:
        orig = repo.get_llm_call(session, call_id)
        if orig is None:
            raise HTTPException(status_code=404, detail="unknown call")
        if not orig.request_json:
            raise HTTPException(status_code=409, detail="call has no stored request")
        api_key = get_secret("openai")
        if not api_key:
            raise HTTPException(status_code=409, detail="no OpenAI key set")
        now = utcnow()
        account_id = _account_id(state)
        settings = state.settings
        used = budget.calls_today(session, account_id, now)
        if used >= settings.model.daily_call_budget:
            raise HTTPException(status_code=409, detail="daily call budget used")
        spent = budget.month_spend_strict(session, now)
        if budget.cap_state(spent, settings.model.monthly_cost_cap_eur) != "ok":
            raise HTTPException(
                status_code=409,
                detail="replays pause at 80% of the monthly cost cap",
            )
        try:
            prompt = load_prompt(body.prompt_version or orig.prompt_version)
        except PromptNotFoundError:
            raise HTTPException(status_code=404, detail="unknown prompt version") from None
        market_info = None
        engine = getattr(state, "agent_engine", None)
        if engine is not None:
            market_info = engine.market_rules.get(orig.pair)
        payload, brief_json, meta, _upgraded = replay_request(
            session,
            orig,
            prompt,
            body.model,
            settings=settings,
            market_info=market_info,
        )
        session.commit()
        client = OpenAIResponsesClient(api_key)
        try:
            result = await client.call(payload)
        finally:
            await client.aclose()

        schema_sent = ((payload.get("text") or {}).get("format") or {}).get("schema")
        if schema_sent is not None:
            # Decision-shaped calls: the post-checks re-run on the output.
            out_checks, status = evaluate_replay(
                result, brief_json, meta, settings, prompt.brief_version
            )
        else:
            # Prose calls (briefings, reviews): the number check re-runs
            # against the facts in the stored input. For Ask and anything
            # else there is nothing stored to check the text against.
            facts = brief_json.get("facts")
            if isinstance(facts, dict) and facts:
                candidate = (result.output_text or "").strip()
                bad = text_numbers_ok(candidate, facts) if candidate else ["empty"]
                out_checks = [
                    CheckOut(
                        name="Numbers match facts",
                        status="pass" if not bad else "fail",
                        detail=(
                            "" if not bad else f"not in facts: {', '.join(str(b) for b in bad[:6])}"
                        ),
                    )
                ]
            else:
                out_checks = [
                    CheckOut(
                        name="Output checks",
                        status="pass",
                        detail="not evaluated for a replay",
                    )
                ]
            status = result.status
        new_row = LlmCallRow(
            id=uuid.uuid4().hex[:16],
            at=now,
            cycle=None,
            account_id=orig.account_id,
            pair=orig.pair,
            decision_id=None,
            feature="replay",
            replay_of=orig.id,
            model=payload["model"],
            prompt_version=prompt.version,
            status=status,
            attempts=json.dumps(
                [
                    {
                        "at": a.at.isoformat(),
                        "outcome": a.outcome,
                        "error": a.error,
                        "http_status": a.http_status,
                        "latency_ms": a.latency_ms,
                    }
                    for a in result.attempts
                ]
            ),
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=None,
            cost_eur=None,
            request_id=result.request_id,
            request_json=(result.request_bytes or b"").decode("utf-8", errors="replace"),
            response_raw=(
                result.response_bytes.decode("utf-8", errors="replace")
                if result.response_bytes is not None
                else None
            ),
            parsed=result.output_text,
            input_checks=orig.input_checks,
            output_checks=json.dumps([c.model_dump() for c in out_checks]),
            verdict=None,
            verdict_note=None,
            verdict_at=None,
        )
        usd, eur = budget.cost_estimates(
            budget.ModelPrices(),
            payload["model"],
            result.input_tokens,
            result.output_tokens,
            budget.usdc_eur_rate(session, now),
        )
        new_row.cost_usd = str(usd) if usd is not None else None
        new_row.cost_eur = str(eur) if eur is not None else None
        reasons.stamp(
            session,
            new_row,
            final_action=reasons.parsed_action(new_row),
            settings=settings,
        )
        repo.add_llm_call(session, new_row)
        session.commit()
        return _call_detail(new_row, session)
    finally:
        session.close()


# ------------------------------------------------------------------ verdict


@router.put("/api/llm-calls/{call_id}/verdict", dependencies=[Depends(require_token)])
def set_verdict(request: Request, call_id: str, body: LlmCallVerdictIn) -> LlmCallDetail:
    """Mark a call Correct or Wrong, with an optional note; null clears."""
    state = get_state(request)
    session = _session(state)
    try:
        row = repo.get_llm_call(session, call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown call")
        row.verdict = body.verdict
        row.verdict_note = body.note if body.verdict else None
        row.verdict_at = utcnow() if body.verdict else None
        session.commit()
        return _call_detail(row, session)
    finally:
        session.close()


# --------------------------------------------------------------------- cost


@router.get("/api/outcomes/scorecard", response_model=OutcomeScorecardOut)
def outcome_scorecard(request: Request) -> OutcomeScorecardOut:
    state = get_state(request)
    session = _session(state)
    try:
        rows = session.execute(
            select(DecisionRow, OutcomeRow)
            .join(OutcomeRow, OutcomeRow.decision_id == DecisionRow.id)
            .where(
                DecisionRow.account_id == state.account_id,
                OutcomeRow.horizon == 72,
            )
        ).all()
        grouped: dict[tuple[str, str | None, str | None], list[tuple[DecisionRow, OutcomeRow]]] = {}
        for decision, outcome in rows:
            grouped.setdefault(
                (decision.decider, decision.model, decision.prompt_version), []
            ).append((decision, outcome))
        items: list[OutcomeScorecardRow] = []
        for (decider, model, prompt), values in grouped.items():
            buys = [outcome for decision, outcome in values if decision.action == "BUY"]
            wins = sum(outcome.label == "win" for outcome in buys)
            realised = [
                Decimal(outcome.realised_r)
                for _, outcome in values
                if outcome.realised_r is not None
            ]
            items.append(
                OutcomeScorecardRow(
                    decider=decider,
                    model=model,
                    prompt_version=prompt,
                    decisions=len(values),
                    buy_win_rate_pct=(wins / len(buys) * 100 if buys else None),
                    average_r=(float(sum(realised) / len(realised)) if realised else None),
                    missed=sum(outcome.label == "missed" for _, outcome in values),
                    avoided=sum(outcome.label == "avoided" for _, outcome in values),
                )
            )
        return OutcomeScorecardOut(items=items)
    finally:
        session.close()


@router.get("/api/costs", response_model=CostsOut)
def get_costs(request: Request, month: str | None = None) -> CostsOut:
    """Estimated model spend per feature for one month, against the cap.
    ``month`` is ``YYYY-MM`` on the Amsterdam clock (default: current)."""
    state = get_state(request)
    session = _session(state)
    try:
        now = utcnow()
        if month:
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise HTTPException(status_code=400, detail="month must be YYYY-MM")
            year, mon = (int(p) for p in month.split("-"))
            start = datetime(year, mon, 1, tzinfo=AMSTERDAM)
            ny, nm = (year + 1, 1) if mon == 12 else (year, mon + 1)
            end = datetime(ny, nm, 1, tzinfo=AMSTERDAM)
            label = month
        else:
            start, end = budget.amsterdam_month_bounds(now)
            label = now.astimezone(AMSTERDAM).strftime("%Y-%m")
        # Spend is grouped into the spec's five features across every
        # account: decisions, Ask, briefings (incl. reviews), shadows,
        # replays.
        raw = repo.llm_costs_by_feature_all(session, start, end)
        by_feature: dict[str, Decimal] = {
            f: Decimal("0") for f in ("decisions", "ask", "briefings", "shadows", "replays")
        }
        total = Decimal("0")
        for feature, spent in raw.items():
            if feature.startswith("shadow:"):
                group = "shadows"
            elif feature in ("briefing", "review"):
                group = "briefings"
            elif feature == "replay":
                group = "replays"
            elif feature == "decision":
                group = "decisions"
            else:
                group = feature
            by_feature[group] = by_feature.get(group, Decimal("0")) + spent
            total += spent
        return CostsOut(
            month=label,
            cap_eur=state.settings.model.monthly_cost_cap_eur,
            spent_eur=total,
            spent_usd=repo.llm_cost_usd_between_all(session, start, end),
            by_feature=by_feature,
        )
    finally:
        session.close()
