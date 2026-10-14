"""Source registry endpoints: descriptors, enable/disable, keyring-backed
secrets (never returned, only "set" + last4) and the markets list."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import httpx
import keyring
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main
from app import repo
from app.api.deps import AppState
from app.config import get_env
from app.db import ErrorRow, MarketRow, make_engine, make_session_factory, utcnow
from app.repo import load_settings
from app.sources import SourceRegistry


def test_source_failure_threshold_and_recovery_callbacks() -> None:
    registry = SourceRegistry()
    failures: list[tuple[str, str]] = []
    recovered: list[str] = []
    registry.on_failure_threshold = lambda source_id, error: failures.append((source_id, error))
    registry.on_recovered = recovered.append

    for _ in range(4):
        registry.record("bitvavo", RuntimeError("offline"))
    assert failures == [("bitvavo", "offline")]
    registry.record("bitvavo", None)
    assert recovered == ["bitvavo"]


def test_source_recovery_requires_a_continuous_hour(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover at 10:00, fail at 10:30, recover at 10:50: the 10:00 timer
    must not resolve the error at 11:00 — only the latest generation can,
    and never while the source is failing again."""
    monkeypatch.setattr(app.main, "SOURCE_RECOVERY_RESOLVE_S", 0)
    state = cast(AppState, cast(FastAPI, client.app).state.app_state)
    session = state.session_factory()
    try:
        repo.add_activity(session, "error", "data source failing: bitvavo", at=utcnow())
        session.commit()
    finally:
        session.close()

    def unresolved() -> bool:
        s = state.session_factory()
        try:
            row = s.scalars(
                select(ErrorRow).where(ErrorRow.text == "data source failing: bitvavo")
            ).one()
            return row.resolved_at is None
        finally:
            s.close()

    # Generation 3 is the latest recovery; a stale timer must not resolve.
    generations = {"bitvavo": 3}
    asyncio.run(app.main._resolve_source_error_after_recovery(state, "bitvavo", 1, generations))
    assert unresolved()

    # Nor may the latest timer resolve while the source is failing again.
    state.sources.record("bitvavo", RuntimeError("still down"))
    asyncio.run(app.main._resolve_source_error_after_recovery(state, "bitvavo", 3, generations))
    assert unresolved()

    # Recovery without going through the callback (which would need a loop):
    # the current-generation timer now resolves the error.
    state.sources.status("bitvavo").runtime.failures = 0
    asyncio.run(app.main._resolve_source_error_after_recovery(state, "bitvavo", 3, generations))
    assert not unresolved()


def _token(client: TestClient) -> str:
    token: str = client.get("/api/session").json()["token"]
    return token


def test_sources_listed(client: TestClient) -> None:
    body = client.get("/api/sources").json()
    by_id = {s["id"]: s for s in body}
    assert set(by_id) == {"bitvavo", "openai", "okx", "deribit"}
    assert by_id["bitvavo"]["key_requirement"] == "none"
    assert by_id["bitvavo"]["enabled"] is True
    openai = by_id["openai"]
    assert openai["key_requirement"] == "required"
    assert openai["key_set"] is False
    assert openai["enabled"] is False
    assert "key" in openai["disabled_reason"].lower()


def test_missing_required_key_disables_not_errors(client: TestClient) -> None:
    openai = next(s for s in client.get("/api/sources").json() if s["id"] == "openai")
    assert openai["enabled"] is False
    assert openai["last_error"] is None


def test_secret_set_shows_last4_only(client: TestClient) -> None:
    token = _token(client)
    # Deliberately key-shaped so the last4 assertion is meaningful.
    key = "sk-test-abcdef1234567890wxyz"  # gitleaks:allow
    resp = client.put("/api/secrets/openai", json={"key": key}, headers={"X-Local-Token": token})
    assert resp.status_code == 200
    openai = next(s for s in client.get("/api/sources").json() if s["id"] == "openai")
    assert openai["key_set"] is True
    assert openai["key_last4"] == "wxyz"
    assert openai["enabled"] is True
    # The key itself never appears anywhere in the API response.
    assert key not in resp.text and key not in client.get("/api/sources").text


def test_secret_delete(client: TestClient) -> None:
    token = _token(client)
    client.put(
        "/api/secrets/openai", json={"key": "sk-test-0000"}, headers={"X-Local-Token": token}
    )
    resp = client.delete("/api/secrets/openai", headers={"X-Local-Token": token})
    assert resp.status_code == 200
    openai = next(s for s in client.get("/api/sources").json() if s["id"] == "openai")
    assert openai["key_set"] is False


def test_secret_rejected_for_keyless_source(client: TestClient) -> None:
    token = _token(client)
    resp = client.put(
        "/api/secrets/bitvavo", json={"key": "whatever"}, headers={"X-Local-Token": token}
    )
    assert resp.status_code == 422


