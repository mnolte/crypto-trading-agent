"""Migrations run against the configured TA_DB_PATH, never a hard-coded path."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from app.config import get_env
from app.db import ALEMBIC_DIR, ALEMBIC_INI, run_migrations


def _head() -> str | None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _alembic_version(db_path: Path) -> str | None:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()


def test_migrations_use_ta_db_path(env_paths: Path) -> None:
    """The throwaway TA_DB_PATH gets the schema; alembic.ini's placeholder is ignored."""
    db_path = get_env().ta_db_path
    db_path.unlink(missing_ok=True)  # cover the fresh migrate, not the seeded copy
    run_migrations(db_path)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        tables = sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()

    assert "settings" in tables
    assert "candles" in tables
    assert "markets" in tables
    assert "positions" in tables
    assert "trades" in tables
    assert "decisions" in tables
    assert "llm_calls" in tables
    assert "market_context" in tables
    assert _alembic_version(db_path) == _head()


def test_alembic_cli_respects_ta_db_path(env_paths: Path) -> None:
    """A bare `alembic upgrade head` migrates TA_DB_PATH, not the ini placeholder."""
    db_path = get_env().ta_db_path
    db_path.unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")

    assert _alembic_version(db_path) == _head()


def test_migrations_dont_touch_default_path(env_paths: Path) -> None:
    """Migrations never create or modify the repo's default data/agent.db —
    whether or not a developer's local database happens to exist there."""
    from app.config import REPO_ROOT

    get_env().ta_db_path.unlink(missing_ok=True)
    default_db = REPO_ROOT / "data" / "agent.db"
    identity = (default_db.stat().st_ino, default_db.stat().st_dev) if default_db.exists() else None

    run_migrations(get_env().ta_db_path)

    assert get_env().ta_db_path.is_file()
    if identity is None:
        assert not default_db.exists()
    else:
        # A local dev database may be written by a running app during the
        # test, so mtime is unreliable; a delete+recreate would change the
        # inode, and that is what we can check deterministically.
        assert (default_db.stat().st_ino, default_db.stat().st_dev) == identity
