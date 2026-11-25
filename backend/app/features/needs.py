"""The Needs you list and its health line — computed in code, cheaply.

Items are things only the user can resolve, in the spec's fixed order:
kill, approvals, errors, a failing source, email, an ineligible market,
the monthly cost cap. The health line reports Bitvavo candles, the price
stream, clock offset, the OpenAI key, the v0.2 collector and email.

``load_needs`` gathers the inputs; ``compute_needs`` is pure. A failure
anywhere yields an empty Needs — the card must never break a snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app import repo
from app.agent import budget
from app.agent.engine import PRICE_FALLBACK_S, WATCH_S
from app.db import AMSTERDAM, DecisionRow, LlmCallRow, MarketRow
from app.models import HealthItem, KillInfo, Needs, NeedsItem, NotificationSettings
from app.sources import SourceStatus

if TYPE_CHECKING:
    from app.api.deps import AppState

log = logging.getLogger(__name__)

CLOCK_WARN_S = 5.0
SOURCE_FAILURE_NEED = 3


@dataclass
class NeedsInputs:
    """Everything compute_needs looks at — gathered by load_needs."""

    now: datetime
    engine_status: str | None = None
    kill: KillInfo | None = None
    pending: list[DecisionRow] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    inspector_act: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    sources: list[SourceStatus] = field(default_factory=list)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    failing_markets: list[MarketRow] = field(default_factory=list)
    month_spend_eur: Decimal = Decimal("0")
    month_cap_eur: Decimal = Decimal("10")
    stale_grace_s: int = 900
    btc_1h_close: datetime | None = None
    price_fresh_at: datetime | None = None
    clock_offset_s: float | None = None
    collector: list[tuple[str, bool, datetime | None]] = field(default_factory=list)
    # (source name, enabled, newest stored ts)


def _email_needed(notifications: NotificationSettings) -> bool:
    return any(event.email for event in notifications.events.values())


def _iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _email_failing(errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            e
            for e in errors
            if e.get("event") == "notify.email_failed"
            or e.get("example") == "notification delivery failed"
        ),
        None,
    )


def compute_needs(inputs: NeedsInputs) -> Needs:
    """Items in the spec's order, then the health line."""
    items: list[NeedsItem] = []
    notif = inputs.notifications

    # 1. Kill switch fired, not acknowledged.
    if inputs.engine_status == "killed" or inputs.kill is not None:
        kill = inputs.kill
        detail = None
        if kill is not None:
            detail = (
                f"drawdown {float(kill.drawdown_pct):.1f}% from peak "
                f"€{float(kill.peak):,.0f} · positions closed"
            )
        items.append(
            NeedsItem(
                kind="kill",
                title="Kill switch fired",
                detail=detail,
                action_label="Review and acknowledge",
                action_target="/",
                since=repo.aware_utc(kill.at) if kill is not None else None,
            )
        )

    # 2. Trades waiting for approval.
    if inputs.pending:
        pairs = ", ".join(sorted({row.pair for row in inputs.pending}))
        first = min(inputs.pending, key=lambda row: row.at)
        execute = next(
            (repo.aware_utc(row.execute_at) for row in inputs.pending if row.execute_at), None
        )
        detail = pairs + (
            f" · veto window ends {execute.astimezone(AMSTERDAM):%H:%M}" if execute else ""
        )
        n = len(inputs.pending)
        items.append(
            NeedsItem(
                kind="approvals",
                title=f"{n} trade{'s' if n != 1 else ''} waiting for approval",
                detail=detail,
                action_label="Approve or veto",
                action_target="/",
                since=repo.aware_utc(first.at),
            )
        )

    # 3. Errors not reviewed — now the unreviewed problems in logs.db.
    if inputs.errors:
        texts = " · ".join(row["example"] for row in inputs.errors[:2])
        n = len(inputs.errors)
        first_seen = inputs.errors[-1].get("first_seen")
        items.append(
            NeedsItem(
                kind="errors",
                title=f"{n} error{'s' if n != 1 else ''} not reviewed",
                detail=f"{texts} · Readiness waits for these.",
                action_label="Review",
                action_target="/logs",
                since=_iso(first_seen) if isinstance(first_seen, str) else None,
            )
        )

    # Inspector act groups: one item per reason group until a later call
    # of that group succeeds. Attention itself is computed and frozen when
    # the call is written.
    for group in inputs.inspector_act:
        items.append(
            NeedsItem(
                kind="inspector",
                title=f"Model calls need you: {group['label']}",
                detail=group.get("detail"),
                action_label="Open inspector",
                action_target=f"/inspector?attention=act&reason={group['code']}&period=7d",
                since=_iso(str(group.get("at") or "")),
            )
        )

    # 3b. The app is degraded: a task is crash-looping or logs.db is
    # not writable.
    if inputs.degraded:
        items.append(
            NeedsItem(
                kind="degraded",
                title="The app is degraded",
                detail=" · ".join(inputs.degraded[:3]),
                action_label="Logs",
                action_target="/logs",
                since=None,
            )
        )

    # 4. An API key or data source failing.
    for status in inputs.sources:
        if status.runtime.failures >= SOURCE_FAILURE_NEED:
            items.append(
                NeedsItem(
                    kind="source",
                    title=f"{status.spec.name} failing",
                    detail=status.runtime.last_error or "3 failures in a row",
                    action_label="Fix in Settings",
                    action_target="/settings",
                    since=status.runtime.last_success_at,
                )
            )

    # 5. Email alerts not set up, or failing.
    failing_error = _email_failing(inputs.errors)
    email_setup = _email_needed(notif) and not notif.smtp.host
    if failing_error is not None:
        items.append(
            NeedsItem(
                kind="email",
                title="Email alerts are failing.",
                detail=str(failing_error.get("example") or ""),
                action_label="Fix in Settings",
                action_target="/settings",
                since=_iso(str(failing_error.get("first_seen") or "")),
            )
        )
    elif email_setup:
        items.append(
            NeedsItem(
                kind="email",
                title="Email alerts are not set up.",
                detail="Kill switch and daily loss alerts reach you only while a tab is open.",
                action_label="Set up in Settings",
                action_target="/settings",
            )
        )

    # 6. An active market failing the eligibility check.
    for row in inputs.failing_markets:
        items.append(
            NeedsItem(
                kind="market",
                title=f"{row.market} is failing the eligibility check",
                detail=row.failed_rule,
                action_label="Open Universe",
                action_target="/universe?filter=failing",
                since=repo.aware_utc(row.checked_at) if row.checked_at else None,
            )
        )

    # 7. Monthly cost cap at 80% or more.
    cap = inputs.month_cap_eur
    if cap > 0:
        pct = inputs.month_spend_eur / cap * 100
        if pct >= 80:
            items.append(
                NeedsItem(
                    kind="cost_cap",
                    title=f"Monthly cost cap at {float(pct):.0f}%",
                    detail=f"€{inputs.month_spend_eur:.2f} of €{cap:.2f} used this month",
                    action_label="Raise the cap",
                    action_target="/settings",
                )
            )

    return Needs(items=items, health=_health(inputs))


