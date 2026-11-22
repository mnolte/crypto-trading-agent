"""Needs you: one item per cause, in spec order, gone when the cause is gone."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from fastapi.testclient import TestClient

from app.api.deps import AppState
from app.db import DecisionRow, LlmCallRow, MarketRow
from app.features.needs import load_needs
from app.logstore.record import COLUMNS, fingerprint
from app.models import KillInfo, Needs, NotificationEventSettings

NOW = datetime(2026, 10, 13, 12, 34, tzinfo=UTC)


def _state(client: TestClient) -> AppState:
    app: object = client.app
    return cast(AppState, app.state.app_state)  # type: ignore[attr-defined]


def _needs(state: AppState, now: datetime | None = None) -> Needs:
    session = state.session_factory()
    try:
        return load_needs(state, session, now or datetime.now(UTC))
    finally:
        session.close()


def _decision_row(**kw: object) -> DecisionRow:
    defaults: dict[str, object] = {
        "id": "d1",
        "cycle": 1,
        "at": NOW.replace(tzinfo=None),
        "account_id": 1,
        "pair": "SOL-EUR",
        "decider": "openai",
        "model": "gpt-4o-mini",
        "prompt_version": "decision.v1",
        "action": "HOLD",
        "confidence": 0.5,
        "reasoning": "test",
        "invalidation": None,
        "signals": "[]",
        "plan": None,
        "gates": "[]",
        "status": "none",
        "execute_at": None,
        "error": None,
        "regime": "{}",
        "btc_regime": "{}",
        "invalidation_price": None,
        "risk_pct_used": "1",
        "settings_version": 1,
        "shadow_id": None,
        "stand_in": False,
        "raw_brief": None,
        "raw_response": None,
        "prefilter": True,
    }
    defaults.update(kw)
    return DecisionRow(**defaults)


def _log_problem(
    state: object,
    text: str = "cycle failed for SOL-EUR: boom",
    *,
    event: str = "cycle.step_failed",
    pair: str | None = None,
) -> str:
    """Write an ERROR line into the test app's logs.db — an unreviewed
    problem, which is what the Needs items read now."""
    line: dict[str, Any] = dict.fromkeys(COLUMNS)
    line.update(
        {
            "ts": NOW.isoformat(),
            "level": "ERROR",
            "levelno": 40,
            "logger": "agent.scheduler",
            "event": event,
            "message": text,
            "process": "app",
            "pid": 1,
            "pair": pair,
            "fingerprint": fingerprint("agent.scheduler", event, text),
            "version": "test",
        }
    )
    state.log_store.write_lines([line])  # type: ignore[attr-defined]
    return str(line["fingerprint"])


def _no_email(state: AppState) -> None:
    """Turn every notification event's email flag off (and back)."""
    for key, event in state.settings.notifications.events.items():
        state.settings.notifications.events[key] = NotificationEventSettings(
            browser=event.browser, email=False
        )


def test_empty_when_nothing_is_wrong(client: TestClient) -> None:
    state = _state(client)
    _no_email(state)
    needs = _needs(state)
    assert needs.items == []
    assert [h.key for h in needs.health] == [
        "bitvavo",
        "stream",
        "clock",
        "openai",
        "collector",
        "email",
    ]


def test_kill_item(client: TestClient) -> None:
    state = _state(client)
    engine = state.agent_engine
    assert engine is not None
    engine.status = "killed"
    engine.kill = KillInfo(
        at=NOW,
        peak=Decimal("11000"),
        equity=Decimal("10000"),
        drawdown_pct=Decimal("9.1"),
        closed=["SOL-EUR"],
    )
    item = _needs(state).items[0]
    assert item.kind == "kill"
    assert item.title == "Kill switch fired"
    assert item.action_target == "/"
    assert item.since == NOW
    assert "drawdown" in (item.detail or "") and "peak" in (item.detail or "")
    engine.status = "paused"
    engine.kill = None
    assert all(i.kind != "kill" for i in _needs(state).items)


def test_pending_approvals_item(client: TestClient) -> None:
    state = _state(client)
    session = state.session_factory()
    try:
        session.add(_decision_row(status="pending", pair="SOL-EUR"))
        session.add(_decision_row(id="d2", status="pending", pair="ETH-EUR"))
        session.commit()
    finally:
        session.close()
    item = next(i for i in _needs(state).items if i.kind == "approvals")
    assert "2 trades" in item.title
    assert "ETH-EUR" in (item.detail or "") and "SOL-EUR" in (item.detail or "")
    assert item.action_target == "/"


