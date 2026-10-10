"""Shared fixtures: an app on a throwaway SQLite database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_env
from app.main import create_app


@pytest.fixture
def env_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data = tmp_path / "data"
    monkeypatch.setenv("TA_DB_PATH", str(data / "agent.db"))
    monkeypatch.setenv("TA_DATA_DIR", str(data))
    get_env.cache_clear()
    yield data
    get_env.cache_clear()


@pytest.fixture
def client(env_paths: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c
