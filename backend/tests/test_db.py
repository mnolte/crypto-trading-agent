"""Connection pragmas: WAL so readers never block the writer, and a real
busy timeout so concurrent writers wait instead of failing with
``database is locked``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import BUSY_TIMEOUT_MS, make_engine


def test_engine_applies_sqlite_pragmas(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "agent.db")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == BUSY_TIMEOUT_MS
            assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1
    finally:
        engine.dispose()


def test_wal_mode_persists_on_the_database_file(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    engine = make_engine(db_path)
    try:
        with engine.connect():
            pass
    finally:
        engine.dispose()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_uncommitted_write_does_not_block_reads(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "agent.db")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            conn.exec_driver_sql("INSERT INTO t (v) VALUES ('committed')")
        holding = engine.connect()
        holding.begin()
        holding.exec_driver_sql("INSERT INTO t (v) VALUES ('uncommitted')")
        try:
            with engine.connect() as reader:
                rows = reader.exec_driver_sql("SELECT v FROM t").all()
            assert rows == [("committed",)]
        finally:
            holding.close()
    finally:
        engine.dispose()
