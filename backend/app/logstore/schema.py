"""The ``logs.db`` schema — its own small module, not Alembic.

``logs.db`` is a store of its own next to the trading database: it is
never in the 03:00 backup and Alembic never manages it. ``meta`` carries
the schema version and one-shot flags (for example the errors_legacy
import); ``migrate`` applies the steps in order.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    levelno INTEGER NOT NULL,
    logger TEXT NOT NULL,
    event TEXT,
    message TEXT NOT NULL,
    process TEXT,
    pid INTEGER,
    task TEXT,
    cycle_id TEXT,
    job TEXT,
    pair TEXT,
    decision_id TEXT,
    trade_id TEXT,
    order_id TEXT,
    shadow_id TEXT,
    backtest_id TEXT,
    request_id TEXT,
    duration_ms REAL,
    error_id TEXT,
    exc_type TEXT,
    exc_message TEXT,
    traceback TEXT,
    data TEXT,
    fingerprint TEXT,
    version TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(
    message, event, exc_type, exc_message, data,
    content='log_lines', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS log_lines_ai AFTER INSERT ON log_lines BEGIN
    INSERT INTO log_fts(rowid, message, event, exc_type, exc_message, data)
    VALUES (new.id, new.message, new.event, new.exc_type, new.exc_message, new.data);
END;
CREATE TRIGGER IF NOT EXISTS log_lines_ad AFTER DELETE ON log_lines BEGIN
    INSERT INTO log_fts(log_fts, rowid, message, event, exc_type, exc_message, data)
    VALUES ('delete', old.id, old.message, old.event, old.exc_type, old.exc_message, old.data);
END;

CREATE INDEX IF NOT EXISTS ix_log_ts ON log_lines (ts);
CREATE INDEX IF NOT EXISTS ix_log_levelno_ts ON log_lines (levelno, ts);
CREATE INDEX IF NOT EXISTS ix_log_logger_ts ON log_lines (logger, ts);
CREATE INDEX IF NOT EXISTS ix_log_event_ts ON log_lines (event, ts);
CREATE INDEX IF NOT EXISTS ix_log_cycle ON log_lines (cycle_id);
CREATE INDEX IF NOT EXISTS ix_log_pair_ts ON log_lines (pair, ts);
CREATE INDEX IF NOT EXISTS ix_log_fingerprint_ts ON log_lines (fingerprint, ts);
CREATE INDEX IF NOT EXISTS ix_log_error_id ON log_lines (error_id);
CREATE INDEX IF NOT EXISTS ix_log_request_id ON log_lines (request_id);

CREATE TABLE IF NOT EXISTS log_problems (
    fingerprint TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    levelno INTEGER NOT NULL,
    logger TEXT NOT NULL,
    event TEXT,
    example TEXT NOT NULL,
    count INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    pairs TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'unreviewed',
    note TEXT,
    reviewed_at TEXT,
    count_at_review INTEGER
);

CREATE INDEX IF NOT EXISTS ix_problems_state ON log_problems (state);
CREATE INDEX IF NOT EXISTS ix_problems_last_seen ON log_problems (last_seen);
"""


def _version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    """Apply schema steps the database hasn't seen yet."""
    version = _version(conn)
    if version < 1:
        conn.executescript(_V1)
    if version != SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
