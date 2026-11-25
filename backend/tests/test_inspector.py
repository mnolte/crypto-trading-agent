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
from app.agent import reasons
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
    cost_usd: str | None = "0.0110",
    cost_eur: str | None = "0.0100",
    latency_ms: int = 10,
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
        attempts=json.dumps([{"at": at.isoformat(), "outcome": "sent", "latency_ms": 10}]),
        latency_ms=latency_ms,
        input_tokens=100,
        output_tokens=50,
        cost_usd=cost_usd,
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
        input_checks=json.dumps([{"name": "Fresh", "status": "pass", "detail": "ok"}]),
        output_checks=output_checks,
        verdict=None,
        verdict_note=None,
        verdict_at=None,
    )
    outcome, found = reasons.classify(row, reasons.parsed_action(row), 15)
    row.outcome_status = outcome
    row.reasons = json.dumps(found, separators=(",", ":"))
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
                status="none",
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
    items = client.get("/api/llm-calls?period=all&skipped=true").json()["items"]
    kinds = {i["id"]: i["kind"] for i in items}
    assert kinds["c1"] == "call"
    assert kinds["d1"] == "skipped"
    assert "d2" not in kinds  # prefilter passed rows are not skips
    # Newest first.
    assert items[0]["id"] == "c1"


def test_list_skips_hidden_with_filters(client: TestClient) -> None:
    _call_row(client, call_id="c1", status="api_error")
    _decision_row(client, decision_id="d1", prefilter=False)
    items = client.get("/api/llm-calls?period=all&status=api_error").json()["items"]
    assert [i["id"] for i in items] == ["c1"]
    items = client.get("/api/llm-calls?period=all&skipped=false").json()["items"]
    assert [i["id"] for i in items] == ["c1"]


def test_list_issues_only_warn_or_fail(client: TestClient) -> None:
    _call_row(
        client,
        call_id="clean",
        output_checks=json.dumps([{"name": "Levels sane", "status": "pass", "detail": ""}]),
    )
    _call_row(
        client,
        call_id="warned",
        output_checks=json.dumps([{"name": "Signal claims", "status": "warn", "detail": "x"}]),
    )
    items = client.get("/api/llm-calls?period=all&issues=true").json()["items"]
    assert [i["id"] for i in items] == ["warned"]
    assert items[0]["issues"] == 1


def test_list_pair_filter(client: TestClient) -> None:
    _call_row(client, call_id="sol", pair="SOL-EUR")
    _call_row(client, call_id="eth", pair="ETH-EUR")
    items = client.get("/api/llm-calls?period=all&pair=SOL-EUR").json()["items"]
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
    assert client.put("/api/llm-calls/c1/verdict", json={"verdict": "correct"}).status_code == 403


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
    detail = client.put("/api/llm-calls/c1/verdict", json={"verdict": None}, headers=headers).json()
    assert detail["verdict"] is None
    assert detail["verdict_note"] is None


# ------------------------------------------------------------------- export --


