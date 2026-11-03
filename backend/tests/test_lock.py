"""The single-engine lock file next to the database."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import EngineAlreadyRunningError, EngineLock
from app.main import create_app


def test_second_lock_is_refused(tmp_path: Path) -> None:
    lock = EngineLock(tmp_path / "agent.db")
    lock.acquire()
    try:
        with pytest.raises(EngineAlreadyRunningError):
            EngineLock(tmp_path / "agent.db").acquire()
    finally:
        lock.release()


def test_lock_reacquired_after_release(tmp_path: Path) -> None:
    lock = EngineLock(tmp_path / "agent.db")
    lock.acquire()
    lock.release()
    EngineLock(tmp_path / "agent.db").acquire()


def test_second_app_instance_refused(env_paths: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_refresh(*args: object) -> None:
        return None

    monkeypatch.setattr("app.agent.engine.Engine.refresh_prices", _no_refresh)
    monkeypatch.setattr("app.main.refresh_market_data", _no_refresh)
    monkeypatch.setattr("app.main.daily_market_loop", _no_refresh)
    with TestClient(create_app()):
        with pytest.raises(EngineAlreadyRunningError):
            with TestClient(create_app()):
                pass
