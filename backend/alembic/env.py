"""Alembic environment. The database URL comes from the app settings
(TA_DB_PATH); ``-x db=...`` or a programmatically set ``sqlalchemy.url``
wins for scripts and tests. The value in alembic.ini is a placeholder."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    db = context.get_x_argument(as_dictionary=True).get("db")
    if db:
        return f"sqlite:///{db}"
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    from app.config import get_env

    return f"sqlite:///{get_env().ta_db_path}"


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": _url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