@respx.mock
def test_secret_test_endpoint(client: TestClient) -> None:
    token = _token(client)
    client.put(
        "/api/secrets/openai", json={"key": "sk-test-good"}, headers={"X-Local-Token": token}
    )
    route = respx.get("https://api.openai.com/v1/models").mock(
        side_effect=lambda request: (
            httpx.Response(200, json={"data": []})
            if request.headers["Authorization"] == "Bearer sk-test-good"
            else httpx.Response(401, json={"error": {"message": "bad key"}})
        )
    )
    resp = client.post("/api/secrets/openai/test", headers={"X-Local-Token": token})
    assert resp.json() == {"ok": True, "error": None}
    assert route.called

    client.put("/api/secrets/openai", json={"key": "sk-test-bad"}, headers={"X-Local-Token": token})
    resp = client.post("/api/secrets/openai/test", headers={"X-Local-Token": token})
    assert resp.json()["ok"] is False
    assert "bad key" in resp.json()["error"]


def test_source_disable_persists(client: TestClient, env_paths: Path) -> None:
    token = _token(client)
    resp = client.put(
        "/api/sources/openai", json={"enabled": False}, headers={"X-Local-Token": token}
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["disabled_reason"] == "disabled in settings"

    engine = make_engine(get_env().ta_db_path)
    session = make_session_factory(engine)()
    try:
        assert "openai" in load_settings(session).disabled_sources
    finally:
        session.close()
        engine.dispose()

    resp = client.put(
        "/api/sources/openai", json={"enabled": True}, headers={"X-Local-Token": token}
    )
    assert resp.json()["disabled_reason"] == "API key missing"  # still no key


def test_unknown_source_404(client: TestClient) -> None:
    token = _token(client)
    assert (
        client.put(
            "/api/sources/nope", json={"enabled": True}, headers={"X-Local-Token": token}
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/secrets/nope", json={"key": "x"}, headers={"X-Local-Token": token}
        ).status_code
        == 404
    )


def test_market_activation_and_listing(sim_client: TestClient) -> None:
    token = _token(sim_client)

    # Seed an ineligible row, then activation must be refused with the rule.
    engine = make_engine(get_env().ta_db_path)
    session = make_session_factory(engine)()
    try:
        session.add(MarketRow(market="THIN-EUR", eligible=False, failed_rule="volume"))
        session.add(MarketRow(market="NEW-EUR", eligible=True, active=False))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    resp = sim_client.put(
        "/api/markets/THIN-EUR", json={"active": True}, headers={"X-Local-Token": token}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "volume"

    resp = sim_client.put(
        "/api/markets/NEW-EUR", json={"active": True}, headers={"X-Local-Token": token}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["eligible"] is True
    # The sim has full history: activation backfills and warming ends.
    assert body["warming_up"] is False

    listed = {m["market"]: m for m in sim_client.get("/api/markets").json()}
    assert listed["NEW-EUR"]["active"] is True
    assert listed["THIN-EUR"]["failed_rule"] == "volume"

    resp = sim_client.put(
        "/api/markets/NEW-EUR", json={"active": False}, headers={"X-Local-Token": token}
    )
    assert resp.status_code == 200 and resp.json()["active"] is False


def test_writes_require_token(client: TestClient) -> None:
    assert client.put("/api/sources/openai", json={"enabled": True}).status_code == 403
    assert client.put("/api/secrets/openai", json={"key": "x"}).status_code == 403
    assert client.delete("/api/secrets/openai").status_code == 403
    assert client.post("/api/secrets/openai/test").status_code == 403
    assert client.put("/api/markets/BTC-EUR", json={"active": True}).status_code == 403


def test_reads_survive_missing_keyring_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A headless host (CI, a server without dbus) has no keyring backend:
    get_password raises NoKeyringError. Reads must degrade to env/.env —
    /api/health and /api/sources keep answering."""
    monkeypatch.setattr(
        keyring,
        "get_password",
        _raise_no_keyring,
    )
    assert client.get("/api/health").status_code == 200
    openai = next(s for s in client.get("/api/sources").json() if s["id"] == "openai")
    assert openai["key_set"] is False


def test_secret_write_reports_missing_keyring_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no keyring backend a key cannot be stored — a clear 503, not a
    bare 500."""
    monkeypatch.setattr(keyring, "set_password", _raise_no_keyring)
    monkeypatch.setattr(keyring, "delete_password", _raise_no_keyring)
    token = _token(client)
    resp = client.put(
        "/api/secrets/openai", json={"key": "sk-test-0000"}, headers={"X-Local-Token": token}
    )
    assert resp.status_code == 503
    assert "keyring" in resp.json()["detail"].lower()
    resp = client.delete("/api/secrets/openai", headers={"X-Local-Token": token})
    assert resp.status_code == 503


def _raise_no_keyring(*args: object) -> None:
    raise keyring.errors.NoKeyringError
