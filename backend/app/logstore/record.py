"""Turn a ``logging.LogRecord`` into the ``log_lines`` row dict.

Column names match the spec's table exactly. Context fields come from
``logstore.context`` (the trace context) merged with ``extra`` fields on
the record — explicit extras win.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import traceback as tb_module
from datetime import UTC, datetime
from typing import Any

from app.logstore import context
from app.logstore.scrub import scrub_text

LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL"}

# log_lines columns, in insert order. `id` is the rowid.
COLUMNS = (
    "ts",
    "level",
    "levelno",
    "logger",
    "event",
    "message",
    "process",
    "pid",
    "task",
    "cycle_id",
    "job",
    "pair",
    "decision_id",
    "trade_id",
    "order_id",
    "shadow_id",
    "backtest_id",
    "request_id",
    "duration_ms",
    "error_id",
    "exc_type",
    "exc_message",
    "traceback",
    "data",
    "fingerprint",
    "version",
)

# Module logger name -> component name used on the Logs screen and in
# queries. Anything unmapped keeps its name minus the leading "app.".
_COMPONENTS = {
    "app.main": "app",
    "app.agent.engine": "agent.scheduler",
    "app.agent.deciders.rules": "agent.rules",
    "app.agent.deciders.openai_decider": "agent.decider",
    "app.agent.deciders.base": "agent.decider",
    "app.agent.openai_client": "agent.decider",
    "app.agent.risk": "agent.risk",
    "app.agent.signals": "agent.rules",
    "app.agent.brief": "agent.decider",
    "app.agent.brief_v2": "agent.decider",
    "app.agent.broker.paper": "broker.paper",
    "app.agent.market.bitvavo": "market.bitvavo",
    "app.agent.market.universe": "market.universe",
    "app.agent.market.sim": "market.bitvavo",
    "app.agent.outcomes": "agent.outcomes",
    "app.agent.limit": "broker.paper",
    "app.context.collector": "context.collector",
    "app.context.scheduler": "context.collector",
    "app.context.backfill": "context.collector",
    "app.api.notifications": "notify",
    "app.operations": "ops.backup",
    "app.logstore.writer": "ops.logs",
    "app.features.briefings": "ops.jobs",
    "app.features.shadows": "agent.shadows",
    "app.features.whatif": "agent.whatif",
    "app.features.needs": "app",
    "app.features.ask": "agent.decider",
    "py.warnings": "python.warning",
}


def component_name(logger_name: str) -> str:
    if logger_name in _COMPONENTS:
        return _COMPONENTS[logger_name]
    if logger_name == "app" or not logger_name.startswith("app."):
        return logger_name
    return logger_name.removeprefix("app.")


def new_error_id() -> str:
    return "e-" + secrets.token_hex(4)


def _ts(created: float) -> str:
    dt = datetime.fromtimestamp(created, UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_FINGERPRINT_SUBS = [
    re.compile(r'"[^"]*"'),  # quoted values
    re.compile(r"'[^']*'"),
    re.compile(r"\b[A-Z]{2,6}-EUR\b"),  # pairs
    re.compile(r"\b[cejwbt]-[0-9a-f]+\b"),  # c-0716, e-3f9a21c0, j-0313, w-/b- ids
    re.compile(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"),  # times
    re.compile(r"\d{4}-\d{2}-\d{2}"),  # dates
    re.compile(r"\d+(?:[.,]\d+)?"),  # numbers
]


def fingerprint(logger: str, event: str | None, message: str) -> str:
    """logger + event + the message with volatile values replaced."""
    normalized = message
    for pattern in _FINGERPRINT_SUBS:
        normalized = pattern.sub("<v>", normalized)
    normalized = re.sub(r"(?:<v>\s*){2,}", "<v> ", normalized).strip()
    if len(normalized) > 120:
        normalized = normalized[:120] + "…"
    return f"{logger}|{event or '-'}|{normalized}"


def build_line(
    record: logging.LogRecord,
    *,
    process: str,
    version: str,
) -> dict[str, Any]:
    """The row dict for ``log_lines`` — scrubbed and context-stamped."""
    levelno = record.levelno
    line: dict[str, Any] = {
        "ts": _ts(record.created),
        "level": LEVEL_NAMES.get(levelno, record.levelname),
        "levelno": levelno,
        "logger": component_name(record.name),
        "event": getattr(record, "event", None),
        "message": scrub_text(record.getMessage()),
        "process": getattr(record, "process_name", process),
        "pid": os.getpid(),
        "task": getattr(record, "taskName", None) or record.threadName,
        "duration_ms": getattr(record, "duration_ms", None),
        "error_id": getattr(record, "error_id", None),
        "exc_type": None,
        "exc_message": None,
        "traceback": None,
        "data": None,
        "fingerprint": None,
        "version": version,
    }
    ctx = context.current()
    for field in context.CONTEXT_FIELDS:
        line[field] = getattr(record, field, None) or ctx.get(field)
    if line["error_id"] is None and levelno >= logging.ERROR:
        line["error_id"] = new_error_id()
    if record.exc_info and record.exc_info[0] is not None:
        exc = record.exc_info[1]
        line["exc_type"] = (
            f"{record.exc_info[0].__module__}.{record.exc_info[0].__qualname__}"
        )
        line["exc_message"] = scrub_text(str(exc))
        line["traceback"] = scrub_text(
            "".join(tb_module.format_exception(*record.exc_info))
        )
    data = getattr(record, "data", None)
    if data is not None:
        import json

        try:
            line["data"] = scrub_text(
                json.dumps(data, default=str, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            line["data"] = scrub_text(str(data))
    if levelno >= logging.WARNING:
        line["fingerprint"] = fingerprint(
            str(line["logger"]), line["event"], str(line["message"])
        )
    return line
