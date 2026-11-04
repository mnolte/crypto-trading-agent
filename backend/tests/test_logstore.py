"""The log store: queueing, batches, fallback, retention, scrubbing."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.logstore import scrub
from app.logstore.record import COLUMNS, fingerprint
from app.logstore.writer import LogStore


def make_line(**kw: Any) -> dict[str, Any]:
    line: dict[str, Any] = dict.fromkeys(COLUMNS)
    line.update(
        {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": "INFO",
            "levelno": 20,
            "logger": "agent.scheduler",
            "event": "cycle.end",
            "message": "Cycle c-0001 ended",
            "process": "app",
            "pid": os.getpid(),
            "task": "cycle",
            "version": "0.1.0+test",
        }
    )
    line.update(kw)
    return line


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[LogStore]:
    s = LogStore(tmp_path / "data", retry_s=0.05)
    s.start()
    yield s
    s.stop()


def wait_for(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting")


def rows(store: LogStore, where: str = "1=1") -> list[sqlite3.Row]:
    with store.connect_readonly() as conn:
        return list(conn.execute(f"SELECT * FROM log_lines WHERE {where}"))


def test_lines_land_with_every_column(store: LogStore) -> None:
    store.submit(
        make_line(
            level="ERROR",
            levelno=40,
            event="model.call_failed",
            message="BTC-EUR: decision call timed out",
            cycle_id="c-0716",
            pair="BTC-EUR",
            request_id="r-123",
            error_id="e-3f9a21c0",
            duration_ms=60018.0,
            exc_type="app.errors.ModelError",
            exc_message="timeout after 2 attempts",
            traceback="Traceback …",
            data='{"attempts":2}',
            fingerprint="agent.decider|model.call_failed|<v>",
        )
    )
    wait_for(lambda: rows(store))
    row = dict(rows(store)[0])
    for col in COLUMNS:
        assert col in row
    assert row["level"] == "ERROR"
    assert row["cycle_id"] == "c-0716"
    assert row["pair"] == "BTC-EUR"
    assert row["error_id"] == "e-3f9a21c0"
    assert row["duration_ms"] == 60018.0
    assert row["exc_type"] == "app.errors.ModelError"
    assert row["version"] == "0.1.0+test"


def test_fts_finds_words_in_message_and_exc(store: LogStore) -> None:
    store.submit(make_line(message="BTC-EUR: newest candle missing"))
    store.submit(
        make_line(message="unrelated", exc_message="zerodivision in sizing math")
    )
    wait_for(lambda: len(rows(store)) == 2)
    with store.connect_readonly() as conn:
        hits = list(
            conn.execute(
                "SELECT l.message FROM log_fts f JOIN log_lines l ON l.id = f.rowid "
                "WHERE log_fts MATCH ?",
                ("missing",),
            )
        )
        assert len(hits) == 1 and "newest candle" in hits[0][0]
        hits = list(
            conn.execute(
                "SELECT l.exc_message FROM log_fts f JOIN log_lines l ON l.id = f.rowid "
                "WHERE log_fts MATCH ?",
                ("zerodivision",),
            )
        )
        assert len(hits) == 1


def test_batches_commit_quickly_and_error_at_once(store: LogStore) -> None:
    store.submit(make_line(level="DEBUG", levelno=10, message="d1"))
    t0 = time.monotonic()
    wait_for(lambda: len(rows(store)) == 1, timeout=1.0)
    assert time.monotonic() - t0 < 1.0
    store.submit(make_line(level="ERROR", levelno=40, message="boom"))
    wait_for(lambda: rows(store, "level='ERROR'"), timeout=1.0)


def test_queue_drops_debug_before_info_never_warning(store: LogStore) -> None:
    queue = store.queue
    queue.configure(4)
    filler = make_line(level="DEBUG", levelno=10)
    for _ in range(4):
        assert queue.put(dict(filler)) == "queued"
    # Full of DEBUG: a DEBUG drops, an INFO takes a DEBUG's slot.
    assert queue.put(dict(filler)) == "dropped"
    assert queue.dropped["DEBUG"] >= 1
    assert queue.put(make_line(level="INFO", levelno=20)) == "queued"
    # Fill the rest: an INFO with no DEBUG left drops itself.
    for _ in range(3):
        queue.put(make_line(level="INFO", levelno=20))
    assert queue.put(make_line(level="INFO", levelno=20)) == "dropped"
    # WARNING and up are never dropped — they take a shallower slot.
    warn = make_line(level="WARNING", levelno=30)
    assert queue.put(dict(warn)) == "queued"
    assert queue.put(make_line(level="CRITICAL", levelno=50)) == "queued"


def test_unwritable_db_uses_fallback_then_imports(store: LogStore) -> None:
    db = store.db_path
    wait_for(lambda: db.exists())
    # The writer's open fd keeps write access after a chmod; close it so
    # the next write re-opens the file and hits the read-only mode.
    with store._conn_lock:
        if store._conn is not None:
            store._conn.close()
            store._conn = None
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        if path.exists():
            path.chmod(0o444)
    store.submit(make_line(level="ERROR", levelno=40, message="during outage"))
    wait_for(lambda: store._broken, timeout=3.0)
    store.submit(make_line(level="INFO", levelno=20, message="while broken"))
    wait_for(
        lambda: any(store.log_dir.glob("fallback-*.jsonl")), timeout=3.0
    )
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        if path.exists():
            path.chmod(0o644)
    wait_for(lambda: not store._broken, timeout=5.0)
    wait_for(
        lambda: len(rows(store, "message LIKE '%outage%' OR message LIKE '%broken%'")) == 2,
        timeout=5.0,
    )
    assert not list(store.log_dir.glob("fallback-*.jsonl"))
    assert list(store.log_dir.glob("fallback-*.imported"))


def test_retention_by_level_and_age(store: LogStore) -> None:
    now = datetime.now(UTC)
    for level, days in (
        ("DEBUG", 40), ("INFO", 40), ("WARNING", 400), ("WARNING", 100), ("ERROR", 300)
    ):
        store.submit(
            make_line(
                ts=(now - timedelta(days=days)).isoformat(),
                level=level,
                levelno=logging.getLevelName(level),
                message=f"old {level} {days}d",
            )
        )
    wait_for(lambda: len(rows(store)) == 5)
    keep = {"DEBUG": 7, "INFO": 30, "WARNING": 180, "ERROR": 365, "CRITICAL": 365}
    removed = store.run_retention(keep, max_db_mb=2000, now=now)
    wait_for(lambda: len(rows(store)) == 2)
    # 40-day debug/info and the 400-day warning are gone; the 100-day
    # warning and 300-day error stay within their keep windows.
    assert {r["level"] for r in rows(store)} == {"WARNING", "ERROR"}
    assert removed["DEBUG"] == 1 and removed["INFO"] == 1 and removed["WARNING"] == 1


def test_size_cap_drops_debug_first(store: LogStore) -> None:
    blob = json.dumps({"pad": "x" * 2800})
    for i in range(40):
        store.submit(make_line(level="DEBUG", levelno=10, message=f"debug {i}", data=blob))
        store.submit(make_line(level="INFO", levelno=20, message=f"info {i}", data=blob))
        store.submit(
            make_line(level="WARNING", levelno=30, message=f"warn {i}", data=blob)
        )
    wait_for(lambda: len(rows(store)) == 120)
    # ~0.2 MB cap forces the cap path; DEBUG goes first, then INFO,
    # until the file is under the limit. WARNING and up keep their age.
    store.run_retention(
        {"DEBUG": 9999, "INFO": 9999, "WARNING": 9999, "ERROR": 9999},
        max_db_mb=0.2,
        now=datetime.now(UTC),
    )
    wait_for(lambda: not rows(store, "level='DEBUG'"))
    assert len(rows(store, "level='INFO'")) < 40
    assert len(rows(store, "level='WARNING'")) == 40


def test_fingerprint_groups_recurrences() -> None:
    a = fingerprint(
        "market.bitvavo", "candles.stale", "BTC-EUR: newest candle retry 1 of 3 in 30 s"
    )
    b = fingerprint(
        "market.bitvavo", "candles.stale", "ETH-EUR: newest candle retry 2 of 3 in 31 s"
    )
    c = fingerprint("market.bitvavo", "candles.stale", "different message altogether")
    assert a == b != c


def test_problem_grouping_and_review(store: LogStore) -> None:
    for pair, msg in (
        ("BTC-EUR", "BTC-EUR: newest 1h candle (12:00) not there after 30 s"),
        ("ETH-EUR", "ETH-EUR: newest 1h candle (12:00) not there after 31 s"),
    ):
        store.submit(
            make_line(
                level="WARNING",
                levelno=30,
                pair=pair,
                message=msg,
                fingerprint=fingerprint("agent.scheduler", "candles.stale", msg),
            )
        )
    wait_for(lambda: len(rows(store)) == 2)
    with store.connect_readonly() as conn:
        probs = list(conn.execute("SELECT * FROM log_problems"))
        assert len(probs) == 1
        prob = dict(probs[0])
        assert prob["count"] == 2
        assert prob["state"] == "unreviewed"
        assert set(json.loads(prob["pairs"])) == {"BTC-EUR", "ETH-EUR"}
        fp = prob["fingerprint"]
    with pytest.raises(ValueError, match="note"):
        store.review_problem(fp, "fixed", "", datetime.now(UTC))
    assert store.review_problem(fp, "fixed", "cause gone", datetime.now(UTC))
    # A fixed problem that comes back is unreviewed again.
    msg = "BTC-EUR: newest 1h candle (12:00) not there after 33 s"
    store.submit(
        make_line(
            level="WARNING",
            levelno=30,
            pair="BTC-EUR",
            message=msg,
            fingerprint=fingerprint("agent.scheduler", "candles.stale", msg),
        )
    )
    wait_for(lambda: dict(rows(store)[0])["fingerprint"] is not None)
    with store.connect_readonly() as conn:
        prob = dict(
            conn.execute("SELECT * FROM log_problems WHERE fingerprint = ?", (fp,)).fetchone()
        )
        assert prob["state"] == "unreviewed"
        assert "came back" in prob["note"]
        assert prob["count"] == 3


def test_scrubbing_covers_stored_keys_and_headers(tmp_path: Path) -> None:
    store = LogStore(tmp_path / "data")
    store.start()
    try:
        scrub.register_secret("sk-livekey-abcdef123456")
        scrub.register_secret("smtp-passw0rd!")
        secret_msg = "key sk-livekey-abcdef123456 and pass smtp-passw0rd!"
        store.submit(
            make_line(
                level="ERROR", levelno=40, message=secret_msg, exc_message="smtp-passw0rd!"
            )
        )
        store.submit(
            make_line(
                level="WARNING",
                levelno=30,
                message="Authorization: Bearer eyJabcdef123456.token.here now",
                data='{"headers":{"Cookie":"session=abc123"},"api_key":"hunter2hunter2"}',
            )
        )
        store.submit(make_line(message="force fallback"))
        wait_for(lambda: len(rows(store)) >= 2)
        with store.connect_readonly() as conn:
            all_text = " ".join(
                " ".join(str(r[c]) for c in ("message", "exc_message", "data", "traceback") if r[c])
                for r in conn.execute("SELECT * FROM log_lines")
            )
        assert "sk-livekey-abcdef123456" not in all_text
        assert "smtp-passw0rd" not in all_text
        assert "eyJabcdef123456" not in all_text
        assert "session=abc123" not in all_text
        assert "hunter2hunter2" not in all_text
        assert "[redacted]" in all_text
    finally:
        store.stop()
        scrub.forget_secrets()
