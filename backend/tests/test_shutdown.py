"""Graceful shutdown: the spec's Ctrl+C/SIGTERM sequence end to end.

Shutdown is hard to drive through a real signal in tests (TestClient's
lifespan runs on a portal thread, so ``signal.signal`` never installs),
so the installed handlers are captured through a patched ``signal``
module and invoked directly, and ``TestClient.__exit__`` runs the
lifespan's shutdown side.
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as app_mod
from app.api import deps
from app.db import make_engine
from app.main import create_app


def _start(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient on the same quiet startup as the ``client`` fixture."""

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.main.refresh_market_data", _noop)
    monkeypatch.setattr("app.main.daily_market_loop", _noop)
    monkeypatch.setattr("app.agent.engine.Engine.refresh_prices", _noop)
    return TestClient(create_app(), base_url="http://127.0.0.1")


def _log_messages(data_dir: Path) -> list[str]:
    conn = sqlite3.connect(data_dir / "logs" / "logs.db")
    try:
        return [row[0] for row in conn.execute("SELECT message FROM log_lines ORDER BY id")]
    finally:
        conn.close()


def test_clean_shutdown_releases_lock_and_interrupts_runs(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _start(monkeypatch)
    client.__enter__()
    app = cast(FastAPI, client.app)
    state = app.state.app_state
    session = state.session_factory()
    try:
        session.execute(
            text(
                "INSERT INTO whatif_scenarios "
                "(id, name, spec, status, created_at, updated_at) "
                "VALUES ('w-shut', 't', '{}', 'running', :now, :now)"
            ),
            {"now": datetime.now(UTC)},
        )
        session.commit()
    finally:
        session.close()

    client.__exit__(None, None, None)

    # Lock released; WAL checkpointed (a leftover empty -wal is fine —
    # a finishing backup may still hold the file open).
    assert not (env_paths / "agent.db.lock").exists()
    wal = env_paths / "agent.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    eng = make_engine(env_paths / "agent.db")
    try:
        with eng.begin() as conn:
            status = conn.execute(
                text("SELECT status FROM whatif_scenarios WHERE id = 'w-shut'")
            ).scalar()
    finally:
        eng.dispose()
    assert status == "interrupted"

    messages = _log_messages(env_paths)
    assert "Shutdown requested" in messages
    assert any(m.startswith("Shutdown complete in") for m in messages)


def test_writes_get_503_reads_pass_while_shutting_down(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _start(monkeypatch)
    client.__enter__()
    cast(FastAPI, client.app).state.shutting_down = True
    res = client.post("/api/agent/pause", headers={"X-Local-Token": "x"})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "shutting_down"
    assert client.get("/api/health").status_code == 200
    client.__exit__(None, None, None)


def test_shutdown_waits_for_a_running_cycle(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _start(monkeypatch)
    client.__enter__()
    state = cast(FastAPI, client.app).state.app_state
    state.settings.storage.shutdown_grace_seconds = 5
    state.agent_engine.cycle_in_progress = True
    threading.Timer(0.3, setattr, args=(state.agent_engine, "cycle_in_progress", False)).start()
    started = time.monotonic()
    client.__exit__(None, None, None)
    elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 5


def test_second_sigint_skips_the_wait_but_still_shuts_down(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _start(monkeypatch)
    client.__enter__()
    app = cast(FastAPI, client.app)
    state = app.state.app_state
    state.settings.storage.shutdown_grace_seconds = 60
    state.agent_engine.cycle_in_progress = True  # never clears
    app.state.sigint_skip_wait = True
    started = time.monotonic()
    client.__exit__(None, None, None)
    elapsed = time.monotonic() - started
    assert elapsed < 10  # the 60 s grace was skipped
    assert not (env_paths / "agent.db.lock").exists()  # final steps still ran
    wal = env_paths / "agent.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_sigint_handler_first_announces_then_skips_then_forwards(
    env_paths: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First Ctrl+C forwards to uvicorn; the second logs a warning and
    skips only the wait; the third forwards as a last-resort abort."""
    installed: dict[int, Any] = {}
    previous = {signal.SIGINT: MagicMock(), signal.SIGTERM: MagicMock()}
    monkeypatch.setattr(signal, "getsignal", lambda sig: previous[sig])
    monkeypatch.setattr(signal, "signal", lambda sig, h: installed.__setitem__(sig, h))

    client = _start(monkeypatch)
    client.__enter__()
    app = cast(FastAPI, client.app)
    handler = installed[signal.SIGINT]

    handler(signal.SIGINT, None)
    assert app.state.sigint_requested is True
    assert app.state.shutdown_requested is True
    assert previous[signal.SIGINT].call_count == 1

    with caplog.at_level("WARNING", logger="app.main"):
        handler(signal.SIGINT, None)
    assert app.state.sigint_skip_wait is True
    assert previous[signal.SIGINT].call_count == 1  # not forwarded
    assert any("second interrupt" in r.message for r in caplog.records)

    handler(signal.SIGINT, None)
    assert previous[signal.SIGINT].call_count == 2  # last-resort abort

    # A first SIGTERM behaves like the first Ctrl+C.
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert previous[signal.SIGTERM].call_count == 1
    client.__exit__(None, None, None)


async def test_announce_shutdown_pushes_to_ws_clients() -> None:
    payloads: list[str] = []

    class FakeWS:
        async def send_text(self, payload: str) -> None:
            payloads.append(payload)

    app = FastAPI()
    state = MagicMock()
    state.ws_clients = {FakeWS()}
    app.state.app_state = state

    app_mod._announce_shutdown(app)
    await asyncio.sleep(0.1)
    assert len(payloads) == 1
    assert '"type":"alert"' in payloads[0]
    assert "shutting down" in payloads[0]
    # Once only.
    app_mod._announce_shutdown(app)
    await asyncio.sleep(0.05)
    assert len(payloads) == 1


async def test_cancelled_request_ends_quietly_during_shutdown() -> None:
    """Uvicorn cancels requests still running when its graceful-shutdown
    window expires; once shutdown is requested that must not surface as
    an ASGI error, and outside shutdown it must still propagate."""

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        raise asyncio.CancelledError

    async def send(message: Any) -> None:
        return None

    async def run(flag: bool) -> None:
        app = FastAPI()
        app.state.shutdown_requested = flag
        middleware = deps.RequestIdMiddleware(inner)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/health",
            "headers": [],
            "app": app,
        }
        await middleware(scope, None, send)  # type: ignore[arg-type]

    await run(True)
    with pytest.raises(asyncio.CancelledError):
        await run(False)


async def test_drain_tasks_gives_emails_a_send_window() -> None:
    finished: list[str] = []

    async def email() -> None:
        await asyncio.sleep(0.05)
        finished.append("email")

    async def forever() -> None:
        await asyncio.sleep(3600)

    tasks = [
        asyncio.create_task(email(), name="notify-email"),
        asyncio.create_task(forever(), name="job-backup"),
    ]
    await app_mod._drain_tasks(tasks, lambda: False)
    assert finished == ["email"]  # the email send was not cut off
    assert all(t.done() for t in tasks)

    # Forced: even emails cancel at once.
    tasks = [
        asyncio.create_task(forever(), name="notify-email"),
        asyncio.create_task(forever(), name="job-backup"),
    ]
    await app_mod._drain_tasks(tasks, lambda: True)
    assert all(t.done() for t in tasks)
