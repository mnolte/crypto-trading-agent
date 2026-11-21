from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.notifications import NotificationDispatcher
from app.db import AlertRow, utcnow


def _token(client: TestClient) -> dict[str, str]:
    return {"X-Local-Token": client.get("/api/session").json()["token"]}


def test_notification_settings_and_smtp_password_never_return_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _token(client)
    response = client.put(
        "/api/notifications/smtp-password",
        headers=headers,
        json={"password": "not-a-real-password"},
    )
    assert response.status_code == 200
    assert response.json() == {"set": True, "last4": "word"}
    assert "not-a-real-password" not in response.text
    assert "password" not in client.get("/api/notifications").text

    sent = Mock()
    monkeypatch.setattr("app.api.notifications._send_email", sent)
    settings = client.get("/api/settings").json()
    settings["notifications"]["email_enabled"] = True
    settings["notifications"]["smtp"].update(
        {
            "host": "smtp.example.test",
            "sender": "agent@example.test",
            "recipient": "marc@example.test",
        }
    )
    assert (
        client.put(
            "/api/settings",
            headers=headers,
            json={"settings": settings, "reason": "smtp setup"},
        ).status_code
        == 200
    )
    result = client.post("/api/notifications/test", headers=headers)
    assert result.status_code == 200
    assert result.json()["email"] is True
    sent.assert_called_once()


async def test_real_notification_dispatches_and_deduplicates(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    state.settings.notifications.browser_enabled = True
    websocket = Mock()
    websocket.send_text = AsyncMock()
    state.ws_clients.add(websocket)
    dispatcher = NotificationDispatcher(state)

    await dispatcher.notify("kill_switch", "Kill switch fired", "Drawdown hit")
    await dispatcher.notify("kill_switch", "Kill switch fired", "Drawdown hit")

    websocket.send_text.assert_awaited_once()
    assert '"type":"alert"' in websocket.send_text.await_args.args[0]


async def test_browser_alert_dedup_survives_restart(client: TestClient) -> None:
    """A fresh dispatcher has no in-memory send history: the alerts table is
    the 30-minute repeat rule's memory across restarts."""
    state = cast(Any, client.app).state.app_state
    state.settings.notifications.browser_enabled = True
    websocket = Mock()
    websocket.send_text = AsyncMock()
    state.ws_clients.add(websocket)

    await NotificationDispatcher(state).notify("kill_switch", "Kill switch fired", "Drawdown hit")
    # Cold start — the second dispatcher must find the sent row in the table.
    await NotificationDispatcher(state).notify("kill_switch", "Kill switch fired", "Drawdown hit")

    websocket.send_text.assert_awaited_once()


async def test_alert_row_records_folded_repeats(client: TestClient) -> None:
    """Suppressed sends fold into the next send's row as a repeat count."""
    state = cast(Any, client.app).state.app_state
    state.settings.notifications.browser_enabled = True
    websocket = Mock()
    websocket.send_text = AsyncMock()
    state.ws_clients.add(websocket)
    dispatcher = NotificationDispatcher(state)
    key = ("kill_switch", "Kill switch fired", "browser")

    dispatcher._last_sent[key] = utcnow()
    await dispatcher.notify("kill_switch", "Kill switch fired", "Drawdown hit")
    await dispatcher.notify("kill_switch", "Kill switch fired", "Drawdown hit")
    dispatcher._last_sent[key] = utcnow() - timedelta(minutes=31)
    await dispatcher.notify("kill_switch", "Kill switch fired", "Drawdown hit")

    assert "(2 repeats)" in websocket.send_text.await_args.args[0]
    session = state.session_factory()
    try:
        row = session.scalar(select(AlertRow).order_by(AlertRow.at.desc()).limit(1))
    finally:
        session.close()
    assert row is not None and row.repeats == 2
