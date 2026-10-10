"""Local-only security rules: local token, Host header, WebSocket Origin."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def _token(client: TestClient) -> str:
    token: str = client.get("/api/session").json()["token"]
    return token


WS_HOST = {"host": "127.0.0.1"}


def test_session_returns_token(client: TestClient) -> None:
    token = _token(client)
    assert isinstance(token, str) and len(token) >= 32


def test_session_has_no_cors_headers(client: TestClient) -> None:
    response = client.get("/api/session")
    assert "access-control-allow-origin" not in response.headers


def test_write_requires_local_token(client: TestClient) -> None:
    assert client.put("/api/settings", json={}).status_code == 403
    assert (
        client.put("/api/settings", json={}, headers={"X-Local-Token": "wrong"}).status_code == 403
    )


def test_write_with_local_token(client: TestClient) -> None:
    response = client.put("/api/settings", json={}, headers={"X-Local-Token": _token(client)})
    assert response.status_code == 200


def test_reads_do_not_require_token(client: TestClient) -> None:
    assert client.get("/api/session").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/snapshot").status_code == 200
    assert client.get("/api/settings").status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "localhost"])
def test_local_hosts_allowed(client: TestClient, host: str) -> None:
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "169.254.169.254", "127.0.0.1.evil.com", ""])
def test_foreign_hosts_rejected(client: TestClient, host: str) -> None:
    assert client.get("/api/health", headers={"Host": host}).status_code == 403


def test_websocket_sends_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws", headers=WS_HOST) as ws:
        message = ws.receive_json()
    assert message["type"] == "snapshot"
    assert message["data"]["status"] == "paused"


def test_websocket_accepts_local_origin(client: TestClient) -> None:
    headers = {**WS_HOST, "origin": "http://localhost:5173"}
    with client.websocket_connect("/ws", headers=headers) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_websocket_rejects_foreign_origin(client: TestClient) -> None:
    headers = {**WS_HOST, "origin": "https://evil.example"}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=headers):
            pass


def test_websocket_rejects_foreign_host(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"host": "evil.example"}):
            pass
