"""Failure injection for the PR 15 error-handling paths: supervised
tasks, global hooks, unclean-start detection and the API error shape."""

from __future__ import annotations

import asyncio
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main
from app import tasks as tasks_mod
from app.api.deps import AppState
from app.config import EnvConfig
from app.db import make_engine, run_migrations


@pytest.fixture(autouse=True)
def _reset_crash_state() -> None:
    tasks_mod._crashes.clear()
    tasks_mod.crash_loops.clear()


def _state(client: TestClient) -> AppState:
    return cast(AppState, cast(FastAPI, client.app).state.app_state)


async def test_spawn_supervisor_restarts_a_crashed_task() -> None:
    """A crashed supervised task is logged with task.crashed and
    restarted with backoff."""
    runs = 0

    async def crash_once() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    mock_log = MagicMock()
    with (
        patch.object(tasks_mod, "log", mock_log),
        patch.object(tasks_mod, "BACKOFF_START_S", 0.01),
    ):
        task = tasks_mod.spawn(crash_once, name="test-crash")
        await asyncio.sleep(0.2)
        task.cancel()
    assert runs == 2
    error_calls = [
        c
        for c in mock_log.exception.call_args_list
        if c.kwargs.get("extra", {}).get("event") == "task.crashed"
    ]
    assert len(error_calls) == 1


async def test_spawn_oneshot_records_the_crash_once() -> None:
    """restart=False: the crash is recorded and logged; no restart."""

    async def dies() -> None:
        raise ValueError("nope")

    mock_log = MagicMock()
    with patch.object(tasks_mod, "log", mock_log):
        task = tasks_mod.spawn(dies, name="oneshot", restart=False)
        await asyncio.sleep(0.1)
        assert task.done()
    assert "oneshot" in tasks_mod._crashes
    assert mock_log.exception.call_count == 1


def test_record_crash_marks_a_crash_loop() -> None:
    for i in range(tasks_mod.CRASH_LIMIT - 1):
        assert tasks_mod.record_crash("flaky") is False, i
    assert tasks_mod.record_crash("flaky") is True
    assert "flaky" in tasks_mod.crash_loops


def test_degraded_needs_item_covers_crash_loops(client: TestClient) -> None:
    state = _state(client)
    tasks_mod.crash_loops.add("flaky")
    from app.features.needs import _degraded

    assert any("flaky" in d for d in _degraded(state))


def test_global_hooks_log_uncaught(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_log = MagicMock()
    monkeypatch.setattr(tasks_mod, "log", mock_log)
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda *a: None)
    tasks_mod.install_global_hooks()
    exc = RuntimeError("unhandled")
    sys.excepthook(RuntimeError, exc, exc.__traceback__)
    threading.excepthook(
        threading.ExceptHookArgs(
            (RuntimeError, exc, exc.__traceback__, None),
        )
    )
    events = [
        c.kwargs.get("extra", {}).get("event")
        for c in mock_log.critical.call_args_list + mock_log.error.call_args_list
    ]
    assert "task.crashed" in events


def test_unclean_start_marks_interrupted_runs(tmp_path: Path) -> None:
    """A stale lock on startup: warning logged, quick_check passes, a
    'running' what-if run is marked interrupted."""
    db_path = tmp_path / "agent.db"
    eng = make_engine(db_path)
    run_migrations(db_path)
    with eng.begin() as conn:
        conn.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO whatif_scenarios "
                "(id, name, spec, status, created_at, updated_at) "
                "VALUES ('w1', 't', '{}', 'running', :now, :now)"
            ),
            {"now": datetime.now(UTC)},
        )
    eng.dispose()
    env = EnvConfig(TA_DATA_DIR=str(tmp_path), TA_DB_PATH=str(db_path))
    mock_log = MagicMock()
    with patch.object(app.main, "log", mock_log):
        app.main._unclean_start(env)
    eng = make_engine(db_path)
    with eng.begin() as conn:
        status = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT status FROM whatif_scenarios WHERE id = 'w1'"
            )
        ).scalar()
    eng.dispose()
    assert status == "interrupted"
    warned = [
        c
        for c in mock_log.warning.call_args_list
        if c.kwargs.get("extra", {}).get("event") == "app.unclean_shutdown"
    ]
    assert warned


def test_api_error_shape_and_request_id(client: TestClient) -> None:
    res = client.get("/api/no-such-route", headers={"X-Request-Id": "r-test-1"})
    assert res.status_code == 404
    body = res.json()["error"]
    assert body["code"] == "not_found" and body["message"]
    assert res.headers["x-request-id"] == "r-test-1"
    # A method mismatch lands on the API catch-all: still the shaped
    # error, never the SPA's index.html.
    res = client.post("/api/needs", headers={"X-Local-Token": "x"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    # Validation errors share the shape.
    res = client.post(
        "/api/logs/client",
        headers={"X-Local-Token": _token(client)},
        json={"level": 5},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid"


def _token(client: TestClient) -> str:
    return str(client.get("/api/session").json()["token"])


def test_store_handler_survives_migrations(tmp_path: Path) -> None:
    """alembic's fileConfig used to wipe root.handlers; the store must
    still be attached after run_migrations so app lines reach logs.db."""
    import logging

    from app import logstore
    from app.db import run_migrations

    store = logstore.install(tmp_path)
    try:
        run_migrations(tmp_path / "agent.db")
        assert any(
            type(h).__name__ == "StoreHandler"
            for h in logging.getLogger().handlers
        )
        logging.getLogger("app").warning("post-migration probe")
        import time as _t

        _t.sleep(1.5)
        import sqlite3 as _s

        conn = _s.connect(str(store.db_path))
        rows = conn.execute(
            "SELECT COUNT(*) FROM log_lines WHERE message LIKE '%post-migration probe%'"
        ).fetchone()[0]
        conn.close()
        assert rows == 1
    finally:
        logstore.shutdown()
