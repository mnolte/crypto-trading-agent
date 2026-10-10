"""Environment configuration and secrets loading.

Runtime placement (host, port, database path, data directory) comes from
environment variables or a git-ignored ``.env`` file, so the same build can run
on a VPS later without code changes. Secrets (API keys, the SMTP password) are
read from the OS keyring first, falling back to a ``*_API_KEY`` variable in the
environment or ``.env``. Secrets never land in the database or in logs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import keyring
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
ENV_FILE = REPO_ROOT / ".env"
KEYRING_SERVICE = "trading-agent"

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class EnvConfig(BaseSettings):
    """Where the app runs. Field names map to TA_* environment variables."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    ta_host: str = "127.0.0.1"
    ta_port: int = 8000
    ta_db_path: Path = REPO_ROOT / "data" / "agent.db"
    ta_data_dir: Path = REPO_ROOT / "data"


@lru_cache
def get_env() -> EnvConfig:
    return EnvConfig()


def _env_file_value(name: str) -> str | None:
    """Read a single variable from .env without exporting it to os.environ."""
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'") or None
    return None


def get_secret(source: str) -> str | None:
    """Return the API key for a source, or None.

    Order: OS keyring, then a ``{SOURCE}_API_KEY`` environment variable, then the
    same variable in ``.env``.
    """
    stored = keyring.get_password(KEYRING_SERVICE, source)
    if stored:
        return stored
    name = f"{source.upper().replace('-', '_')}_API_KEY"
    return os.environ.get(name) or _env_file_value(name)


def set_secret(source: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, source, value)


def delete_secret(source: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, source)
    except keyring.errors.PasswordDeleteError:
        pass


def secret_status(source: str) -> dict[str, object]:
    """Only ever expose whether a key is set and its last 4 characters."""
    value = get_secret(source)
    return {"set": value is not None, "last4": value[-4:] if value else None}
