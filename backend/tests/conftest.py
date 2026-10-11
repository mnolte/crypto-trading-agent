"""Shared fixtures: an app on a throwaway SQLite database.

No real network (pytest-socket blocks it) and no real keyring — secrets live
in a per-test dict so they never touch the OS keychain or a developer's
``.env``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from fastapi.testclient import TestClient

import app.config
from app.agent.market.sim import SimMarket
from app.config import get_env
from app.main import create_app


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """In-memory keyring + no ambient *_API_KEY env vars or .env file."""
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "delete_password", lambda s, u: store.pop((s, u), None))
    for name in ("OPENAI_API_KEY", "BITVAVO_API_KEY", "COINGECKO_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app.config, "ENV_FILE", Path("/nonexistent/.env"))
    return store


@pytest.fixture
def env_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    data = tmp_path / "data"
    monkeypatch.setenv("TA_DB_PATH", str(data / "agent.db"))
    monkeypatch.setenv("TA_DATA_DIR", str(data))
    monkeypatch.setenv("TA_DATA_SOURCE", "sim")
    get_env.cache_clear()
    yield data
    get_env.cache_clear()


@pytest.fixture
def sim_market(monkeypatch: pytest.MonkeyPatch) -> SimMarket:
    """Patch the market-client factory so the app runs on the simulated
    market — no Bitvavo calls anywhere."""
    sim = SimMarket(seed=7)
    monkeypatch.setattr("app.api.deps.make_market_client", lambda settings: sim)
    return sim


@pytest.fixture
def client(env_paths: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def _no_refresh(_state: object) -> None:
        return None

    monkeypatch.setattr("app.main.refresh_market_data", _no_refresh)
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c


@pytest.fixture
def sim_client(
    env_paths: Path, sim_market: SimMarket, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    async def _no_refresh(_state: object) -> None:
        return None

    monkeypatch.setattr("app.main.refresh_market_data", _no_refresh)
    with TestClient(create_app(), base_url="http://127.0.0.1") as c:
        yield c
