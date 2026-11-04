"""Trace context: contextvars that stamp every log line written inside.

Every cycle, watcher pass, job and API request gets an id; ``log_context``
adds pair, decision, trade, order and shadow ids as they become known.
``current()`` merges the live context into a log record; tasks started
through ``app.tasks.spawn`` copy the context so lines written inside a
spawned task during a cycle still carry the cycle id and pair.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

# Every context field is a column on log_lines.
CONTEXT_FIELDS = (
    "cycle_id",
    "job",
    "pair",
    "decision_id",
    "trade_id",
    "order_id",
    "shadow_id",
    "backtest_id",
    "request_id",
)

_ctx: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "ta_log_context", default=None
)


def current() -> dict[str, str]:
    """The live trace context (a copy; never mutated in place)."""
    return dict(_ctx.get() or {})


@contextmanager
def log_context(**fields: str | None) -> Iterator[None]:
    """Add trace fields for the duration of the block, then restore."""
    merged = (_ctx.get() or {}).copy()
    for key, value in fields.items():
        if key not in CONTEXT_FIELDS:
            raise KeyError(f"unknown log-context field {key!r}")
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    token = _ctx.set(merged)
    try:
        yield
    finally:
        _ctx.reset(token)
