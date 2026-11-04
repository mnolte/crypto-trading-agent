"""The log store: queue, writer thread, ``logs.db``, the fallback file.

A log call puts the record on the in-memory queue and returns. One
writer thread commits the queue in batches every second or every 500
lines; an ERROR or CRITICAL line commits at once with the lines before
it. When ``logs.db`` can't be written, lines go to the fallback file
and to stderr, the writer retries every 30 s, and once it can write
again it imports the fallback file and renames it ``.imported``. The
writer's own failures never go through the logger — a broken store
can't loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.logstore import schema
from app.logstore.queue import LogQueue
from app.logstore.record import COLUMNS
from app.logstore.scrub import scrub_text

BATCH_SIZE = 500
FLUSH_S = 1.0
RETRY_S = 30.0
DROPPED_REPORT_S = 60.0
RETENTION_BATCH = 5000
IMPORTED_KEEP_DAYS = 30
PROBLEM_MAX_PAIRS = 5

_INSERT = (
    f"INSERT INTO log_lines ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)})"
)


def _fallback_name(day: datetime) -> str:
    return f"fallback-{day:%Y-%m-%d}.jsonl"


class LogStore:
    """Owns the queue, the writer thread and the fallback file."""

    def __init__(
        self,
        data_dir: Path,
        *,
        process: str = "app",
        queue_size: int = 20000,
        retry_s: float = RETRY_S,
        min_level: int = logging.DEBUG,
        clock: Callable[[], datetime] | None = None,
        version: str = "",
    ) -> None:
        self.log_dir = data_dir / "logs"
        self.db_path = self.log_dir / "logs.db"
        self.process = process
        self.version = version
        self.min_level = min_level
        self.retry_s = retry_s
        self.queue = LogQueue(queue_size)
        self.clock = clock or (lambda: datetime.now(UTC))
        # Called from the writer thread for every WARNING-or-higher line
        # after a batch commits — the app wires it to the WS "logs" topic.
        self.on_line: Callable[[dict[str, Any]], None] | None = None
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()  # conn is writer-only, but close() races
        self._fb_lock = threading.Lock()
        self._broken = False
        self._next_probe = 0.0
        self._last_dropped_report = time.monotonic()
        self._writer = threading.Thread(target=self._run, name="log-writer", daemon=True)
        self._started = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """First thing up at startup: directory, schema, fallback import,
        thread. An unwritable logs.db marks the store broken instead of
        stopping startup — the fallback file covers the gap."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._ensure_conn()
        except sqlite3.Error as exc:
            self._broken = True
            self._next_probe = time.monotonic() + self.retry_s
            self._stderr(f"logs.store_failed: logs.db not writable ({exc}); fallback file active")
        self._started = True
        self._writer.start()
        try:
            self._import_fallbacks()
        except (sqlite3.Error, OSError) as exc:
            self._stderr(f"logs: startup fallback import failed: {exc}")

    def stop(self, timeout: float = 10.0) -> None:
        """Last thing down: drain the queue, commit, close. Never raises."""
        if not self._started:
            return
        self.queue.close()
        self._writer.join(timeout)
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
        self._started = False

    def configure(
        self,
        *,
        queue_size: int | None = None,
        min_level: int | None = None,
    ) -> None:
        if queue_size is not None:
            self.queue.configure(queue_size)
        if min_level is not None:
            self.min_level = min_level

    # ----------------------------------------------------------------- write

    def submit(self, line: dict[str, Any]) -> str:
        """One prepared row onto the queue; returns the outcome."""
        if int(line["levelno"]) < self.min_level:
            return "dropped"
        # Scrub again at the boundary — cheap, idempotent, and covers any
        # path that built a line without the handler's filter.
        for key in ("message", "exc_message", "traceback", "data"):
            if line.get(key):
                line[key] = scrub_text(str(line[key]))
        outcome = self.queue.put(line)
        if outcome == "fallback":
            self.write_fallback(line)
        return outcome

    def write_fallback(self, line: dict[str, Any]) -> None:
        """Append one row to today's fallback file — direct write, no
        logger involved."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            target = self.log_dir / _fallback_name(self.clock())
            with self._fb_lock, target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")
        except OSError as exc:
            self._stderr(f"logs: fallback file write failed: {exc}")

    def _stderr(self, text: str) -> None:
        try:
            sys.stderr.write(text + "\n")
        except OSError:
            pass

    def _run(self) -> None:
        while True:
            batch = self.queue.drain(BATCH_SIZE, FLUSH_S)
            if not batch and self.queue.closed:
                break
            if batch:
                self._write(batch)
            elif self._broken and time.monotonic() >= self._next_probe:
                # Retry even with no new lines, so recovery and the
                # fallback import don't wait for the next event.
                self._probe()
            self._report_dropped()
            if self.queue.closed and len(self.queue) == 0 and not batch:
                break
        self._report_dropped()

    def _write(self, batch: list[dict[str, Any]]) -> None:
        if self._broken:
            for line in batch:
                self._to_fallback_now(line)
            if time.monotonic() >= self._next_probe:
                self._probe()
            return
        try:
            with self._conn_lock:
                conn = self._ensure_conn()
                self._insert_batch(conn, batch)
            if self.on_line is not None:
                for line in batch:
                    if int(line.get("levelno") or 0) >= logging.WARNING:
                        try:
                            self.on_line(line)
                        except Exception:  # noqa: BLE001, S110 - a push must never kill the writer
                            pass
        except sqlite3.Error as exc:
            self._broken = True
            self._next_probe = time.monotonic() + self.retry_s
            self._stderr(f"logs.store_failed: logs.db not writable ({exc}); fallback file active")
            for line in batch:
                self._to_fallback_now(line)

    def _to_fallback_now(self, line: dict[str, Any]) -> None:
        self.write_fallback(line)
        self._stderr(
            f"{line.get('ts', '')} {line.get('level', '?')} "
            f"{line.get('logger', '?')} {line.get('message', '')}"
        )

    def _probe(self) -> None:
        """Try a write transaction; on success import the fallback files."""
        self._next_probe = time.monotonic() + self.retry_s
        try:
            with self._conn_lock:
                conn = self._ensure_conn()
                conn.rollback()  # clear any implicit transaction a failed batch left
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
        except sqlite3.Error as exc:
            self._stderr(f"logs: logs.db still not writable ({exc})")
            return
        self._broken = False
        self._stderr("logs: logs.db writable again; importing fallback files")
        try:
            self._import_fallbacks()
        except (sqlite3.Error, OSError) as exc:
            self._stderr(f"logs: fallback import failed: {exc}")

    def _report_dropped(self) -> None:
        """Dropped counts, once a minute, as a WARNING through the logger."""
        now = time.monotonic()
        if now - self._last_dropped_report < DROPPED_REPORT_S:
            return
        counts = dict(self.queue.dropped)
        if not any(counts.values()):
            self._last_dropped_report = now
            return
        self.queue.dropped["DEBUG"] = 0
        self.queue.dropped["INFO"] = 0
        self._last_dropped_report = now
        logging.getLogger("app.logstore").warning(
            "log queue full: dropped %d DEBUG, %d INFO lines (WARNING and up are never dropped)",
            counts["DEBUG"],
            counts["INFO"],
            extra={"event": "logs.dropped", "data": counts},
        )

    # --------------------------------------------------------------- database

    def _ensure_conn(self) -> sqlite3.Connection:
        """The writer's connection — may be opened on the main thread at
        start() and used by the writer after, so check_same_thread is off;
        every use sits under ``_conn_lock``."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            schema.migrate(conn)
            self._conn = conn
        return self._conn

    def _insert_batch(self, conn: sqlite3.Connection, batch: list[dict[str, Any]]) -> None:
        with conn:
            for line in batch:
                conn.execute(_INSERT, line)
                if line.get("fingerprint") is not None:
                    self._update_problem(conn, line)

    def _update_problem(self, conn: sqlite3.Connection, line: dict[str, Any]) -> None:
        fp = line["fingerprint"]
        row = conn.execute(
            "SELECT level, levelno, count, pairs, state, note, reviewed_at "
            "FROM log_problems WHERE fingerprint = ?",
            (fp,),
        ).fetchone()
        ts = str(line["ts"])
        pair = line.get("pair")
        if row is None:
            conn.execute(
                "INSERT INTO log_problems "
                "(fingerprint, level, levelno, logger, event, example, count, "
                " first_seen, last_seen, pairs, state) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'unreviewed')",
                (
                    fp,
                    line["level"],
                    line["levelno"],
                    line["logger"],
                    line.get("event"),
                    line["message"],
                    ts,
                    ts,
                    json.dumps([pair] if pair else []),
                ),
            )
            return
        level, levelno, count, pairs_raw, state, _note, reviewed_at = row
        pairs = list(json.loads(pairs_raw))
        if pair and pair not in pairs and len(pairs) < PROBLEM_MAX_PAIRS:
            pairs.append(pair)
        new_levelno = max(int(levelno), int(line["levelno"]))
        new_level = str(line["level"]) if int(line["levelno"]) > int(levelno) else str(level)
        if state == "fixed":
            state = "unreviewed"
            note = "came back after it was marked fixed"
        elif state == "expected" and int(line["levelno"]) > int(levelno):
            state = "unreviewed"
            note = "came back at a higher level after it was marked expected"
        else:
            note = _note
        conn.execute(
            "UPDATE log_problems SET level = ?, levelno = ?, count = ?, "
            "last_seen = ?, pairs = ?, state = ?, note = ?, reviewed_at = ? "
            "WHERE fingerprint = ?",
            (
                new_level,
                new_levelno,
                int(count) + 1,
                ts,
                json.dumps(pairs),
                state,
                note,
                reviewed_at,
                fp,
            ),
        )

    # --------------------------------------------------------------- fallback

    def _import_fallbacks(self) -> None:
        """Import every ``fallback-*.jsonl`` once logs.db can be written."""
        files = sorted(self.log_dir.glob("fallback-*.jsonl"))
        if not files:
            return
        with self._conn_lock:
            conn = self._ensure_conn()
            for path in files:
                with conn:
                    with path.open(encoding="utf-8") as fh:
                        for raw in fh:
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                line = json.loads(raw)
                            except json.JSONDecodeError:
                                self._stderr(f"logs: unparseable fallback line skipped: {raw[:80]}")
                                continue
                            conn.execute(_INSERT, {c: line.get(c) for c in COLUMNS})
                            if line.get("fingerprint") is not None:
                                self._update_problem(conn, line)
                path.rename(path.with_suffix(".imported"))

    # --------------------------------------------------------------- queries

    def write_lines(self, lines: list[dict[str, Any]]) -> None:
        """Insert lines synchronously — for the one-time legacy import,
        which must see its problems before it reviews them."""
        with self._conn_lock:
            try:
                conn = self._ensure_conn()
                self._insert_batch(conn, lines)
            except sqlite3.Error as exc:
                self._stderr(f"logs: legacy import write failed ({exc})")

    def meta_get(self, key: str) -> str | None:
        with self._conn_lock:
            try:
                return schema.meta_get(self._ensure_conn(), key)
            except sqlite3.Error:
                return None

    def meta_set(self, key: str, value: str) -> None:
        with self._conn_lock:
            try:
                conn = self._ensure_conn()
                with conn:
                    schema.meta_set(conn, key, value)
            except sqlite3.Error:
                self._stderr(f"logs: meta write {key} failed")

    def unreviewed_problems(self, min_levelno: int = logging.WARNING) -> list[dict[str, Any]]:
        """Problems nobody reviewed yet — the Needs-you and readiness
        inputs now that the errors table is retired."""
        try:
            with self.connect_readonly() as conn:
                rows = list(
                    conn.execute(
                        "SELECT * FROM log_problems WHERE state = 'unreviewed' "
                        "AND levelno >= ? ORDER BY last_seen DESC",
                        (min_levelno,),
                    )
                )
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            p = dict(r)
            p["pairs"] = json.loads(p.get("pairs") or "[]")
            out.append(p)
        return out

    def count_unreviewed(self, min_levelno: int = logging.ERROR) -> int:
        return self.count_unreviewed_since(None, min_levelno)

    def count_unreviewed_since(self, since: str | None, min_levelno: int = logging.ERROR) -> int:
        try:
            with self.connect_readonly() as conn:
                where = "state = 'unreviewed' AND levelno >= ?"
                params: list[Any] = [min_levelno]
                if since is not None:
                    where += " AND last_seen >= ?"
                    params.append(since)
                return int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM log_problems WHERE {where}",
                        params,
                    ).fetchone()[0]
                )
        except sqlite3.Error:
            return 0

    def connect_readonly(self) -> sqlite3.Connection:
        """A read-only connection for the API — WAL lets it read while the
        writer commits."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def review_problem(self, fingerprint: str, state: str, note: str, now: datetime) -> bool:
        """Mark a problem expected or fixed; a note is required."""
        if state not in ("expected", "fixed"):
            raise ValueError(f"state must be expected or fixed, got {state!r}")
        if not note.strip():
            raise ValueError("a review needs a note")
        with self._conn_lock:
            conn = self._ensure_conn()
            with conn:
                row = conn.execute(
                    "SELECT count FROM log_problems WHERE fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                if row is None:
                    return False
                conn.execute(
                    "UPDATE log_problems SET state = ?, note = ?, reviewed_at = ?, "
                    "count_at_review = ? WHERE fingerprint = ?",
                    (state, note, now.isoformat(), int(row[0]), fingerprint),
                )
        return True

    # --------------------------------------------------------------- retention

    def run_retention(
        self,
        keep_days: dict[str, int],
        max_db_mb: float,
        now: datetime,
    ) -> dict[str, int]:
        """Daily retention: per-level age limits in batches, the size cap
        (oldest DEBUG first, then INFO), then an incremental vacuum and
        removal of ``.imported`` files older than 30 days. Problems stay."""
        removed: dict[str, int] = {}
        with self._conn_lock:
            conn = self._ensure_conn()
            with conn:
                for level, days in keep_days.items():
                    cutoff = (now - timedelta(days=days)).isoformat()
                    total = 0
                    while True:
                        cur = conn.execute(
                            "DELETE FROM log_lines WHERE id IN ("
                            "  SELECT id FROM log_lines WHERE level = ? AND ts < ? LIMIT ?"
                            ")",
                            (level, cutoff, RETENTION_BATCH),
                        )
                        total += cur.rowcount
                        if cur.rowcount < RETENTION_BATCH:
                            break
                    removed[level] = total
            cap_bytes = int(max_db_mb * 1024 * 1024)
            for level in ("DEBUG", "INFO"):
                while self._file_size() > cap_bytes:
                    with conn:
                        cur = conn.execute(
                            "DELETE FROM log_lines WHERE id IN ("
                            "  SELECT id FROM log_lines WHERE level = ? ORDER BY id LIMIT ?"
                            ")",
                            (level, RETENTION_BATCH),
                        )
                    if cur.rowcount == 0:
                        break
                    removed[level] = removed.get(level, 0) + cur.rowcount
                    # Pages only return to the OS after a checkpoint and
                    # vacuum — otherwise every level would be emptied.
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("PRAGMA incremental_vacuum")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA incremental_vacuum")
        imported_cutoff = now - timedelta(days=IMPORTED_KEEP_DAYS)
        for path in self.log_dir.glob("fallback-*.imported"):
            try:
                day = datetime.strptime(path.stem, "fallback-%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day < imported_cutoff:
                path.unlink()
        return removed

    def _file_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            if path.exists():
                total += path.stat().st_size
        return total