def _health(inputs: NeedsInputs) -> list[HealthItem]:
    now = inputs.now
    out: list[HealthItem] = []

    # Bitvavo: the newest stored BTC-EUR 1h close against the stale limit.
    if inputs.btc_1h_close is None:
        out.append(HealthItem(key="bitvavo", label="Bitvavo", state="unknown", value=None))
    else:
        close = inputs.btc_1h_close
        limit = timedelta(seconds=3600 + inputs.stale_grace_s)
        out.append(
            HealthItem(
                key="bitvavo",
                label="Bitvavo",
                state="ok" if now - close < limit else "warn",
                value=f"candles {close:%H:%M:%S}",
            )
        )

    # Price stream: freshest ticker update the watcher saw.
    fresh = inputs.price_fresh_at
    if fresh is None:
        out.append(HealthItem(key="stream", label="Price stream", state="down", value="down"))
    else:
        age = (now - fresh).total_seconds()
        if age <= 2 * WATCH_S:
            out.append(
                HealthItem(key="stream", label="Price stream", state="ok", value="connected")
            )
        elif age <= PRICE_FALLBACK_S:
            out.append(
                HealthItem(
                    key="stream",
                    label="Price stream",
                    state="warn",
                    value=f"stale {int(age)} s",
                )
            )
        else:
            out.append(HealthItem(key="stream", label="Price stream", state="down", value="down"))

    # Clock offset vs the exchange.
    if inputs.clock_offset_s is None:
        out.append(HealthItem(key="clock", label="Clock", state="unknown", value=None))
    else:
        offset = inputs.clock_offset_s
        out.append(
            HealthItem(
                key="clock",
                label="Clock",
                state="ok" if abs(offset) < CLOCK_WARN_S else "warn",
                value=f"{offset:+.1f} s",
            )
        )

    # OpenAI key from the source registry.
    openai = next((s for s in inputs.sources if s.spec.id == "openai"), None)
    if openai is None or not openai.key_set:
        out.append(HealthItem(key="openai", label="OpenAI key", state="warn", value="not set"))
    elif openai.runtime.failures >= SOURCE_FAILURE_NEED:
        out.append(HealthItem(key="openai", label="OpenAI key", state="down", value="failing"))
    elif openai.runtime.failures > 0:
        out.append(HealthItem(key="openai", label="OpenAI key", state="warn", value="unstable"))
    else:
        out.append(HealthItem(key="openai", label="OpenAI key", state="ok", value="passing"))

    # Collector (v0.2): OKX + Deribit stored context freshness.
    enabled = [(name, newest) for name, on, newest in inputs.collector if on]
    if not enabled:
        out.append(HealthItem(key="collector", label="Collector", state="off", value="off"))
    else:
        parts: list[str] = []
        stale = False
        for name, newest in enabled:
            if newest is None:
                stale = True
                parts.append(f"{name} —")
            else:
                stale = stale or (now - newest).total_seconds() > 3600
                parts.append(f"{name} {newest.astimezone(AMSTERDAM):%H:%M}")
        out.append(
            HealthItem(
                key="collector",
                label="Collector",
                state="warn" if stale else "ok",
                value=", ".join(parts),
            )
        )

    # Email.
    if _email_failing(inputs.errors) is not None:
        out.append(HealthItem(key="email", label="Email", state="down", value="failing"))
    elif not _email_needed(inputs.notifications):
        out.append(HealthItem(key="email", label="Email", state="off", value="off"))
    elif not inputs.notifications.smtp.host:
        out.append(HealthItem(key="email", label="Email", state="warn", value="not set up"))
    else:
        out.append(HealthItem(key="email", label="Email", state="ok", value="on"))

    return out


