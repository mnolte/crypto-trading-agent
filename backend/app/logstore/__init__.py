"""The log store: ``data/logs/logs.db`` plus console and a fallback file.

``install`` is the first thing the app starts and ``shutdown`` the last
it stops. Callers log through ``logging`` as usual — the store handler
adds the trace context, scrubs secrets, stamps fingerprints and error
ids, and the writer thread owns the database.
"""

from __future__ import annotations

import json
import logging
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.logstore.context import CONTEXT_FIELDS, current, log_context
from app.logstore.handler import ConsoleFormatter, StoreHandler, make_library_filter
from app.logstore.record import fingerprint, new_error_id
from app.logstore.scrub import register_secret
from app.logstore.writer import LogStore

__all__ = [
    "CONTEXT_FIELDS",
    "LogStore",
    "configure",
    "current",
    "get_store",
    "install",
    "log_context",
    "new_error_id",
    "register_secret",
    "shutdown",
]

_store: LogStore | None = None
_handler: StoreHandler | None = None
_library_filter = None
_console: logging.StreamHandler[Any] | None = None
_VERSION = ""


def _app_version() -> str:
    """App version + git short commit, e.g. ``0.1.0+aa258e9``."""
    global _VERSION
    if _VERSION:
        return _VERSION
    version = "0.1.0"
    try:
        import subprocess

        from app.config import REPO_ROOT

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        commit = ""
    _VERSION = f"{version}+{commit}" if commit else version
    return _VERSION


def install(data_dir: Path, *, process: str = "app") -> LogStore:
    """Start the writer before anything else; call exactly once."""
    global _store, _handler, _library_filter, _console
    if _store is not None:
        return _store
    store = LogStore(data_dir, process=process, version=_app_version())
    store.start()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    _handler = StoreHandler(store)
    _library_filter = make_library_filter()
    _handler.addFilter(_library_filter)
    root.addHandler(_handler)
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    console.setLevel(logging.INFO)
    root.addHandler(console)
    _console = console
    # Python warnings and library loggers share the same store.
    logging.captureWarnings(True)
    warnings.simplefilter("default")
    for noisy in ("uvicorn.access", "httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _store = store
    return store


def get_store() -> LogStore | None:
    return _store


def import_legacy_errors(store: LogStore, rows: list[Any]) -> int:
    """One-time import of the retired errors table: every row becomes a
    ``legacy.error`` line and a problem — resolved rows come in fixed,
    the rest unreviewed. ``meta['errors_legacy_imported']`` makes a
    second startup a no-op."""
    if store.meta_get("errors_legacy_imported") == "1":
        return 0
    count = 0
    lines = []
    for row in rows:
        first = (
            row.first_at.isoformat()
            if hasattr(row.first_at, "isoformat")
            else str(row.first_at)
        )
        resolved = getattr(row, "resolved_at", None) is not None
        line = {
            "ts": first,
            "level": "ERROR",
            "levelno": 40,
            "logger": "app",
            "event": "legacy.error",
            "message": row.text,
            "process": "app",
            "pid": None,
            "task": None,
            "cycle_id": None,
            "job": None,
            "pair": getattr(row, "pair", None),
            "decision_id": None,
            "trade_id": None,
            "order_id": None,
            "shadow_id": None,
            "backtest_id": None,
            "request_id": None,
            "duration_ms": None,
            "error_id": None,
            "exc_type": None,
            "exc_message": None,
            "traceback": None,
            "data": json.dumps(
                {
                    "legacy_id": row.id,
                    "affected_decision_or_fill": bool(
                        getattr(row, "affected_decision_or_fill", False)
                    ),
                    "last_at": str(getattr(row, "last_at", "")),
                }
            ),
            "fingerprint": f"legacy.error|{fingerprint('app', 'legacy.error', row.text)}",
            "version": None,
        }
        lines.append((line, resolved, row))
        count += 1
    # Synchronous insert: the problem rows must exist before resolved
    # rows are marked fixed.
    store.write_lines([line for line, _r, _row in lines])
    for line, resolved, row in lines:
        if resolved:
            store.review_problem(
                line["fingerprint"] or "",
                "fixed",
                f"legacy resolution: {getattr(row, 'resolution_note', '') or 'resolved'}",
                row.resolved_at
                if hasattr(row.resolved_at, "isoformat")
                else datetime.now(UTC),
            )
    store.meta_set("errors_legacy_imported", "1")
    return count


def unreviewed_problems(min_levelno: int = logging.ERROR) -> list[dict[str, Any]]:
    """Unreviewed problems from the installed store (empty when it is
    not installed or not readable)."""
    store = _store
    if store is None:
        return []
    return store.unreviewed_problems(min_levelno)


def unreviewed_error_count(min_levelno: int = logging.ERROR) -> int:
    store = _store
    if store is None:
        return 0
    return store.count_unreviewed(min_levelno)


def unreviewed_error_count_since(
    since: str | None, min_levelno: int = logging.ERROR
) -> int:
    store = _store
    if store is None:
        return 0
    return store.count_unreviewed_since(since, min_levelno)


def shutdown() -> None:
    """Flush and close the writer — the last step of a clean shutdown."""
    global _store, _handler, _console
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler = None
    if _console is not None:
        root.removeHandler(_console)
        _console = None
    if _store is not None:
        _store.stop()
        _store = None


def configure(
    *,
    console_level: str | None = None,
    queue_size: int | None = None,
    library_debug: bool | None = None,
    backtest_debug: bool = False,
) -> None:
    """Apply ``settings.logging`` once settings are loaded."""
    if _console is not None and console_level is not None:
        _console.setLevel(getattr(logging, console_level, logging.INFO))
    if _library_filter is not None and library_debug is not None:
        _library_filter.library_debug = library_debug
    if _store is not None:
        _store.configure(queue_size=queue_size)
        if backtest_debug and _store.process.startswith("backtest-"):
            _store.configure(min_level=logging.DEBUG)
