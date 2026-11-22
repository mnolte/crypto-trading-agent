"""Startup recovery gates: corrupt storage and failed migrations fail closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_mod
from app.agent.engine import Engine
from app.config import EnvConfig
from app.db import WhatifRow, make_engine, run_migrations, sqlite_quick_check, utcnow
from app.main import StartupRecoveryError, create_app
from app.models import WhatIfSpecIn


def _quiet_start(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.main.refresh_market_data", _noop)
    monkeypatch.setattr("app.main.daily_market_loop", _noop)
    monkeypatch.setattr(Engine, "refresh_prices", _noop)


def test_sqlite_quick_check_missing_ok_and_corrupt(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    assert sqlite_quick_check(missing) is None

    healthy = tmp_path / "healthy.db"
    eng = make_engine(healthy)
    eng.dispose()
    assert sqlite_quick_check(healthy) is None

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    assert sqlite_quick_check(corrupt) is not None
    assert sqlite_quick_check(corrupt, read_only=True) is not None


def test_startup_fails_closed_on_corrupt_trading_db(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = env_paths / "agent.db"
    payload = b"definitely not a sqlite database" * 4
    db_path.write_bytes(payload)
    migrate = MagicMock()
    _quiet_start(monkeypatch)
    monkeypatch.setattr(app_mod, "run_migrations", migrate)

    with pytest.raises(StartupRecoveryError, match="integrity check"):
        with TestClient(create_app(), base_url="http://127.0.0.1"):
            pass

    migrate.assert_not_called()
    assert db_path.read_bytes() == payload
    assert not (env_paths / "agent.db.lock").exists()


def test_startup_fails_closed_when_migrations_fail(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_start(monkeypatch)
    mock_log = MagicMock()
    monkeypatch.setattr(
        app_mod,
        "run_migrations",
        MagicMock(side_effect=RuntimeError("migration boom")),
    )

    with patch.object(app_mod, "log", mock_log):
        with pytest.raises(StartupRecoveryError, match="migrations failed"):
            with TestClient(create_app(), base_url="http://127.0.0.1"):
                pass

    critical = [
        call
        for call in mock_log.critical.call_args_list
        if call.kwargs.get("extra", {}).get("event") == "db.migration_failed"
    ]
    assert critical
    assert not (env_paths / "agent.db.lock").exists()


def test_unclean_start_closes_interrupted_work_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    eng = make_engine(db_path)
    run_migrations(db_path)
    now = "2026-11-22 00:00:00"
    with eng.begin() as conn:
        for status in ("queued", "running", "done"):
            conn.execute(
                text(
                    "INSERT INTO whatif_scenarios "
                    "(id, name, spec, status, created_at, updated_at) "
                    "VALUES (:id, 't', '{}', :status, :now, :now)"
                ),
                {"id": f"w-{status}", "status": status, "now": now},
            )
        conn.execute(
            text(
                "INSERT INTO sync_runs "
                "(job, started_at, status, done, failures, updated_at) "
                "VALUES ('daily_check', :now, 'running', 0, '[]', :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO sync_runs "
                "(job, started_at, status, done, failures, updated_at) "
                "VALUES ('exchange_rules', :now, 'ok', 1, NULL, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO market_checks "
                "(started_at, trigger, status, thresholds, funnel, kept, held_back) "
                "VALUES (:now, 'startup', 'ok', '{}', '[]', '[]', '[]')"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO market_checks "
                "(started_at, finished_at, trigger, status, thresholds, funnel, kept, held_back) "
                "VALUES (:now, :now, 'startup', 'ok', '{}', '[]', '[]', '[]')"
            ),
            {"now": now},
        )
    eng.dispose()

    env = EnvConfig(TA_DATA_DIR=str(tmp_path), TA_DB_PATH=str(db_path))
    app_mod._unclean_start(env)

    eng = make_engine(db_path)
    try:
        with eng.begin() as conn:
            whatif = dict(conn.execute(text("SELECT id, status FROM whatif_scenarios")).fetchall())
            sync = list(
                conn.execute(text("SELECT job, status, failures FROM sync_runs ORDER BY id"))
            )
            checks = list(
                conn.execute(
                    text("SELECT status, error, finished_at FROM market_checks ORDER BY id")
                )
            )
        assert whatif == {
            "w-queued": "interrupted",
            "w-running": "interrupted",
            "w-done": "done",
        }
        assert [(row.job, row.status) for row in sync] == [
            ("daily_check", "failed"),
            ("exchange_rules", "ok"),
        ]
        assert "unclean restart" in json.loads(sync[0].failures)[-1]["error"]
        assert [(row.status, row.finished_at is not None) for row in checks] == [
            ("failed", True),
            ("ok", True),
        ]
        assert "unclean restart" in (checks[0].error or "")

        counts = app_mod._interrupt_running_runs(eng, reason="again")
        assert counts == {"whatif": 0, "sync_runs": 0, "market_checks": 0}
    finally:
        eng.dispose()


def test_auxiliary_database_problems_are_reported_not_blocking(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"
    logs_dir.mkdir()
    backup_dir.mkdir()
    (logs_dir / "logs.db").write_bytes(b"corrupt logs database")
    (backup_dir / "agent-2026-11-22.db").write_bytes(b"corrupt backup database")
    env = EnvConfig(TA_DATA_DIR=str(tmp_path), TA_DB_PATH=str(tmp_path / "agent.db"))
    mock_log = MagicMock()

    with patch.object(app_mod, "log", mock_log):
        app_mod._report_auxiliary_database_problems(env)

    events = [
        call.kwargs.get("extra", {}).get("event")
        for call in mock_log.critical.call_args_list + mock_log.error.call_args_list
    ]
    assert "logs.corrupt" in events
    assert "backup.invalid" in events


def test_interrupted_whatif_row_is_api_readable(client: TestClient) -> None:
    state = cast(Any, client.app).state.app_state
    session = state.session_factory()
    try:
        spec = WhatIfSpecIn.model_validate(
            {
                "from": datetime(2026, 11, 1, tzinfo=UTC).isoformat(),
                "to": datetime(2026, 11, 2, tzinfo=UTC).isoformat(),
                "overrides": {},
            }
        )
        session.add(
            WhatifRow(
                id="w-int",
                name="saved",
                spec=spec.model_dump_json(by_alias=True),
                status="interrupted",
                progress=0.4,
                saved=True,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()

    res = client.get("/api/whatif/w-int")
    assert res.status_code == 200
    assert res.json()["status"] == "interrupted"
