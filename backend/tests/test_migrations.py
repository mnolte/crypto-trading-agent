"""Migrations run against the configured TA_DB_PATH, never a hard-coded path."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from app.config import get_env
from app.db import ALEMBIC_DIR, ALEMBIC_INI, run_migrations


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
    run_migrations(db_path)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        tables = sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()

    assert "settings" in tables
    assert _alembic_version(db_path) == "0001"


def test_alembic_cli_respects_ta_db_path(env_paths: Path) -> None:
    """A bare `alembic upgrade head` migrates TA_DB_PATH, not the ini placeholder."""
    db_path = get_env().ta_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")

    assert _alembic_version(db_path) == "0001"


def test_migrations_dont_touch_default_path(env_paths: Path) -> None:
    """No database lands at the repo's default data/ during tests."""
    from app.config import REPO_ROOT

    run_migrations(get_env().ta_db_path)
    assert get_env().ta_db_path.is_file()
    assert not (REPO_ROOT / "data" / "agent.db").exists()
