"""Uvicorn placement options come from TA_HOST/TA_PORT and .env configuration."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import get_env
from app.serve import serve_options


@pytest.fixture(autouse=True)
def clear_env_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("TA_HOST", raising=False)
    monkeypatch.delenv("TA_PORT", raising=False)
    get_env.cache_clear()
    yield
    get_env.cache_clear()


def test_serve_options_default_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    get_env.cache_clear()
    options = serve_options(reload=False)
    assert options.host == "127.0.0.1"
    assert options.port == 8000
    assert options.reload is False


def test_serve_options_honor_ta_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TA_HOST", "0.0.0.0")
    monkeypatch.setenv("TA_PORT", "8123")
    get_env.cache_clear()
    options = serve_options(reload=True)
    assert options.host == "0.0.0.0"
    assert options.port == 8123
    assert options.reload is True
