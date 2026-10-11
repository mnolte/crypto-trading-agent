"""The LLM inspector API: call history, detail, downloads, replays,
verdicts, the JSONL export and the costs endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import repo
from app.config import set_secret
from app.db import DecisionRow, LlmCallRow

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _state(client: TestClient) -> Any:
    return cast(Any, client.app).state.app_state


def _session(client: TestClient) -> Session:
    return cast(Session, _state(client).session_factory())


def _token(client: TestClient) -> str:
    return str(client.get("/api/session").json()["token"])


def _call_row(
    client: TestClient,
    *,
    call_id: str = "call-1",
    at: datetime = NOW,
    pair: str = "SOL-EUR",
    feature: str = "decision",
    status: str = "ok",
    parsed: str | None = '{"action":"HOLD","confidence":0.5}',
    output_checks: str = "[]",
    cost_eur: str | None = "0.0100",
    decision_id: str | None = None,
    request_json: str | None = None,
) -> LlmCallRow:
    row = LlmCallRow(
        id=call_id,
        at=at,
        cycle=3,
        account_id=1,
        pair=pair,
        decision_id=decision_id,
        feature=feature,
        replay_of=None,
        model="gpt-4o-mini",
        prompt_version="decision.v1",
        status=status,
        attempts=json.dumps(
            [{"at": at.isoformat(), "outcome": "sent", "latency_ms": 10}]
        ),
        latency_ms=10,
        input_tokens=100,
        output_tokens=50,
        cost_usd="0.0110",
        cost_eur=cost_eur,
        request_id="req-1",
        request_json=request_json
        or json.dumps(
            {
                "model": "gpt-4o-mini",
                "instructions": "SYSTEM PROMPT BODY",
                "input": json.dumps({"pair": pair, "price": 100.0}),
                "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
            }
        ),
        response_raw='{"id":"resp_1"}',
        parsed=parsed,
        input_checks=json.dumps(
            [{"name": "Fresh", "status": "pass", "detail": "ok"}]
        ),
        output_checks=output_checks,
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )
    session = _session(client)
    try:
        repo.add_llm_call(session, row)
        session.commit()
    finally:
        session.close()
    return row


def _decision_row(
    client: TestClient,
    *,
    decision_id: str,
    pair: str = "SOL-EUR",
    prefilter: bool = False,
    at: datetime = NOW,
) -> None:
    session = _session(client)
    try:
        session.add(
            DecisionRow(
                id=decision_id,
                cycle=3,
                at=at,
                pair=pair,
                decider="openai",
                model=None,
                prompt_version=None,
                action="HOLD",
                confidence=0.0,
                reasoning="no setup",
                signals="[]",
                gates="[]",
                status="hold",
                account_id=1,
                regime='{"trend":"up","volatility":"normal","age_4h":1}',
                btc_regime='{"trend":"up","volatility":"normal","age_4h":1}',
                risk_pct_used="1",
                settings_version=1,
                prefilter=prefilter,
            )
        )
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------- list --


def test_list_merges_calls_and_prefilter_skips(client: TestClient) -> None:
    _call_row(client, call_id="c1", at=NOW)
    _decision_row(client, decision_id="d1", prefilter=False, at=NOW - timedelta(minutes=5))
    _decision_row(client, decision_id="d2", prefilter=True, at=NOW - timedelta(minutes=6))
    items = client.get("/api/llm-calls").json()["items"]
    kinds = {i["id"]: i["kind"] for i in items}
    assert kinds["c1"] == "call"
    assert kinds["d1"] == "skipped"
    assert "d2" not in kinds  # prefilter passed rows are not skips
    # Newest first.
    assert items[0]["id"] == "c1"


def test_list_skips_hidden_with_filters(client: TestClient) -> None:
    _call_row(client, call_id="c1", status="api_error")
    _decision_row(client, decision_id="d1", prefilter=False)
    items = client.get("/api/llm-calls?status=api_error").json()["items"]
    assert [i["id"] for i in items] == ["c1"]
    items = client.get("/api/llm-calls?skipped=false").json()["items"]
    assert [i["id"] for i in items] == ["c1"]


def test_list_issues_only_warn_or_fail(client: TestClient) -> None:
    _call_row(
        client,
        call_id="clean",
        output_checks=json.dumps(
            [{"name": "Levels sane", "status": "pass", "detail": ""}]
        ),
    )
    _call_row(
        client,
        call_id="warned",
        output_checks=json.dumps(
            [{"name": "Signal claims", "status": "warn", "detail": "x"}]
        ),
    )
    items = client.get("/api/llm-calls?issues=true").json()["items"]
    assert [i["id"] for i in items] == ["warned"]
    assert items[0]["issues"] == 1


def test_list_pair_filter(client: TestClient) -> None:
    _call_row(client, call_id="sol", pair="SOL-EUR")
    _call_row(client, call_id="eth", pair="ETH-EUR")
    items = client.get("/api/llm-calls?pair=SOL-EUR").json()["items"]
    assert [i["id"] for i in items] == ["sol"]


# ------------------------------------------------------------------- detail --


def test_detail_exposes_request_parts_and_checks(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    detail = client.get("/api/llm-calls/c1").json()
    assert detail["system_prompt"] == "SYSTEM PROMPT BODY"
    assert json.loads(detail["brief"])["pair"] == "SOL-EUR"
    assert detail["output_schema"] is not None
    assert detail["parsed"]["action"] == "HOLD"
    assert detail["attempts"][0]["outcome"] == "sent"
    assert detail["cost_eur"] == "0.0100"


def test_detail_unknown_call_is_404(client: TestClient) -> None:
    assert client.get("/api/llm-calls/nope").status_code == 404


def test_download_sends_a_json_attachment(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    response = client.get("/api/llm-calls/c1/download")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.json()["id"] == "c1"


# ------------------------------------------------------------------ verdict --


def test_verdict_requires_token(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    assert (
        client.put("/api/llm-calls/c1/verdict", json={"verdict": "correct"}).status_code
        == 403
    )


def test_verdict_set_and_clear(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    headers = {"X-Local-Token": _token(client)}
    detail = client.put(
        "/api/llm-calls/c1/verdict",
        json={"verdict": "wrong", "note": "chased"},
        headers=headers,
    ).json()
    assert detail["verdict"] == "wrong"
    assert detail["verdict_note"] == "chased"
    assert detail["verdict_at"] is not None
    detail = client.put(
        "/api/llm-calls/c1/verdict", json={"verdict": None}, headers=headers
    ).json()
    assert detail["verdict"] is None
    assert detail["verdict_note"] is None


# ------------------------------------------------------------------- export --


def test_export_returns_only_verdicted_calls(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    _call_row(client, call_id="c2", at=NOW - timedelta(minutes=1))
    headers = {"X-Local-Token": _token(client)}
    client.put(
        "/api/llm-calls/c1/verdict", json={"verdict": "correct"}, headers=headers
    )
    response = client.get("/api/llm-calls/export.jsonl")
    assert response.status_code == 200
    lines = [json.loads(t) for t in response.text.strip().splitlines()]
    assert [t["id"] for t in lines] == ["c1"]
    assert lines[0]["verdict"] == "correct"
    assert lines[0]["brief"]["pair"] == "SOL-EUR"


# --------------------------------------------------------------------- cost --


def test_costs_reports_month_spend(client: TestClient) -> None:
    _call_row(client, call_id="c1", cost_eur="0.0100")
    _call_row(client, call_id="c2", cost_eur="0.0200", feature="replay")
    costs = client.get("/api/costs").json()
    assert costs["cap_eur"] == "10"
    assert float(costs["spent_eur"]) == pytest.approx(0.03)
    assert float(costs["by_feature"]["decision"]) == pytest.approx(0.01)
    assert float(costs["by_feature"]["replay"]) == pytest.approx(0.02)


def test_costs_rejects_a_bad_month(client: TestClient) -> None:
    assert client.get("/api/costs?month=oct").status_code == 400
    costs = client.get("/api/costs?month=2026-10").json()
    assert costs["month"] == "2026-10"


# ------------------------------------------------------------------- replay --


def test_replay_without_key_is_409(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    response = client.post(
        "/api/llm-calls/c1/replay",
        json={},
        headers={"X-Local-Token": _token(client)},
    )
    assert response.status_code == 409


def test_replay_unknown_call_is_404(client: TestClient) -> None:
    set_secret("openai", "sk-test")
    response = client.post(
        "/api/llm-calls/nope/replay",
        json={},
        headers={"X-Local-Token": _token(client)},
    )
    assert response.status_code == 404


def test_replay_records_a_linked_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent.openai_client import CallResult

    _call_row(client, call_id="c1")
    set_secret("openai", "sk-test")

    class FakeClient:
        def __init__(self, key: str) -> None:
            pass

        async def call(self, payload: dict[str, Any]) -> CallResult:
            return CallResult(
                status="ok",
                request_bytes=b'{"model":"gpt-4o"}',
                response_bytes=b'{"id":"resp_2"}',
                output_text='{"action":"HOLD","confidence":0.9}',
                input_tokens=10,
                output_tokens=5,
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        "app.api.inspector.OpenAIResponsesClient", FakeClient
    )
    response = client.post(
        "/api/llm-calls/c1/replay",
        json={"model": "gpt-4o"},
        headers={"X-Local-Token": _token(client)},
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["feature"] == "replay"
    assert detail["replay_of"] == "c1"
    assert detail["model"] == "gpt-4o"
    # The original now lists the replay.
    orig = client.get("/api/llm-calls/c1").json()
    assert [r["id"] for r in orig["replays"]] == [detail["id"]]
    assert orig["replays"][0]["differs"] is False
