"""The raw OpenAI Responses call.

The official ``openai`` SDK builds and sends the request; a thin transport
wrapper records the exact request and response bytes so the audit store
matches what went over the wire (tests verify this byte-for-byte against
the mocked transport). SDK retries are disabled — the spec's retry policy
is implemented here so every attempt lands in the call record: one retry
on network errors, timeouts, HTTP 429 and 5xx, never on anything else.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
import openai

from app.db import utcnow

TIMEOUT_S = 30.0  # spec: 30 s timeout on the model call
MAX_ATTEMPTS = 2  # one try plus exactly one retry


class _CaptureTransport(httpx.AsyncHTTPTransport):
    """Records the exact bytes of the last request/response pair."""

    def __init__(self) -> None:
        super().__init__()
        self.last_request: bytes | None = None
        self.last_response: bytes | None = None
        self.last_status: int | None = None
        self.last_headers: httpx.Headers | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = await request.aread()
        response = await super().handle_async_request(request)
        self.last_response = await response.aread()
        self.last_status = response.status_code
        self.last_headers = response.headers
        return response


@dataclass
class CallAttempt:
    at: datetime
    outcome: Literal["sent", "retried", "failed"]
    error: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None


@dataclass
class CallResult:
    """Everything a call produced, good or bad."""

    status: Literal["ok", "timeout", "api_error"]
    request_bytes: bytes
    response_bytes: bytes | None = None
    output_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None
    latency_ms: int = 0
    attempts: list[CallAttempt] = field(default_factory=list)
    error: str | None = None


class OpenAIResponsesClient:
    """One-shot caller for the Responses API with byte-exact capture."""

    def __init__(self, api_key: str, *, timeout_s: float = TIMEOUT_S) -> None:
        self._transport = _CaptureTransport()
        self._sdk = openai.AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_s,
            max_retries=0,  # the spec's retry policy is ours to record
            http_client=httpx.AsyncClient(transport=self._transport),
        )

    async def aclose(self) -> None:
        await self._sdk.close()

    async def call(self, payload: dict[str, Any]) -> CallResult:
        """Send one Responses request; retry once on transient failures."""
        attempts: list[CallAttempt] = []
        result = CallResult(status="api_error", request_bytes=b"")
        for attempt_no in range(MAX_ATTEMPTS):
            t0 = time.monotonic()
            try:
                response = await self._sdk.responses.create(**payload)
            except openai.APITimeoutError as exc:
                self._on_error(result, attempts, attempt_no, t0, "timeout", f"timeout: {exc}")
                continue
            except openai.APIConnectionError as exc:
                self._on_error(result, attempts, attempt_no, t0, "api_error", f"network: {exc}")
                continue
            except openai.APIStatusError as exc:
                status = exc.status_code
                retryable = status == 429 or status >= 500
                msg = exc.message if isinstance(exc.message, str) else str(exc)
                self._on_error(
                    result,
                    attempts,
                    attempt_no,
                    t0,
                    "api_error",
                    f"HTTP {status}: {msg}",
                    http_status=status,
                    retryable=retryable,
                )
                if not retryable:
                    break
                continue
            latency = int((time.monotonic() - t0) * 1000)
            headers = self._transport.last_headers
            attempts.append(
                CallAttempt(
                    at=utcnow(),
                    outcome="sent",
                    http_status=self._transport.last_status,
                    latency_ms=latency,
                )
            )
            usage = getattr(response, "usage", None)
            result.status = "ok"
            result.request_bytes = self._transport.last_request or b""
            result.response_bytes = self._transport.last_response
            result.output_text = _output_text(response)
            result.input_tokens = getattr(usage, "input_tokens", None)
            result.output_tokens = getattr(usage, "output_tokens", None)
            result.request_id = (
                headers.get("x-request-id") if headers is not None else None
            ) or getattr(response, "id", None)
            result.latency_ms = latency
            result.attempts = attempts
            return result
        result.latency_ms = sum(a.latency_ms or 0 for a in attempts)
        result.attempts = attempts
        result.request_bytes = self._transport.last_request or result.request_bytes
        result.response_bytes = self._transport.last_response or result.response_bytes
        if self._transport.last_headers is not None:
            result.request_id = self._transport.last_headers.get("x-request-id")
        return result

    def _on_error(
        self,
        result: CallResult,
        attempts: list[CallAttempt],
        attempt_no: int,
        t0: float,
        status: Literal["timeout", "api_error"],
        error: str,
        *,
        http_status: int | None = None,
        retryable: bool = True,
    ) -> None:
        latency = int((time.monotonic() - t0) * 1000)
        retried = retryable and attempt_no < MAX_ATTEMPTS - 1
        attempts.append(
            CallAttempt(
                at=utcnow(),
                outcome="retried" if retried else "failed",
                error=error,
                http_status=http_status or self._transport.last_status,
                latency_ms=latency,
            )
        )
        result.status = status
        result.error = error


def _output_text(response: Any) -> str | None:
    """The model's text output from a Responses API result."""
    try:
        text = response.output_text
    except Exception:
        text = None
    if text:
        return str(text)
    # Fall back to walking the output items.
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts) or None


def response_output_text(raw: bytes | None) -> str | None:
    """Output text of a stored raw response body — used for replays, which
    re-parse the stored request/response without the SDK object."""
    if raw is None:
        return None
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    parts: list[str] = []
    for item in body.get("output") or []:
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts) or None
