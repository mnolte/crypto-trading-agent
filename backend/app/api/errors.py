"""The one API error shape and the shutdown guard.

Every non-2xx response is ``{"error": {"code": "<stable code>",
"message": "<one sentence>", "error_id": "e-..."}}``. ``error_id`` is
present on 500s so a failing request can be looked up on the Logs
screen. Codes are stable: 400 ``bad_request``, 401/403
``local_token_missing``/``origin_refused``/``forbidden``, 404
``not_found``, 405 ``method_not_allowed``, 409 ``conflict``, 422
``invalid`` (with ``fields``), 429 ``rate_limited`` (with
``Retry-After``), 500 ``internal``, 503 ``starting_up`` or
``shutting_down``.

While the app is shutting down, writes get 503 ``shutting_down`` and
reads keep working.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("app.api.errors")

_STATUS_CODES = {
    400: "bad_request",
    401: "local_token_missing",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid",
    429: "rate_limited",
    500: "internal",
    503: "starting_up",
}

_DEFAULT_MESSAGES = {
    400: "The request could not be read.",
    401: "The local token is missing.",
    403: "This request is not allowed.",
    404: "That page or record does not exist.",
    405: "That method is not supported here.",
    409: "This conflicts with a request already in progress.",
    422: "The request was not valid.",
    429: "Too many requests — try again in a moment.",
    500: "Something went wrong on our side.",
    503: "The app is not ready yet.",
}


def error_body(
    status: int,
    message: str,
    *,
    code: str | None = None,
    error_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code or _STATUS_CODES.get(status, "error"),
        "message": message,
    }
    if error_id is not None:
        body["error_id"] = error_id
    if extra:
        body.update(extra)
    return {"error": body}


def api_error(status: int, message: str, **kw: Any) -> JSONResponse:
    return JSONResponse(
        error_body(status, message, **kw), status_code=status
    )


def new_error_id() -> str:
    return "e-" + secrets.token_hex(4)


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """HTTPException -> the stable shape; keeps Retry-After on 429."""
    detail = exc.detail if isinstance(exc.detail, str) else None
    message = detail or _DEFAULT_MESSAGES.get(exc.status_code, "Request failed.")
    body = error_body(exc.status_code, message)
    headers = {}
    if exc.status_code == 429 and exc.headers and "Retry-After" in exc.headers:
        headers["Retry-After"] = exc.headers["Retry-After"]
    if exc.status_code >= 500:
        log.warning(
            "api request rejected: %s %s -> %d",
            request.method,
            request.url.path,
            exc.status_code,
            extra={"event": "api.rejected"},
        )
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


async def validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 with the fields that failed."""
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        fields[loc or "body"] = err.get("msg", "invalid")
    return JSONResponse(
        error_body(
            422,
            "The request was not valid.",
            code="invalid",
            extra={"fields": fields},
        ),
        status_code=422,
    )


async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """A 500 gets an error id that matches its ERROR line."""
    error_id = new_error_id()
    log.error(
        "unhandled api error: %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
        extra={
            "event": "api.unhandled",
            "error_id": error_id,
        },
    )
    return JSONResponse(
        error_body(
            500, "Something went wrong on our side.", error_id=error_id
        ),
        status_code=500,
    )


class ShutdownGuard:
    """Writes get 503 ``shutting_down`` once shutdown starts; reads
    (and WebSockets, which close on their own) pass."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") not in ("GET", "HEAD")
            and getattr(scope["app"].state, "shutting_down", False)
        ):
            body = b'{"error":{"code":"shutting_down","message":"The app is shutting down."}}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