def test_export_returns_only_verdicted_calls(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    _call_row(client, call_id="c2", at=NOW - timedelta(minutes=1))
    headers = {"X-Local-Token": _token(client)}
    client.put("/api/llm-calls/c1/verdict", json={"verdict": "correct"}, headers=headers)
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
    costs = client.get("/api/costs?month=2026-10").json()
    assert costs["cap_eur"] == "10"
    assert float(costs["spent_eur"]) == pytest.approx(0.03)
    assert float(costs["spent_usd"]) == pytest.approx(0.022)
    assert float(costs["by_feature"]["decisions"]) == pytest.approx(0.01)
    assert float(costs["by_feature"]["replays"]) == pytest.approx(0.02)


def test_costs_usd_none_when_a_priced_call_is_unknown(client: TestClient) -> None:
    _call_row(client, call_id="c1", cost_usd=None)
    costs = client.get("/api/costs?month=2026-10").json()
    assert costs["spent_usd"] is None  # a successful call's price was unknown


def test_costs_usd_ignores_failed_and_blocked_calls(client: TestClient) -> None:
    _call_row(client, call_id="c1", cost_usd="0.0110")
    _call_row(client, call_id="c2", status="api_error", cost_usd=None)
    _call_row(client, call_id="c3", status="input_blocked", cost_usd=None)
    costs = client.get("/api/costs?month=2026-10").json()
    assert float(costs["spent_usd"]) == pytest.approx(0.011)


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


def test_replay_records_a_linked_call(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr("app.api.inspector.OpenAIResponsesClient", FakeClient)
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


def test_replay_of_a_call_without_output_schema(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review and briefing calls store no ``text`` field — the replay must
    not write ``"text": null`` and the returned detail must serialize."""
    from app.agent.openai_client import CallResult

    _call_row(
        client,
        call_id="c1",
        feature="review",
        pair="",
        parsed="Some markdown review",
        request_json=json.dumps(
            {
                "model": "gpt-4o-mini",
                "instructions": "REVIEW PROMPT",
                "input": json.dumps({"facts": {"equity_eur": "10000.00"}}),
                "temperature": 0,
                "max_output_tokens": 600,
            }
        ),
    )
    set_secret("openai", "sk-test")

    class FakeClient:
        def __init__(self, key: str) -> None:
            pass

        async def call(self, payload: dict[str, Any]) -> CallResult:
            return CallResult(
                status="ok",
                request_bytes=json.dumps(payload).encode(),
                response_bytes=b'{"id":"resp_2"}',
                output_text="Equity is €10,000.00 this week.",
                input_tokens=10,
                output_tokens=5,
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("app.api.inspector.OpenAIResponsesClient", FakeClient)
    response = client.post(
        "/api/llm-calls/c1/replay",
        json={},
        headers={"X-Local-Token": _token(client)},
    )
    assert response.status_code == 200
    detail = response.json()
    assert "text" not in json.loads(detail["request_json"])
    # The review's own output check re-runs, not the decision schema's.
    assert detail["status"] == "ok"
    assert detail["output_checks"] == [
        {"name": "Numbers match facts", "status": "pass", "detail": ""}
    ]
    assert client.get(f"/api/llm-calls/{detail['id']}").status_code == 200


def test_detail_tolerates_a_null_text_field(client: TestClient) -> None:
    _call_row(
        client,
        call_id="c1",
        request_json=json.dumps(
            {
                "model": "gpt-4o-mini",
                "input": json.dumps({"pair": "SOL-EUR"}),
                "text": None,
            }
        ),
    )
    detail = client.get("/api/llm-calls/c1").json()
    assert detail["output_schema"] is None
    assert detail["brief"] is not None


# ------------------------------------------------------- item 043 additions --


def test_list_keyset_cursor_and_total(client: TestClient) -> None:
    for index in range(3):
        _call_row(client, call_id=f"c{index}", at=NOW - timedelta(minutes=index))
    first = client.get("/api/llm-calls?period=all&limit=2").json()
    assert first["total"] == 3
    assert len(first["items"]) == 2
    second = client.get(f"/api/llm-calls?period=all&limit=2&cursor={first['next_cursor']}").json()
    assert second["total"] == 3
    assert [i["id"] for i in second["items"]] == ["c2"]
    assert second["next_cursor"] is None


def test_list_reason_and_attention_filters(client: TestClient) -> None:
    row = _call_row(
        client,
        call_id="warned",
        output_checks=json.dumps([{"name": "Signal claims", "status": "warn", "detail": "x"}]),
    )
    stored = json.loads(row.reasons or "[]")
    assert stored[0]["code"] == "claim_mismatch"
    items = client.get("/api/llm-calls?period=all&reason=claim_mismatch").json()["items"]
    assert [i["id"] for i in items] == ["warned"]
    assert client.get("/api/llm-calls?period=all&attention=act").json()["items"] == []


def test_summary_counts_cost_latency_buckets_and_options(client: TestClient) -> None:
    _call_row(client, call_id="live", cost_eur="0.0100", latency_ms=1000)
    _call_row(client, call_id="shadow", feature="shadow:rules", cost_eur="0.0200", latency_ms=2000)
    _decision_row(client, decision_id="skip", prefilter=False)
    data = client.get("/api/llm-calls/summary?period=all&source=live,shadow").json()
    assert data["calls"] == 2
    assert data["cycles_without_call"] == 1
    assert data["cost_eur"]["live"] == "0.0100"
    assert data["cost_eur"]["shadow"] == "0.0200"
    assert data["latency_ms"] == {"median": 1500, "p95": 2000}
    assert data["counts"]["success"] == 2
    assert data["option_counts"]["source"]["shadow"] == 1
    assert data["buckets"]


def test_export_csv_has_metadata_only(client: TestClient) -> None:
    _call_row(client, call_id="c1")
    response = client.get("/api/llm-calls/export.csv")
    assert response.status_code == 200
    text = response.text
    assert "prompt_version" in text
    assert "SYSTEM PROMPT BODY" not in text
    assert "request_json" not in text
    assert "response_raw" not in text


def test_detail_explanations_sections_hash_and_neighbours(client: TestClient) -> None:
    _call_row(client, call_id="older", at=NOW - timedelta(minutes=2))
    _call_row(
        client,
        call_id="focus",
        at=NOW - timedelta(minutes=1),
        output_checks=json.dumps([{"name": "Signal claims", "status": "warn", "detail": "x"}]),
    )
    _call_row(client, call_id="newer", at=NOW)
    detail = client.get("/api/llm-calls/focus?period=all").json()
    assert detail["outcome_status"] == "warning"
    assert detail["explained_reasons"][0]["code"] == "claim_mismatch"
    assert detail["explained_reasons"][0]["why_here"]
    assert detail["sections"][0]["title"] == "Call"
    assert detail["newer_id"] == "newer"
    assert detail["older_id"] == "older"
    hidden = client.get("/api/llm-calls/focus?period=all&pair=ETH-EUR").json()
    assert hidden["outside_filters"] is True
