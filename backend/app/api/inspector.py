"""The LLM inspector: every model call, auditable.

``GET /api/llm-calls`` lists calls newest-first and, with ``skipped=true``,
folds in the decision rows where the Rules pre-filter never sent one. Each
call opens with the exact request bytes, the input checks, the raw
response, the output checks, retries, tokens, latency and cost; replays
re-send the stored request and link back to the original; a verdict marks
a call correct or wrong for the JSONL export.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent import budget
from app.agent.brief import BriefMeta
from app.agent.deciders.openai_decider import check_output, parse_model_output
from app.agent.openai_client import OpenAIResponsesClient
from app.agent.prompts import PromptNotFoundError, load_prompt
from app.api.deps import AppState, get_state, require_token
from app.config import get_secret
from app.db import AMSTERDAM, DecisionRow, LlmCallRow, OutcomeRow, utcnow
from app.models import (
    CheckOut,
    CostsOut,
    LlmCallAttemptOut,
    LlmCallDetail,
    LlmCallListItem,
    LlmCallListOut,
    LlmCallReplayIn,
    LlmCallVerdictIn,
    OutcomeScorecardOut,
    OutcomeScorecardRow,
    Regime,
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
    )


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
    schema = body.get("text", {}).get("format", {}).get("schema")
    return (
        body.get("instructions"),
        body.get("input"),
        json.dumps(schema) if schema is not None else None,
    )


def _attempts(raw: str | None) -> list[LlmCallAttemptOut]:
    out: list[LlmCallAttemptOut] = []
    for a in json.loads(raw or "[]"):
        out.append(LlmCallAttemptOut.model_validate(a))
    return out


def _call_detail(row: LlmCallRow, session: Session) -> LlmCallDetail:
    system_prompt, brief, schema = _request_parts(row)
    replays = repo.list_replays(session, row.id)
    orig_action = _parsed_action(row)
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
    skipped: bool = True,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LlmCallListOut:
    """Call history plus, when ``skipped``, the cycles the pre-filter
    decided not to send — merged newest-first."""
    state = get_state(request)
    session = _session(state)
    try:
        account_id = _account_id(state)
        if issues:
            rows = repo.list_llm_calls_with_issues(
                session, account_id, pair=pair, before=before, limit=limit
            )
        else:
            rows = repo.list_llm_calls(
                session,
                account_id,
                pair=pair,
                status=status,
                verdict=verdict,
                outcome=outcome,
                before=before,
                limit=limit,
            )
        actions = repo.decision_actions(session, [r.decision_id for r in rows if r.decision_id])
        items = [_call_item(r, actions.get(r.decision_id or "")) for r in rows]
        if skipped and status is None and verdict is None and outcome is None and not issues:
            skips = repo.list_prefilter_skips(
                session, account_id, pair=pair, before=before, limit=limit
            )
            items += [_skipped_item(r) for r in skips]
            items.sort(key=lambda i: i.at, reverse=True)
            items = items[:limit]
        return LlmCallListOut(items=items)
    finally:
        session.close()


# -------------------------------------------------------------------- detail


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


@router.get("/api/llm-calls/{call_id}", response_model=LlmCallDetail)
def get_call(request: Request, call_id: str) -> LlmCallDetail:
    state = get_state(request)
    session = _session(state)
    try:
        row = repo.get_llm_call(session, call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown call")
        return _call_detail(row, session)
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
    """Rebuild the check meta a stored brief implies — replays don't have
    live candles, so every value comes from the stored JSON."""
    tf = brief.get("tf", {})
    atr_pct = None
    h1 = tf.get("1h", {})
    if isinstance(h1.get("atr_pct"), dict):
        atr_pct = h1["atr_pct"].get("value")
    price = float(brief.get("price") or 0)
    atr_1h = (atr_pct * price / 100) if atr_pct and price else None
    data_age = brief.get("data_age_s", {})
    limits = brief.get("limits", {}).get("max_age_s", {})
    regime_data = brief.get("regime", {})
    neutral = Regime(trend="sideways", volatility="normal", age_4h=0)
    return BriefMeta(
        pair=brief.get("pair", ""),
        now=datetime.fromisoformat(brief["now"]) if brief.get("now") else utcnow(),
        price=price,
        atr_1h=atr_1h,
        holding=brief.get("position") is not None,
        regime=Regime.model_validate(regime_data["pair"]) if regime_data.get("pair") else neutral,
        btc_regime=Regime.model_validate(regime_data["btc"]) if regime_data.get("btc") else None,
        bars={},
        data_age_s={k: float(v) for k, v in data_age.items()},
        stale_tfs=[tf_ for tf_, age in data_age.items() if age > limits.get(tf_, 0)],
        support=[lvl["price"] for lvl in brief.get("structure", {}).get("support", [])],
        resistance=[lvl["price"] for lvl in brief.get("structure", {}).get("resistance", [])],
    )


@router.post("/api/llm-calls/{call_id}/replay", dependencies=[Depends(require_token)])
async def replay_call(request: Request, call_id: str, body: LlmCallReplayIn) -> LlmCallDetail:
    """Re-send the stored request, optionally on another model or prompt
    version. Replays count against the daily budget and pause at 80% of the
    monthly cap."""
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
        spent = budget.month_spend_all(session, now)
        if budget.cap_state(spent, settings.model.monthly_cost_cap_eur) != "ok":
            raise HTTPException(
                status_code=409,
                detail="replays pause at 80% of the monthly cost cap",
            )
        try:
            prompt = load_prompt(body.prompt_version or orig.prompt_version)
        except PromptNotFoundError:
            raise HTTPException(status_code=404, detail="unknown prompt version") from None
        orig_request = json.loads(orig.request_json)
        payload = {
            "model": body.model or orig.model,
            "instructions": prompt.body,
            "input": orig_request.get("input"),
            "temperature": prompt.temperature,
            "max_output_tokens": prompt.max_output_tokens,
            "text": orig_request.get("text"),
        }
        client = OpenAIResponsesClient(api_key)
        try:
            result = await client.call(payload)
        finally:
            await client.aclose()

        brief_json = json.loads(orig_request.get("input") or "{}")
        meta = _meta_from_brief(brief_json)
        out, parse_error = parse_model_output(result.output_text)
        # The Rules baseline isn't re-evaluated for replays — its inputs are
        # not part of the stored request.
        verdict = check_output(out, parse_error, brief_json, meta, "?", settings)
        for i, check in enumerate(verdict.checks):
            if check.name == "Rules baseline":
                verdict.checks[i] = CheckOut(
                    name="Rules baseline",
                    status="pass",
                    detail="not evaluated for a replay",
                )
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
            status=result.status if out is not None or result.status != "ok" else "invalid_output",
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
            output_checks=json.dumps([c.model_dump() for c in verdict.checks]),
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