def _degraded(state: AppState) -> list[str]:
    """Crash-looping tasks and an unwritable log store make the app
    degraded — a Needs-you item until cleared."""
    from app import tasks as tasks_mod

    out = [f"task {name} keeps crashing" for name in sorted(tasks_mod.crash_loops)]
    if state.log_store is not None and state.log_store._broken:
        out.append("logs.db is not writable; lines go to the fallback file")
    return out


def load_needs(state: AppState, session: Session, now: datetime) -> Needs:
    """Gather every input and compute the Needs — cheap, never raises."""
    try:
        settings = state.settings
        engine = state.agent_engine
        account_id = state.broker.account_id if state.broker is not None else state.account_id
        btc = repo.load_candles(session, "BTC-EUR", "1h", 1)
        act_rows = session.scalars(
            select(LlmCallRow)
            .where(LlmCallRow.attention == "act")
            .order_by(desc(LlmCallRow.at))
            .limit(100)
        ).all()
        inspector_act: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in act_rows:
            import json as _json

            found = _json.loads(row.reasons or "[]")
            code = str(found[0].get("code")) if found else "error"
            if code in seen:
                continue
            later_success = session.scalar(
                select(LlmCallRow.id).where(
                    LlmCallRow.at > row.at,
                    LlmCallRow.outcome_status == "success",
                    LlmCallRow.reasons.like(f'%"{code}"%'),
                )
            )
            if later_success:
                continue
            seen.add(code)
            inspector_act.append(
                {
                    "code": code,
                    "label": code.replace("_", " "),
                    "detail": row.pair,
                    "at": repo.aware_utc(row.at).isoformat(),
                }
            )
        inputs = NeedsInputs(
            now=now,
            engine_status=engine.status if engine is not None else None,
            kill=engine.kill if engine is not None else None,
            pending=repo.pending_decisions(session, account_id),
            errors=state.log_store.unreviewed_problems() if state.log_store is not None else [],
            inspector_act=inspector_act,
            sources=state.sources.statuses(),
            notifications=settings.notifications,
            failing_markets=[
                row for row in repo.list_market_rows(session) if row.active and not row.eligible
            ],
            degraded=_degraded(state),
            month_spend_eur=budget.month_spend_all(session, now),
            month_cap_eur=settings.model.monthly_cost_cap_eur,
            stale_grace_s=settings.market.stale_grace_s,
            btc_1h_close=(btc[-1].open_time + timedelta(seconds=3600)) if btc else None,
            price_fresh_at=(
                max(engine._price_fresh_at.values(), default=None) if engine is not None else None
            ),
            clock_offset_s=engine.clock_offset_s if engine is not None else None,
            collector=[
                (
                    spec_name,
                    source_id not in settings.disabled_sources,
                    repo.newest_market_context(session, source_id),
                )
                for source_id, spec_name in (("okx", "OKX"), ("deribit", "Deribit"))
            ],
        )
        return compute_needs(inputs)
    except Exception:
        log.exception("needs assembly failed")
        return Needs()
