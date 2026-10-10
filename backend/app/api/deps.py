"""Shared API dependencies and the local-security rules.

Local-only rules from the spec:

- The app binds to 127.0.0.1 and every request must carry a Host of
  127.0.0.1 or localhost (blocks DNS rebinding). Enforced by middleware.
- Every write (POST/PUT/DELETE) requires the ``X-Local-Token`` header
  generated at startup; the served UI reads it from ``GET /api/session``.
  Other sites can call the endpoint but cannot read the response (no CORS).
- The WebSocket accepts only connections whose Origin is the app itself,
  because browsers do not apply CORS to WebSockets.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, WebSocket
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import LOCAL_HOSTS
from app.models import RiskBudget, Settings, Snapshot, Usage

TOKEN_HEADER = "x-local-token"


def generate_local_token() -> str:
    return secrets.token_urlsafe(32)


def host_allowed(host_header: str | None) -> bool:
    """Only 127.0.0.1 and localhost may address the app; port is ignored."""
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0].strip("[]").lower()
    return host in LOCAL_HOSTS


def origin_allowed(origin: str | None) -> bool:
    """A WebSocket Origin must be the app itself.

    Browsers always send Origin on a WebSocket handshake, so a missing header
    means a non-browser client (tests, curl) — accepted. A foreign Origin is
    refused.
    """
    if not origin:
        return True
    hostname = urlparse(origin).hostname
    return hostname is not None and hostname.lower() in LOCAL_HOSTS


class LocalOnlyMiddleware:
    """Reject any request whose Host header is not 127.0.0.1 or localhost."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1") or None
            if not host_allowed(host):
                if scope["type"] == "websocket":
                    # Close before accept.
                    await send({"type": "websocket.close", "code": 4403})
                    return
                response = _plain_403()
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _plain_403() -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Forbidden: bad Host header"})

    return app


@dataclass
class AppState:
    """Per-process state assembled by the lifespan."""

    local_token: str
    engine: Engine
    session_factory: sessionmaker[Session]
    settings: Settings
    settings_version: int
    status: Literal["running", "paused", "killed", "error"] = "paused"  # restarts paused
    cycle: int = 0
    account_id: int = 1
    ws_clients: set[WebSocket] = field(default_factory=set)


def get_state(request: Request) -> AppState:
    return request.app.state.app_state  # type: ignore[no-any-return]


def get_session(request: Request) -> Session:
    return get_state(request).session_factory()


def require_token(request: Request, x_local_token: str | None = Header(default=None)) -> None:
    state = get_state(request)
    if x_local_token is None or not secrets.compare_digest(x_local_token, state.local_token):
        raise HTTPException(status_code=403, detail="missing or invalid X-Local-Token")


def build_snapshot(state: AppState) -> Snapshot:
    """The full snapshot pushed over the WebSocket after every change.

    Until the engine lands (PR 3) this reports the paused agent with the
    configured paper account.
    """
    settings = state.settings
    start = settings.paper.start_balance_eur
    risk = settings.risk
    return Snapshot(
        status=state.status,
        mode="paper",
        source=settings.data_source,
        cycle=state.cycle,
        next_run_at=None,
        cycle_in_progress=False,
        equity=start,
        cash=start,
        start_equity=start,
        today_pnl=Decimal("0"),
        exposure_pct=0.0,
        drawdown_pct=0.0,
        prices={},
        positions=[],
        latest={},
        pending=[],
        activity=[],
        usage=Usage(
            calls_today=0,
            daily_budget=settings.model.daily_call_budget,
            cost_today_eur=Decimal("0"),
            cost_month_eur=Decimal("0"),
            cap_eur=settings.model.monthly_cost_cap_eur,
        ),
        account_id=state.account_id,
        quiet="",
        risk_budget=RiskBudget(
            open_risk_pct=Decimal("0"),
            open_risk_cap_pct=risk.total_open_risk_pct,
            today_loss_pct=Decimal("0"),
            daily_limit_pct=risk.daily_loss_limit_pct,
            drawdown_pct=Decimal("0"),
            kill_pct=risk.kill_drawdown_pct,
            full_trades_left=0,
            risk_pct_now=risk.risk_per_trade_pct,
            throttle=None,
            shock_brake_until=None,
            correlated_slots_left=risk.correlated_max_positions,
        ),
        paused=[],
        errors_24h=0,
        errors_open=0,
        kill=None,
    )
