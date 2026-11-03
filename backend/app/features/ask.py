"""The Ask panel backend: an OpenAI model with read-only tools.

Spec (user feature 1): the tools are `get_decisions`, `get_trades`,
`get_outcomes`, `get_llm_call`, `get_performance`, `get_indicators_at`,
`get_settings`, `get_activity`, `get_snapshot`, `get_insights`,
`get_readiness` and `get_glossary` — all read-only. Ask can never change
settings, trade or pause anything; the API offers it no write path at all.
Every answer cites the records it used. One question may spend several
model calls, each logged under feature ``ask`` in the LLM inspector and the
monthly cost cap. The daily question limit is ``learning.ask_daily_limit``.
Like the briefing, every number in the answer is checked against the data
the model was actually shown; a rejected answer is regenerated once, then
replaced by a plain fallback that names the records used.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import repo
from app.agent import budget
from app.agent.broker.paper import PaperBroker
from app.agent.openai_client import OpenAIResponsesClient
from app.agent.prompts import load_prompt
from app.config import get_secret
from app.db import AskConversationRow, DecisionRow, OutcomeRow, utcnow
from app.features import glossary as glossary_mod
from app.features.briefings import (
    _call_row,
    _record_result,
    text_numbers_ok,
)
from app.features.insights import compute_insights
from app.features.readiness import compute_readiness
from app.models import (
    AskCitation,
    AskConversationOut,
    AskConversationSummary,
    AskMessageOut,
    CheckOut,
    Settings,
)

log = logging.getLogger(__name__)

ASK_PROMPT = "ask.v1"
ASK_FEATURE = "ask"
MAX_TOOL_ROUNDS = 3  # one question may chain a few read-only calls
MAX_MESSAGES_SENT = 20  # conversation turns replayed to the model


class AskRefusal(Exception):
    """The question is refused before any model call (limit or cap)."""


# ------------------------------------------------------------------- tools --


def _dec_brief(row: DecisionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": repo.aware_utc(row.at).isoformat(),
        "pair": row.pair,
        "decider": row.decider,
        "model": row.model,
        "action": row.action,
        "status": row.status,
        "confidence": row.confidence,
        "reasoning": row.reasoning,
        "error": row.error,
        "gates": json.loads(row.gates or "[]"),
    }


def _tool_decisions(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    pair = args.get("pair")
    rows = repo.list_decision_rows(
        session, account_id=account_id, pair=pair, limit=int(args.get("limit", 10))
    )
    data = [_dec_brief(r) for r in rows]
    cites = [
        AskCitation(
            kind="decision",
            id=r.id,
            label=f"{r.pair} {r.action} {repo.aware_utc(r.at):%Y-%m-%d %H:%M}",
            href=f"/?decision={r.id}",
        )
        for r in rows[:8]
    ]
    return data, cites


def _tool_trades(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    rows = repo.list_trades(session, account_id, limit=int(args.get("limit", 10)))
    data = [
        {
            "id": r.id,
            "pair": r.pair,
            "opened_at": repo.aware_utc(r.opened_at).isoformat(),
            "closed_at": repo.aware_utc(r.closed_at).isoformat() if r.closed_at else None,
            "entry": r.entry,
            "exit": r.exit,
            "exit_reason": r.exit_reason,
            "realised_eur": r.realised_eur,
            "realised_r": r.realised_r,
            "decision_id": r.decision_id,
        }
        for r in rows
    ]
    cites = [
        AskCitation(
            kind="trade",
            id=str(r.id),
            label=f"{r.pair} {r.exit_reason or 'open'}",
            href=f"/performance?trade={r.id}",
        )
        for r in rows[:8]
    ]
    return data, cites


def _tool_outcomes(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    """The outcome scorecard per decider/model/prompt (same maths as the
    inspector's scorecard endpoint)."""
    rows = session.execute(
        select(DecisionRow, OutcomeRow)
        .join(OutcomeRow, OutcomeRow.decision_id == DecisionRow.id)
        .where(OutcomeRow.horizon == 72)
    ).all()
    grouped: dict[tuple[str, str | None, str | None], list[Any]] = {}
    for decision, outcome in rows:
        grouped.setdefault((decision.decider, decision.model, decision.prompt_version), []).append(
            (decision, outcome)
        )
    data = []
    for (decider, model, prompt), values in grouped.items():
        buys = [o for d, o in values if d.action == "BUY"]
        wins = sum(o.label == "win" for o in buys)
        realised = [Decimal(o.realised_r) for _, o in values if o.realised_r is not None]
        data.append(
            {
                "decider": decider,
                "model": model,
                "prompt_version": prompt,
                "decisions": len(values),
                "buy_win_rate_pct": round(wins / len(buys) * 100, 1) if buys else None,
                "average_r": float(sum(realised) / len(realised)) if realised else None,
                "missed": sum(o.label == "missed" for _, o in values),
                "avoided": sum(o.label == "avoided" for _, o in values),
            }
        )
    return data[: int(args.get("limit", 20))], []


def _tool_llm_call(
    session: Session, args: dict[str, Any], _account_id: int
) -> tuple[Any, list[AskCitation]]:
    call_id = str(args.get("id") or "")
    row = repo.get_llm_call(session, call_id)
    if row is None:
        return {"error": "no such call"}, []
    data = {
        "id": row.id,
        "at": repo.aware_utc(row.at).isoformat(),
        "pair": row.pair,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "status": row.status,
        "feature": row.feature,
        "input_checks": json.loads(row.input_checks or "[]"),
        "output_checks": json.loads(row.output_checks or "[]"),
        "verdict": row.verdict,
        "request": row.request_json,
        "response": row.response_raw,
        "cost_eur": row.cost_eur,
    }
    return data, [
        AskCitation(
            kind="call",
            id=row.id,
            label=f"call {row.id} ({row.model})",
            href=f"/inspector?call={row.id}",
        )
    ]


def _tool_performance(
    session: Session, args: dict[str, Any], account_id: int, broker: PaperBroker | None
) -> tuple[Any, list[AskCitation]]:
    trades = repo.list_trades(session, account_id, limit=500)
    closed = [t for t in trades if t.closed_at is not None]
    wins = [t for t in closed if t.realised_r and Decimal(t.realised_r) > 0]
    data = {
        "equity": str(broker.equity()) if broker else None,
        "positions": [
            {
                "pair": p,
                "entry": str(pos.entry),
                "stop": str(pos.stop),
                "target": str(pos.target),
                "unrealised": str(pos.unrealised),
            }
            for p, pos in sorted(broker.positions.items())
        ]
        if broker
        else [],
        "trades_closed": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "total_fees_eur": str(sum(Decimal(t.fee_eur or "0") for t in closed)),
    }
    return data, []


def _tool_activity(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    rows = repo.list_activity(session, kind=args.get("kind"), pair=args.get("pair"), limit=20)
    data = [
        {"at": repo.aware_utc(r.at).isoformat(), "kind": r.kind, "pair": r.pair, "text": r.text}
        for r in rows
    ]
    return data, []


def _tool_settings(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    doc = repo.load_settings(session).model_dump(mode="json")
    doc.pop("notifications", None)  # SMTP recipients stay out of the model's view
    return doc, []


def _tool_snapshot(
    session: Session, args: dict[str, Any], account_id: int, broker: PaperBroker | None
) -> tuple[Any, list[AskCitation]]:
    settings = repo.load_settings(session)
    pauses = repo.list_entry_pauses(session, utcnow())
    data = {
        "positions": list(broker.positions) if broker else [],
        "equity": str(broker.equity()) if broker else None,
        "cash": str(broker.cash) if broker else None,
        "open_risk_eur": str(broker.open_risk()) if broker else None,
        "drawdown_pct": str(broker.drawdown_pct()) if broker else None,
        "today_loss_pct": str(broker.today_loss_pct(utcnow())) if broker else None,
        "daily_loss_limit_pct": str(settings.risk.daily_loss_limit_pct),
        "paused": [
            {"pair": p.pair, "until": repo.aware_utc(p.until).isoformat() if p.until else None}
            for p in pauses
        ],
        "execution_mode": settings.execution_mode,
    }
    return data, []


def _tool_indicators_at(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    pair = str(args.get("pair") or "BTC-EUR")
    at_raw = args.get("at")
    at = datetime.fromisoformat(at_raw) if at_raw else utcnow()
    from app.agent.brief import TF_KEYS
    from app.agent.market.base import drop_forming
    from app.agent.signals import compute_pair_signals

    settings = repo.load_settings(session)
    candles = {
        tf: drop_forming(repo.load_candles(session, pair, tf, 900, end=at), tf, at)
        for tf in TF_KEYS
    }
    if not candles["1h"]:
        return {"error": f"no stored candles for {pair} at {at.isoformat()}"}, []
    try:
        signals = compute_pair_signals(
            pair,
            candles["1h"],
            candles["4h"],
            candles["1d"],
            settings=settings.market,
            now=at,
        )
    except Exception:
        return {"error": "signals could not be computed at that time"}, []
    return signals.model_dump(mode="json"), []


def _tool_insights(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    settings = repo.load_settings(session)
    out = compute_insights(
        session,
        now=utcnow(),
        settings=settings,
        dimension=args.get("dimension") or "pair",
        decider="all",
    )
    return out.model_dump(mode="json"), []


def _tool_readiness(
    session: Session, args: dict[str, Any], account_id: int, broker: PaperBroker | None
) -> tuple[Any, list[AskCitation]]:
    settings = repo.load_settings(session)
    account = repo.current_account(session)
    equity = broker.equity() if broker else Decimal(account.cash)
    out = compute_readiness(
        session, now=utcnow(), account=account, equity=equity, settings=settings
    )
    return out.model_dump(mode="json"), []


def _tool_glossary(
    session: Session, args: dict[str, Any], account_id: int
) -> tuple[Any, list[AskCitation]]:
    terms = glossary_mod.load_glossary()
    term = (args.get("term") or "").lower().strip()
    if term:
        hits = {k: v for k, v in terms.items() if term in k.lower()}
        return hits or {"error": f"no glossary entry for {term!r}"}, []
    return terms, []


# The only tools the model may call — every one read-only by construction.
_TOOL_FNS: dict[str, Callable[..., tuple[Any, list[AskCitation]]]] = {
    "get_decisions": _tool_decisions,
    "get_trades": _tool_trades,
    "get_outcomes": _tool_outcomes,
    "get_llm_call": _tool_llm_call,
    "get_performance": _tool_performance,
    "get_activity": _tool_activity,
    "get_settings": _tool_settings,
    "get_snapshot": _tool_snapshot,
    "get_indicators_at": _tool_indicators_at,
    "get_insights": _tool_insights,
    "get_readiness": _tool_readiness,
    "get_glossary": _tool_glossary,
}

TOOL_SPECS = [
    {
        "type": "function",
        "name": name,
        "description": desc,
        "parameters": params,
    }
    for name, desc, params in [
        (
            "get_decisions",
            "Recent decisions, newest first. Args: pair (optional), limit (default 10).",
            {
                "type": "object",
                "properties": {"pair": {"type": "string"}, "limit": {"type": "integer"}},
            },
        ),
        (
            "get_trades",
            "Recent closed/open trades. Args: limit (default 10).",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        (
            "get_outcomes",
            "The outcome scorecard per decider/model/prompt (win rate, avg R, missed vs avoided).",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        (
            "get_llm_call",
            "One stored model call by id: request, response, checks, verdict.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        (
            "get_performance",
            "Equity, open positions, win rate and total fees.",
            {"type": "object", "properties": {}},
        ),
        (
            "get_activity",
            "Recent activity log lines. Args: kind, pair, limit.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "pair": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        ),
        (
            "get_settings",
            "The current settings document (no secrets).",
            {"type": "object", "properties": {}},
        ),
        (
            "get_snapshot",
            "Risk budget, equity, cash, pauses and execution mode right now.",
            {"type": "object", "properties": {}},
        ),
        (
            "get_indicators_at",
            "Signals and regime for a pair at a UTC time (default: now). Args: pair, at (ISO).",
            {
                "type": "object",
                "properties": {"pair": {"type": "string"}, "at": {"type": "string"}},
            },
        ),
        (
            "get_insights",
            "Evidence slices of outcomes by dimension. Args: dimension (pair, hour, "
            "weekday, regime, volatility, confidence, decider or prompt_version).",
            {"type": "object", "properties": {"dimension": {"type": "string"}}},
        ),
        (
            "get_readiness",
            "The live-readiness checklist with current values.",
            {"type": "object", "properties": {}},
        ),
        (
            "get_glossary",
            "Plain-language definitions. Args: term (optional — empty returns all).",
            {"type": "object", "properties": {"term": {"type": "string"}}},
        ),
    ]
]


def run_tool(
    session: Session, name: str, args: dict[str, Any], account_id: int, broker: PaperBroker | None
) -> tuple[Any, list[AskCitation]]:
    fn = _TOOL_FNS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}, []
    try:
        if name in ("get_performance", "get_snapshot", "get_readiness"):
            return fn(session, args, account_id, broker)
        return fn(session, args, account_id)
    except Exception as exc:  # a broken tool never 500s the question
        log.exception("ask tool %s failed", name)
        return {"error": f"tool error: {exc}"}, []


# -------------------------------------------------------------- answering --


def _payload(
    prompt: Any,
    model: str,
    question: str,
    context: dict[str, Any] | None,
    history: list[dict[str, Any]],
    tool_outputs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    input_items: list[Any] = []
    for msg in history[-MAX_MESSAGES_SENT:]:
        input_items.append({"role": msg["role"], "content": msg["text"]})
    q = question if not context else f"{question}\n\nContext: {json.dumps(context)}"
    input_items.append({"role": "user", "content": q})
    if tool_outputs is not None:
        input_items.extend(tool_outputs)
    return {
        "model": model,
        "instructions": prompt.body,
        "input": input_items,
        "tools": TOOL_SPECS,
        "tool_choice": "auto",
        "temperature": prompt.temperature,
        "max_output_tokens": prompt.max_output_tokens,
    }


def _function_calls(response_raw: bytes | None) -> list[dict[str, Any]]:
    """Extract function_call items from the stored response body."""
    if not response_raw:
        return []
    try:
        body = json.loads(response_raw)
    except json.JSONDecodeError:
        return []
    return [item for item in body.get("output", []) if item.get("type") == "function_call"]


async def answer_question(
    session: Session,
    *,
    now: datetime,
    account_id: int,
    broker: PaperBroker | None,
    settings: Settings,
    question: str,
    conversation: AskConversationRow | None,
    context: dict[str, Any] | None,
    key_fn: Callable[[], str | None] = lambda: get_secret("openai"),
    client_factory: Callable[[str], OpenAIResponsesClient] = OpenAIResponsesClient,
    prices: budget.ModelPrices | None = None,
) -> tuple[AskMessageOut, AskConversationRow, list[str]]:
    """Answer one question. Returns (assistant message, conversation row,
    llm_call ids). Raises AskRefusal for the daily limit or the cost cap."""
    asked = repo.ask_questions_today(session, now)
    limit = settings.learning.ask_daily_limit
    if asked >= limit:
        raise AskRefusal(f"the daily Ask limit is {limit} questions")
    spent = budget.month_spend_strict(session, now)
    if budget.cap_state(spent, settings.model.monthly_cost_cap_eur) != "ok":
        raise AskRefusal("the monthly model-cost cap has Ask paused")

    if conversation is None:
        conversation = AskConversationRow(
            id=uuid.uuid4().hex[:12],
            title=question[:72],
            created_at=now.replace(tzinfo=None),
            updated_at=now.replace(tzinfo=None),
            messages="[]",
        )
        session.add(conversation)
        session.flush()
    messages: list[dict[str, Any]] = json.loads(conversation.messages or "[]")
    user_msg = {
        "role": "user",
        "text": question,
        "at": now.isoformat(),
        "citations": [],
        "call_ids": [],
        "records_used": 0,
        "model": None,
        "cost_eur": None,
        "source": "model",
    }
    messages.append(user_msg)

    citations: list[AskCitation] = []
    call_ids: list[str] = []
    cost_eur = Decimal("0")
    reason: str | None = None
    text: str | None = None
    tool_fact_pool: dict[str, Any] = {}
    prices = prices or budget.ModelPrices()

    key = key_fn()
    if key is None:
        reason = "no OpenAI key"
    else:
        try:
            prompt = load_prompt(ASK_PROMPT)
        except Exception:
            prompt = None
            reason = "ask prompt missing"
        if prompt is not None:
            client = client_factory(key)
            tool_outputs: list[dict[str, Any]] | None = None
            try:
                for _round in range(MAX_TOOL_ROUNDS):
                    payload = _payload(
                        prompt,
                        settings.model.model_id,
                        question,
                        context,
                        messages[:-1],
                        tool_outputs,
                    )
                    call_row = _call_row(
                        payload,
                        now=now,
                        account_id=account_id,
                        cycle=None,
                        prompt_version=prompt.version,
                        in_checks=[CheckOut(name="Question received", status="pass", detail="")],
                    )
                    call_row.feature = ASK_FEATURE
                    session.commit()
                    result = await client.call(payload)
                    _record_result(call_row, result, prices, budget.usdc_eur_rate(session, now))
                    call_ids.append(call_row.id)
                    if call_row.cost_eur:
                        cost_eur += Decimal(call_row.cost_eur)
                    session.add(call_row)
                    session.flush()
                    if result.status != "ok":
                        reason = "model call failed"
                        break
                    fcalls = _function_calls(result.response_bytes)
                    if fcalls:
                        # The Responses API is stateless: the next round's
                        # input must carry the function_call items themselves
                        # before their outputs, or the call 400s.
                        tool_outputs = list(fcalls)
                        for fc in fcalls:
                            try:
                                args = json.loads(fc.get("arguments") or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            data, cites = run_tool(
                                session, fc.get("name", ""), args, account_id, broker
                            )
                            citations.extend(cites)
                            tool_fact_pool[fc.get("name", "?")] = data
                            tool_outputs.append(
                                {
                                    "type": "function_call_output",
                                    "call_id": fc.get("call_id"),
                                    "output": json.dumps(data, default=str)[:20000],
                                }
                            )
                        continue
                    candidate = (result.output_text or "").strip()
                    fact_pool = {"context": context or {}, "tools": tool_fact_pool}
                    bad = text_numbers_ok(candidate, fact_pool) if candidate else ["empty"]
                    call_row.output_checks = json.dumps(
                        [
                            CheckOut(
                                name="Numbers match data",
                                status="pass" if not bad else "fail",
                                detail=""
                                if not bad
                                else f"not in data: {', '.join(str(b) for b in bad[:6])}",
                            ).model_dump()
                            for _ in [0]
                        ]
                    )
                    if candidate and not bad:
                        text = candidate
                        break
                    if candidate:
                        # One regen: feed the rejected numbers back.
                        tool_outputs = None
                        rewrite_q = (
                            f"{question}\n\nRewrite using only numbers from the data. "
                            f"These were wrong: {', '.join(str(b) for b in bad[:6])}."
                        )
                        if tool_fact_pool:
                            rewrite_q += (
                                "\n\nData:\n" + json.dumps(tool_fact_pool, default=str)[:8000]
                            )
                        payload = _payload(
                            prompt,
                            settings.model.model_id,
                            rewrite_q,
                            context,
                            messages[:-1],
                            None,
                        )
                        call_row = _call_row(
                            payload,
                            now=now,
                            account_id=account_id,
                            cycle=None,
                            prompt_version=prompt.version,
                            in_checks=[
                                CheckOut(name="Question received", status="pass", detail="")
                            ],
                        )
                        call_row.feature = ASK_FEATURE
                        session.commit()
                        result = await client.call(payload)
                        _record_result(call_row, result, prices, budget.usdc_eur_rate(session, now))
                        call_ids.append(call_row.id)
                        if call_row.cost_eur:
                            cost_eur += Decimal(call_row.cost_eur)
                        session.add(call_row)
                        session.flush()
                        if result.status == "ok" and result.output_text:
                            cand2 = result.output_text.strip()
                            bad2 = text_numbers_ok(cand2, fact_pool)
                            call_row.output_checks = json.dumps(
                                [
                                    CheckOut(
                                        name="Numbers match data",
                                        status="pass" if not bad2 else "fail",
                                        detail=""
                                        if not bad2
                                        else f"not in data: {', '.join(str(b) for b in bad2[:6])}",
                                    ).model_dump()
                                ]
                            )
                            if not bad2:
                                text = cand2
                    break
            finally:
                await client.aclose()
        if text is None and reason is None:
            reason = "the number check rejected the answer"

    if text is None:
        text = (
            "I could not answer that from the stored records right now"
            + (f" ({reason})." if reason else ".")
            + (" The relevant records are linked below." if citations else "")
        )
    # Dedupe citations, cap at 8.
    seen: set[str] = set()
    deduped: list[AskCitation] = []
    for c in citations:
        if c.id in seen:
            continue
        seen.add(c.id)
        deduped.append(c)
    assistant = AskMessageOut(
        role="assistant",
        text=text,
        at=now,
        citations=deduped[:8],
        call_ids=call_ids,
        records_used=len(deduped),
        model=settings.model.model_id,
        cost_eur=cost_eur,
        source="model" if reason is None else "template",
    )
    messages.append(assistant.model_dump(mode="json"))
    conversation.messages = json.dumps(messages, separators=(",", ":"), ensure_ascii=False)
    conversation.updated_at = now.replace(tzinfo=None)
    session.flush()
    return assistant, conversation, call_ids


def conversation_out(row: AskConversationRow) -> AskConversationOut:
    msgs = [AskMessageOut.model_validate(m) for m in json.loads(row.messages or "[]")]
    return AskConversationOut(
        id=row.id,
        title=row.title,
        created_at=repo.aware_utc(row.created_at),
        messages=msgs,
    )


def conversation_summary(row: AskConversationRow) -> AskConversationSummary:
    msgs = json.loads(row.messages or "[]")
    return AskConversationSummary(
        id=row.id,
        title=row.title,
        updated_at=repo.aware_utc(row.updated_at),
        turns=sum(1 for m in msgs if m.get("role") == "user"),
    )
