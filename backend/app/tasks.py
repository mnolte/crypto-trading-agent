"""``spawn()`` — the only way background tasks start.

A bare ``asyncio.create_task`` outside this helper fails a test. Every
task runs inside a copy of the caller's log context, so lines written
inside a spawned task during a cycle still carry the cycle id and pair.

A task that raises is logged at ERROR (``task.crashed``) and restarted
after a backoff from 5 s to 5 minutes; 5 crashes in 10 minutes is
CRITICAL (``task.crash_loop``) and a Needs you degraded item.
``spawn(..., restart=False)`` is for fire-and-forget one-shots
(notification sends): they are logged on failure, never restarted.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

log = logging.getLogger("app.tasks")

P = ParamSpec("P")
T = TypeVar("T")

BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0
CRASH_LIMIT = 5
CRASH_WINDOW_S = 600.0

_crashes: dict[str, deque[float]] = defaultdict(deque)
crash_loops: set[str] = set()


def record_crash(name: str) -> bool:
    """One more crash for ``name``; True once it is a crash loop."""
    hits = _crashes[name]
    now = time.monotonic()
    hits.append(now)
    while hits and now - hits[0] > CRASH_WINDOW_S:
        hits.popleft()
    if len(hits) >= CRASH_LIMIT:
        crash_loops.add(name)
        return True
    return False


def spawn(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    name: str | None = None,
    restart: bool = True,
    track: Callable[[asyncio.Task[Any]], None] | None = None,
    **kwargs: Any,
) -> asyncio.Task[Any]:
    """Start ``fn(*args, **kwargs)`` as a supervised task.

    ``fn`` is a coroutine *function* (usually an endless loop); on a crash
    it is restarted with exponential backoff. The caller's log context is
    copied into the task.
    """
    task_name = str(name or getattr(fn, "__qualname__", "task"))
    ctx = contextvars.copy_context()

    async def run_once() -> None:
        await fn(*args, **kwargs)

    async def supervisor() -> None:
        backoff = BACKOFF_START_S
        while True:
            try:
                await run_once()
                return  # a clean return ends the task, no restart needed
            except asyncio.CancelledError:
                raise
            except Exception:
                loop_crash = record_crash(task_name)
                log.exception(
                    "background task %s crashed; restarting in %.0f s",
                    task_name,
                    backoff,
                    extra={
                        "event": "task.crashed",
                        "data": {"task_name": task_name, "backoff_s": backoff},
                    },
                )
                if loop_crash:
                    log.critical(
                        "background task %s crashed %d times in %d minutes",
                        task_name,
                        CRASH_LIMIT,
                        int(CRASH_WINDOW_S / 60),
                        extra={
                            "event": "task.crash_loop",
                            "data": {"task_name": task_name},
                        },
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def oneshot() -> None:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            record_crash(task_name)
            log.exception(
                "background task %s failed",
                task_name,
                extra={"event": "task.crashed", "data": {"task_name": task_name}},
            )

    wrapper = supervisor if restart else oneshot
    task = ctx.run(asyncio.create_task, wrapper(), name=task_name)
    if track is not None:
        track(task)
    return task


def install_global_hooks(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """So nothing escapes the log: thread exceptions, un-awaited task
    results, the asyncio exception handler and Python warnings."""
    previous = sys.excepthook

    def _sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        log.critical(
            "uncaught exception on the main thread: %s",
            exc,
            exc_info=(exc_type, exc, tb),
            extra={"event": "task.crashed"},
        )
        previous(exc_type, exc, tb)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        log.error(
            "uncaught exception in thread %s: %s",
            args.thread.name if args.thread else "?",
            args.exc_value,
            exc_info=(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
            )
            if isinstance(args.exc_value, BaseException) and args.exc_type is not None
            else None,
            extra={"event": "task.crashed"},
        )

    def _asyncio_hook(_loop: asyncio.AbstractEventLoop, ctx: dict[str, Any]) -> None:
        exc = ctx.get("exception")
        log.error(
            "asyncio: %s",
            ctx.get("message", exc or "loop error"),
            exc_info=exc if isinstance(exc, BaseException) else None,
            extra={"event": "task.crashed"},
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is not None:
        loop.set_exception_handler(_asyncio_hook)
    logging.captureWarnings(True)