def test_unresolved_errors_item(client: TestClient) -> None:
    state = _state(client)
    _log_problem(state, "cycle failed for SOL-EUR: boom")
    _log_problem(state, "watcher tick failed", event="task.crashed")
    item = next(i for i in _needs(state).items if i.kind == "errors")
    assert item.title == "2 errors not reviewed"
    assert item.action_target == "/logs"
    for p in state.log_store.unreviewed_problems():
        state.log_store.review_problem(p["fingerprint"], "fixed", "seen", NOW)
    assert all(i.kind != "errors" for i in _needs(state).items)


def test_failing_source_item(client: TestClient) -> None:
    state = _state(client)
    # Detach the notification callback: it schedules a task on the app's
    # loop, which is not this test's.
    state.sources.on_failure_threshold = None
    for _ in range(3):
        state.sources.record("bitvavo", RuntimeError("timeout"))
    item = next(i for i in _needs(state).items if i.kind == "source")
    assert "Bitvavo" in item.title
    assert item.action_target == "/settings"
    openai = next(h for h in _needs(state).health if h.key == "openai")
    assert openai.state == "warn" and openai.value == "not set"


def test_email_not_set_up_then_failing(client: TestClient) -> None:
    state = _state(client)
    # Default settings: kill_switch/daily_loss email is on, no SMTP host.
    item = next(i for i in _needs(state).items if i.kind == "email")
    assert item.title == "Email alerts are not set up."
    # Configure a host: the item leaves.
    state.settings.notifications.smtp.host = "smtp.example.com"
    assert all(i.kind != "email" for i in _needs(state).items)
    # A stored delivery failure brings it back, stronger wording.
    _log_problem(state, "notification delivery failed", event="notify.email_failed")
    item = next(i for i in _needs(state).items if i.kind == "email")
    assert item.title == "Email alerts are failing."
    health = next(h for h in _needs(state).health if h.key == "email")
    assert health.state == "down"


def test_ineligible_active_market_item(client: TestClient) -> None:
    state = _state(client)
    session = state.session_factory()
    try:
        row = session.get(MarketRow, "SOL-EUR") or MarketRow(market="SOL-EUR")
        row.active = True
        row.eligible = False
        row.failed_rule = "30-day volume below the minimum"
        session.merge(row)
        session.commit()
    finally:
        session.close()
    item = next(i for i in _needs(state).items if i.kind == "market")
    assert "SOL-EUR" in item.title
    assert item.action_target == "/universe?filter=failing"


def test_cost_cap_item(client: TestClient) -> None:
    state = _state(client)
    session = state.session_factory()
    try:
        session.add(
            LlmCallRow(
                id="c1",
                at=datetime.now(UTC).replace(tzinfo=None),
                cycle=1,
                account_id=1,
                pair="SOL-EUR",
                decision_id=None,
                feature="decision",
                replay_of=None,
                model="gpt-4o-mini",
                prompt_version="decision.v1",
                status="ok",
                attempts="[]",
                latency_ms=1,
                input_tokens=10,
                output_tokens=5,
                cost_usd="9",
                cost_eur="8.5",  # 85% of the €10 default cap
                request_id=None,
                request_json="{}",
                response_raw="{}",
                parsed=None,
                input_checks="[]",
                output_checks="[]",
                verdict=None,
                verdict_note=None,
                verdict_at=None,
            )
        )
        session.commit()
    finally:
        session.close()
    item = next(i for i in _needs(state).items if i.kind == "cost_cap")
    assert "85%" in item.title
    assert "8.5" in (item.detail or "")
    assert item.action_target == "/settings"


def test_item_order_follows_the_spec_table(client: TestClient) -> None:
    state = _state(client)
    engine = state.agent_engine
    assert engine is not None
    engine.status = "killed"
    engine.kill = KillInfo(
        at=NOW,
        peak=Decimal("11000"),
        equity=Decimal("10000"),
        drawdown_pct=Decimal("9"),
        closed=[],
    )
    session = state.session_factory()
    try:
        session.add(_decision_row(status="pending"))
        session.commit()
    finally:
        session.close()
    _log_problem(state)
    kinds = [i.kind for i in _needs(state).items]
    assert kinds[:3] == ["kill", "approvals", "errors"]


def test_needs_endpoint(client: TestClient) -> None:
    body = client.get("/api/needs").json()
    assert "items" in body and "health" in body
    assert len(body["health"]) == 6
