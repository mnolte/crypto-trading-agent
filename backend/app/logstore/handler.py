"""Logging handlers: the queueing store handler and the console line."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app import logevents
from app.logstore.record import build_line, component_name
from app.logstore.scrub import scrub_text
from app.logstore.writer import LogStore

# Library loggers (anything outside app.*) land at WARNING and up unless
# ``logging.library_debug`` is on — uvicorn, httpx, sqlalchemy stay quiet
# in the store while their errors still can't vanish.
_LIBRARY_PREFIXES = ("app", "frontend", "py.warnings")


class _LibraryFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.library_debug = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self.library_debug:
            return True
        name = record.name
        if any(name == p or name.startswith(p + ".") for p in _LIBRARY_PREFIXES):
            return True
        return record.levelno >= logging.WARNING


class StoreHandler(logging.Handler):
    """Puts every record on the store's queue and returns — never blocks
    on disk. Scrubbing and context stamping happen here, at emit time."""

    def __init__(self, store: LogStore) -> None:
        super().__init__(level=logging.DEBUG)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            logevents.check(getattr(record, "event", None))
            line = build_line(record, process=self.store.process, version=self.store.version)
            self.store.submit(line)
        except Exception:
            self.handleError(record)


class ConsoleFormatter(logging.Formatter):
    """One short readable line per record while running make run/dev."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        parts = [
            ts,
            record.levelname.ljust(8),
            component_name(record.name),
            scrub_text(record.getMessage()),
        ]
        error_id = getattr(record, "error_id", None)
        if error_id:
            parts.append(f"[{error_id}]")
        if record.exc_info:
            parts.append("\n" + scrub_text(self.formatException(record.exc_info)))
        return " ".join(parts)


def make_library_filter() -> _LibraryFilter:
    return _LibraryFilter()
