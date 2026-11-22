from __future__ import annotations

import sqlite3
from collections.abc import Callable
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
    state.log_store.submit(
        _problem_line("source failed", "agent.scheduler", "candles.fetch_failed")
    )
    _wait_for(lambda: client.get("/api/snapshot").json()["errors_24h"] == 1)
    problem = client.get("/api/logs/problems").json()["problems"][0]
    token = client.get("/api/session").json()["token"]
    resolved = client.post(
        f"/api/logs/problems/{problem['fingerprint']}/review",
        headers={"X-Local-Token": token},
        json={"state": "fixed", "note": "source recovered"},
    )
    assert resolved.status_code == 200
    assert client.get("/api/snapshot").json()["errors_24h"] == 0


def test_error_activity_stays_out_of_the_legacy_table(client: TestClient) -> None:
    """kind "error" writes nothing now — errors live in logs.db."""
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        repo.add_activity(session, "error", "same failure", pair="BTC-EUR")
        session.commit()
    finally:
        session.close()
    assert client.get("/api/errors").json() == []
    # The same failure logged twice is one problem with count 2. The
    # writer commits in batches, so the two lines can land in different
    # batches: wait for the count, not just for the problem to exist.
    for _ in range(2):
        state.log_store.submit(
            _problem_line("same failure", "agent.scheduler", "cycle.step_failed")
        )
    _wait_for(lambda: any(p["count"] == 2 for p in state.log_store.unreviewed_problems()))
    probs = state.log_store.unreviewed_problems()
    assert len(probs) == 1 and probs[0]["count"] == 2 and probs[0]["example"] == "same failure"


def test_open_error_count_includes_errors_older_than_24h(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    state.log_store.submit(
        _problem_line(
            "old unresolved error",
            "agent.scheduler",
            "cycle.step_failed",
            ts=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        )
    )
    _wait_for(lambda: state.log_store.unreviewed_problems())
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["errors_24h"] == 0
    assert snapshot["errors_open"] == 1


def _problem_line(
    text: str,
    logger: str,
    event: str,
    ts: str | None = None,
) -> dict[str, Any]:
    from app.logstore.record import COLUMNS, fingerprint

    line: dict[str, Any] = dict.fromkeys(COLUMNS)
    line.update(
        {
            "ts": ts or datetime.now(UTC).isoformat(),
            "level": "ERROR",
            "levelno": 40,
            "logger": logger,
            "event": event,
            "message": text,
            "process": "app",
            "pid": 1,
            "fingerprint": fingerprint(logger, event, text),
            "version": "test",
        }
    )
    return line


def _wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        _time.sleep(0.02)
    raise AssertionError("timed out waiting")
