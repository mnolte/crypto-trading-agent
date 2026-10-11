from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.api.notifications import NotificationDispatcher


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
    assert client.put("/api/settings", headers=headers, json=settings).status_code == 200
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
