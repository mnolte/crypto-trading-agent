"""Retention maintenance: raw LLM payloads are pruned without losing decisions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import DecisionRow, LlmCallRow, OutcomeRow

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _token(client: TestClient) -> str:
    return cast(str, client.get("/api/session").json()["token"])


def _decision(decision_id: str, at: datetime) -> DecisionRow:
    return DecisionRow(
        id=decision_id,
        cycle=1,
        at=at.replace(tzinfo=None),
        account_id=1,
        pair="BTC-EUR",
        decider="openai",
        model="gpt-4o-mini",
        prompt_version="decision.v1",
        action="HOLD",
        confidence=0.5,
        reasoning="parsed decision survives",
        invalidation=None,
        signals="[]",
        plan=None,
        gates="[]",
        status="done",
        execute_at=None,
        error=None,
        regime="{}",
        btc_regime="{}",
        invalidation_price=None,
        risk_pct_used="1",
        settings_version=1,
        shadow_id=None,
        stand_in=False,
        raw_brief='{"pair":"BTC-EUR"}',
        raw_response='{"action":"HOLD"}',
        prefilter=True,
    )


def _llm_call(call_id: str, decision_id: str, at: datetime) -> LlmCallRow:
    return LlmCallRow(
        id=call_id,
        at=at.replace(tzinfo=None),
        cycle=1,
        account_id=1,
        pair="BTC-EUR",
        decision_id=decision_id,
        feature="decision",
        replay_of=None,
        model="gpt-4o-mini",
        prompt_version="decision.v1",
        status="ok",
        attempts="[]",
        latency_ms=10,
        input_tokens=10,
        output_tokens=5,
        cost_usd="0.001",
        cost_eur="0.0009",
        request_id="req",
        request_json='{"input":"raw brief"}',
        response_raw='{"id":"resp"}',
        parsed='{"action":"HOLD"}',
        input_checks="[]",
        output_checks="[]",
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )


def test_prune_clears_raw_payloads_older_than_90_days_only(sim_client: TestClient) -> None:
    client = sim_client
    now = datetime.now(UTC)
    old = now - timedelta(days=91)
    recent = now - timedelta(days=89)
    state = cast(FastAPI, client.app).state.app_state
    session = state.session_factory()
    try:
        session.add(_decision("old-decision", old))
        session.add(_decision("recent-decision", recent))
        session.add(_llm_call("old-call", "old-decision", old))
        session.add(_llm_call("recent-call", "recent-decision", recent))
        session.add(
            OutcomeRow(
                decision_id="old-decision",
                horizon=24,
                account_id=1,
                pair="BTC-EUR",
                action="HOLD",
                reference_at=old.replace(tzinfo=None),
                reference_price="100",
                forward_return_pct="1",
                best_move_pct="2",
                worst_move_pct="-1",
                best_move_r="2",
                worst_move_r="-1",
                first_touch=None,
                invalidated=False,
                invalidation_followup_pct=None,
                label="avoided",
                realised_r=None,
                computed_at=old.replace(tzinfo=None),
                method_version="v1",
            )
        )
        session.commit()
    finally:
        session.close()

    assert client.post("/api/maintenance/prune").status_code == 403
    res = client.post("/api/maintenance/prune", headers={"X-Local-Token": _token(client)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["decisions_pruned"] == 1
    assert body["llm_calls_pruned"] == 1
    assert "cutoff" in body

    session = state.session_factory()
    try:
        old_decision = session.get(DecisionRow, "old-decision")
        recent_decision = session.get(DecisionRow, "recent-decision")
        old_call = session.get(LlmCallRow, "old-call")
        recent_call = session.get(LlmCallRow, "recent-call")
        assert old_decision is not None and recent_decision is not None
        assert old_call is not None and recent_call is not None
        assert old_decision.raw_brief is None and old_decision.raw_response is None
        assert old_call.request_json is None and old_call.response_raw is None
        assert recent_decision.raw_brief is not None and recent_decision.raw_response is not None
        assert recent_call.request_json is not None and recent_call.response_raw is not None
        assert old_decision.action == "HOLD"
        assert json.loads(old_call.parsed)["action"] == "HOLD"
        outcome = session.scalar(select(OutcomeRow).where(OutcomeRow.decision_id == "old-decision"))
        assert outcome is not None
    finally:
        session.close()
