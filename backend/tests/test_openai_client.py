"""The Responses client: one retry on transient failures only, byte-exact
capture of the request and response, per-attempt records."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import openai
import pytest

from app.agent.openai_client import (
    MAX_ATTEMPTS,
    OpenAIResponsesClient,
    response_output_text,
)


class _FakeResponses:
    """Scripted ``responses.create``: each entry is a return value or an
    exception to raise, consumed in order."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
        self.calls.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeSDK:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = _FakeResponses(outcomes)


class _FakeResult:
    """The parsed Responses object the SDK would return."""

    def __init__(self, text: str | None = '{"action":"HOLD"}') -> None:
        self.output_text = text
        self.id = "resp_fake"
        self.usage = type("Usage", (), {"input_tokens": 11, "output_tokens": 7})()
        self.output: list[Any] = []


def _client(outcomes: list[Any]) -> tuple[OpenAIResponsesClient, _FakeResponses]:
    client = OpenAIResponsesClient("sk-test")
    fake = _FakeSDK(outcomes)
    client._sdk = fake  # type: ignore[assignment]
    return client, fake.responses


def _status_error(code: int) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


def _conn_error() -> openai.APIConnectionError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APIConnectionError(request=request)


def _timeout() -> openai.APITimeoutError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APITimeoutError(request)


# ------------------------------------------------------------------- retry --


async def test_success_returns_output_and_tokens() -> None:
    client, _ = _client([_FakeResult()])
    result = await client.call({"model": "gpt-4o-mini", "input": "{}"})
    assert result.status == "ok"
    assert result.output_text == '{"action":"HOLD"}'
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.request_id == "resp_fake"
    assert len(result.attempts) == 1
    assert result.attempts[0].outcome == "sent"


async def test_timeout_retries_once_then_succeeds() -> None:
    client, responses = _client([_timeout(), _FakeResult()])
    result = await client.call({"model": "m"})
    assert result.status == "ok"
    assert len(responses.calls) == 2
    assert [a.outcome for a in result.attempts] == ["retried", "sent"]
    assert result.attempts[0].error and "timeout" in result.attempts[0].error


async def test_connection_error_retries() -> None:
    client, responses = _client([_conn_error(), _FakeResult()])
    result = await client.call({"model": "m"})
    assert result.status == "ok"
    assert len(responses.calls) == 2


async def test_429_retries_once() -> None:
    client, responses = _client([_status_error(429), _FakeResult()])
    result = await client.call({"model": "m"})
    assert result.status == "ok"
    assert len(responses.calls) == 2
    assert result.attempts[0].http_status == 429


async def test_5xx_retries_then_reports_api_error() -> None:
    client, responses = _client([_status_error(500), _status_error(502)])
    result = await client.call({"model": "m"})
    assert result.status == "api_error"
    assert len(responses.calls) == MAX_ATTEMPTS
    assert [a.outcome for a in result.attempts] == ["retried", "failed"]
    assert result.error and "HTTP 502" in result.error


async def test_4xx_never_retries() -> None:
    client, responses = _client([_status_error(400), _FakeResult()])
    result = await client.call({"model": "m"})
    assert result.status == "api_error"
    assert len(responses.calls) == 1
    assert result.attempts[0].outcome == "failed"
    assert result.attempts[0].http_status == 400


async def test_double_timeout_reports_timeout() -> None:
    client, responses = _client([_timeout(), _timeout()])
    result = await client.call({"model": "m"})
    assert result.status == "timeout"
    assert len(responses.calls) == MAX_ATTEMPTS


# -------------------------------------------------------------- byte exact --


async def test_request_and_response_captured_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored bytes are exactly what went over the wire."""
    body = (
        b'{"id":"resp_1","status":"completed","output":[{"type":"message",'
        b'"content":[{"type":"output_text","text":"{\\"action\\":\\"HOLD\\"}"}]}],'
        b'"usage":{"input_tokens":3,"output_tokens":2}}'
    )
    seen: list[bytes] = []

    async def fake_handle(
        self: httpx2.AsyncHTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        seen.append(await request.aread())
        return httpx2.Response(
            200,
            content=body,
            headers={"x-request-id": "req-9"},
            request=request,
        )

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", fake_handle)
    client = OpenAIResponsesClient("sk-test")
    try:
        result = await client.call({"model": "gpt-4o-mini", "input": "{}", "temperature": 0})
    finally:
        await client.aclose()
    assert result.status == "ok"
    assert result.request_bytes == seen[0]
    sent = json.loads(result.request_bytes)
    assert sent["model"] == "gpt-4o-mini"
    assert sent["temperature"] == 0
    assert result.response_bytes == body
    assert result.request_id == "req-9"
    assert result.output_text == '{"action":"HOLD"}'
    assert result.input_tokens == 3


# --------------------------------------------------------------- raw parse --


def test_response_output_text_reads_stored_body() -> None:
    body = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"action":"BUY"}'}],
                }
            ]
        }
    ).encode()
    assert response_output_text(body) == '{"action":"BUY"}'


def test_response_output_text_handles_garbage() -> None:
    assert response_output_text(None) is None
    assert response_output_text(b"not json") is None
    assert response_output_text(b"{}") is None
