"""Settings persistence and the skeleton snapshot."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import make_engine, make_session_factory
from app.models import DEFAULT_MARKETS
from app.repo import load_settings, settings_version


def test_settings_defaults(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["active_markets"] == DEFAULT_MARKETS
    assert body["execution_mode"] == "auto"
    assert body["data_source"] == "bitvavo"
    assert body["model"]["decider"] == "openai"
    assert body["model"]["model_id"] == "gpt-4o-mini"
    assert body["model"]["prompt_version"] == "decision.v1"
    assert body["paper"]["start_balance_eur"] == "10000"
    assert body["risk"]["kill_drawdown_pct"] == "8"


def test_settings_put_and_reload(client: TestClient, env_paths: object) -> None:
    token = client.get("/api/session").json()["token"]
    current = client.get("/api/settings").json()
    current["model"]["decider"] = "rules"
    current["risk"]["min_rr"] = 2.5
    response = client.put("/api/settings", json=current, headers={"X-Local-Token": token})
    assert response.status_code == 200

    # Reload straight from the database: the document survived persistence.
    from app.config import get_env

    engine = make_engine(get_env().ta_db_path)
    session = make_session_factory(engine)()
    try:
        stored = load_settings(session)
        assert stored.model.decider == "rules"
        assert str(stored.risk.min_rr) == "2.5"
        assert settings_version(session) >= 2
    finally:
        session.close()
        engine.dispose()


def test_settings_unknown_keys_dropped(env_paths: object) -> None:
    from app.config import get_env
    from app.db import run_migrations

    run_migrations(get_env().ta_db_path)
    engine = make_engine(get_env().ta_db_path)
    session = make_session_factory(engine)()
    try:
        settings = load_settings(session)
        doc = settings.model_dump()
        doc["bogus_key"] = {"not": "real"}
        assert "bogus_key" not in type(settings).model_validate(doc).model_dump()
    finally:
        session.close()
        engine.dispose()


def test_settings_invalid_rejected(client: TestClient) -> None:
    token = client.get("/api/session").json()["token"]
    body = client.get("/api/settings").json()
    body["execution_mode"] = "yolo"
    response = client.put("/api/settings", json=body, headers={"X-Local-Token": token})
    assert response.status_code == 422


def test_snapshot_shape(client: TestClient) -> None:
    snap = client.get("/api/snapshot").json()
    assert snap["status"] == "paused"
    assert snap["mode"] == "paper"
    assert snap["source"] == "bitvavo"
    assert snap["equity"] == "10000"  # money travels as strings
    assert snap["cash"] == "10000"
    assert snap["today_pnl"] == "0"
    assert snap["positions"] == []
    assert snap["pending"] == []
    assert snap["kill"] is None
    assert snap["usage"]["daily_budget"] == 1000
    assert snap["usage"]["cap_eur"] == "10"


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "paused"
    assert body["sources"] == {"bitvavo": "ok"}
