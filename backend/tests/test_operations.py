from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from app import repo
from app.operations import backup_database, backup_due, next_backup_at


def test_backup_uses_sqlite_api_and_keeps_fourteen(tmp_path: Path) -> None:
    db = tmp_path / "agent.db"
    with sqlite3.connect(db) as connection:
        connection.execute("create table sample (value text)")
        connection.execute("insert into sample values ('kept')")
    data_dir = tmp_path / "data"
    now = datetime(2026, 10, 11, 3, tzinfo=UTC)
    for days in range(16):
        backup_database(db, data_dir, now - timedelta(days=days))
    backups = sorted((data_dir / "backups").glob("agent-*.db"))
    assert len(backups) == 14
    with sqlite3.connect(data_dir / "backups" / "agent-2026-10-11.db") as connection:
        assert connection.execute("select value from sample").fetchone() == ("kept",)


def test_backup_waits_until_0300_and_runs_once_per_date(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    before = datetime(2026, 10, 11, 2, 59, tzinfo=UTC)
    due = datetime(2026, 10, 11, 3, tzinfo=UTC)
    assert not backup_due(data_dir, before)
    assert next_backup_at(before) == due
    assert backup_due(data_dir, due)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True)
    (backup_dir / "agent-2026-10-11.db").touch()
    assert not backup_due(data_dir, due + timedelta(hours=1))
    assert next_backup_at(due) == due + timedelta(days=1)


def test_error_badge_count_and_resolution(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        repo.add_activity(session, "error", "source failed", pair="BTC-EUR")
        session.commit()
    finally:
        session.close()
    assert client.get("/api/snapshot").json()["errors_24h"] == 1
    error = client.get("/api/errors").json()[0]
    token = client.get("/api/session").json()["token"]
    resolved = client.put(
        f"/api/errors/{error['id']}/resolve",
        headers={"X-Local-Token": token},
        json={"note": "source recovered"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolution_note"] == "source recovered"
    assert client.get("/api/snapshot").json()["errors_24h"] == 0


def test_repeated_errors_update_one_open_error(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        repo.add_activity(session, "error", "same failure", pair="BTC-EUR")
        session.commit()
        repo.add_activity(session, "error", "same failure", pair="BTC-EUR")
        session.commit()
    finally:
        session.close()
    errors = client.get("/api/errors").json()
    assert len(errors) == 1
    assert errors[0]["text"] == "same failure"


def test_open_error_count_includes_errors_older_than_24h(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        repo.add_activity(
            session,
            "error",
            "old unresolved error",
            at=datetime.now(UTC) - timedelta(days=2),
        )
        session.commit()
    finally:
        session.close()
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["errors_24h"] == 0
    assert snapshot["errors_open"] == 1
