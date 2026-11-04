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

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, WebSocket
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from app import logstore, repo
from app.agent import budget
from app.agent.broker.paper import PaperBroker
from app.agent.market import make_market_client
from app.agent.market.base import MarketClient, MarketInfo
from app.agent.market.bitvavo import BitvavoClient
from app.config import LOCAL_HOSTS
from app.features import quiet as quiet_mod
from app.features.needs import load_needs
from app.features.performance import since_start
from app.models import (
    ActivityItem,
    EntryPause,
    RiskBudget,
    Settings,
    Snapshot,
    Usage,
)
from app.sources import SourceRegistry

if TYPE_CHECKING:
    from app.agent.engine import Engine as AgentEngine

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


def new_request_id() -> str:
    return "r-" + secrets.token_hex(6)


class RequestIdMiddleware:
    """Every request gets an id, stamped into the log context for its
    duration and returned as ``X-Request-Id`` on the response. Pure ASGI,
    so the context variable reaches the endpoint in the same task."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()

        async def send_with_id(message: object) -> None:
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)  # type: ignore[arg-type]

        with logstore.log_context(request_id=request_id):
            await self.app(scope, receive, send_with_id)


@dataclass
class AppState:
    """Per-process state assembled by the lifespan."""

    local_token: str
    engine: Engine
    session_factory: sessionmaker[Session]
    settings: Settings
    settings_version: int
    market_client: MarketClient
    sources: SourceRegistry
    status: Literal["running", "paused", "killed", "error"] = "paused"  # restarts paused
    cycle: int = 0
    account_id: int = 1
    market_rules: dict[str, MarketInfo] = field(default_factory=dict)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    ws_clients: set[WebSocket] = field(default_factory=set)
    broker: PaperBroker | None = None
    agent_engine: AgentEngine | None = None
    shutting_down: bool = False
    log_store: Any = None


def build_market_client(settings: Settings, sources: SourceRegistry) -> MarketClient:
    """The market client for the configured source, wired so every request
    outcome lands in the source registry."""
    client = make_market_client(settings)
    if isinstance(client, BitvavoClient):
        client.on_event = lambda error: sources.record(
            "bitvavo",
            error,
            rate_limit_remaining=client.rate_limit_remaining,
            rate_limit_limit=client.rate_limit_limit,
            rate_limit_reset_at=client.rate_limit_reset_at,
        )
    return client


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

    With the engine running this reflects the live paper account: positions
    at last price, the newest decision per pair, pending approvals and the
    risk meter. The UI never computes trading values itself.
    """
    settings = state.settings
    engine = state.agent_engine
    broker = state.broker
    start = settings.paper.start_balance_eur
    usage = Usage(
        calls_today=0,
        daily_budget=settings.model.daily_call_budget,
        cost_today_eur=Decimal("0"),
        cost_month_eur=Decimal("0"),
        cap_eur=settings.model.monthly_cost_cap_eur,
    )
    if engine is None or broker is None:
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
            usage=usage,
            account_id=state.account_id,
            quiet="",
            risk_budget=RiskBudget(
                open_risk_pct=Decimal("0"),
                open_risk_cap_pct=settings.risk.total_open_risk_pct,
                today_loss_pct=Decimal("0"),
                daily_limit_pct=settings.risk.daily_loss_limit_pct,
                drawdown_pct=Decimal("0"),
                kill_pct=settings.risk.kill_drawdown_pct,
                full_trades_left=0,
                room_limited_by="risk",
                risk_pct_now=settings.risk.risk_per_trade_pct,
                throttle=None,
                shock_brake_until=None,
                correlated_slots_left=settings.risk.correlated_max_positions,
            ),
            paused=[],
            errors_24h=0,
            errors_open=0,
            kill=None,
        )

    session = state.session_factory()
    try:
        now = engine.clock()
        equity = broker.equity()
        cash = broker.cash
        positions = [p.to_model() for p in broker.positions.values()]
        stop_near_atr = settings.ui.stop_near_atr
        for model, held in zip(positions, broker.positions.values(), strict=True):
            model.stop_above_entry = held.stop >= held.entry
            if held.last <= 0:
                continue
            dist = held.last - held.stop
            model.stop_dist_pct = dist / held.last * 100
            atr = engine._atr_1h(session, held.pair, now)
            if atr is not None and atr > 0:
                model.stop_dist_atr = dist / atr
                model.stop_near = model.stop_dist_atr < stop_near_atr
            if held.qty > 0 and held.r_unit > 0:
                model.pnl_r = held.unrealised / (held.qty * held.r_unit)
        activity = [
            ActivityItem(
                at=repo.aware_utc(row.at),
                kind=row.kind,
                pair=row.pair,
                text=row.text,
                decision_id=row.decision_id,
            )
            for row in repo.list_activity(session, since=now - timedelta(hours=24), limit=2000)
        ]
        today = repo.trades_today(session, broker.account_id)
        usage = Usage(
            calls_today=budget.calls_today(session, broker.account_id, now),
            daily_budget=settings.model.daily_call_budget,
            cost_today_eur=repo.llm_cost_between(
                session, broker.account_id, budget.amsterdam_day_start(now)
            ),
            cost_month_eur=budget.month_spend_all(session, now),
            cap_eur=settings.model.monthly_cost_cap_eur,
        )
        errors_24h = repo.error_count_since(session, now - timedelta(hours=24))
        pauses = repo.list_entry_pauses(session, now)
        _agent_pct, btc_pct = since_start(session, broker.account, equity, now)
        return Snapshot(
            status=engine.status,
            mode="paper",
            source=settings.data_source,
            cycle=engine.cycle,
            next_run_at=engine.next_run_at,
            cycle_in_progress=engine.cycle_in_progress,
            equity=equity,
            cash=cash,
            start_equity=Decimal(broker.account.start_balance),
            today_pnl=broker.today_pnl(now),
            exposure_pct=float(broker.exposure() / equity * 100) if equity > 0 else 0.0,
            drawdown_pct=float(broker.drawdown_pct()),
            prices=engine.prices,
            positions=positions,
            latest=engine.latest,
            pending=list(engine.pending.values()),
            activity=activity,
            usage=usage,
            account_id=broker.account_id,
            quiet=(
                quiet_mod.quiet_line(
                    session,
                    broker.account_id,
                    now=now,
                    shock_brake_until=engine.shock_brake_until,
                )
                if settings.learning.quiet_line
                else ""
            ),
            risk_budget=engine.risk_budget(),
            paused=[
                EntryPause(
                    pair=p.pair,
                    until=repo.aware_utc(p.until) if p.until is not None else None,
                )
                for p in pauses
            ],
            errors_24h=errors_24h,
            errors_open=repo.unresolved_error_count(session),
            kill=engine.kill,
            today_trades=len(today),
            today_won=sum(1 for t in today if Decimal(t.realised_eur) > 0),
            return_since_start_pct=(
                float((equity / Decimal(broker.account.start_balance) - 1) * 100)
                if Decimal(broker.account.start_balance) > 0
                else 0.0
            ),
            needs=load_needs(state, session, now),
            btc_return_since_start_pct=float(btc_pct) if btc_pct is not None else None,
            account_started_at=repo.aware_utc(broker.account.started_at),
        )
    finally:
        session.close()
