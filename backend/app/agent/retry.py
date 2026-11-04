"""``retry_call()`` — the one retry helper for idempotent reads.

Carries the policies the spec already sets, unchanged:

- candles: 3 tries, 10 s apart (the freshness retry);
- OpenAI: one retry on a network error, 429 or 5xx;
- collector sources: one retry.

It honours ``Retry-After`` and never runs past an optional deadline.
Each attempt is a DEBUG line; giving up is the catalogue's line. A paper
fill, a database write that may have committed and an email are never
retried by it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger("app.agent.retry")

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 2  # total tries, not retries
    interval_s: float = 10.0
    deadline_s: float | None = None


# The named policies, numbers unchanged from the existing rules.
CANDLE_POLICY = RetryPolicy(attempts=3, interval_s=10.0)
OPENAI_POLICY = RetryPolicy(attempts=2)
COLLECTOR_POLICY = RetryPolicy(attempts=2, interval_s=0.25)


async def retry_call(  # noqa: UP047
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    retry_if: Callable[[T], bool] | None = None,
    retry_on: Callable[[BaseException], bool] | None = None,
    retry_after_s: Callable[[BaseException], float | None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] | None = None,
    label: str = "call",
) -> T:
    """Call ``fn`` up to ``policy.attempts`` times.

    ``retry_if`` decides a *result* is worth another try (a missing
    candle); ``retry_on`` decides an *exception* is retryable (429, 5xx,
    network). ``retry_after_s`` overrides the wait from the response's
    Retry-After, capped by the deadline.
    """
    monotonic = clock or (lambda: __import__("time").monotonic())
    deadline = monotonic() + policy.deadline_s if policy.deadline_s else None
    last_exc: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            result = await fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised unless retryable
            last_exc = exc
            retryable = retry_on(exc) if retry_on is not None else False
        else:
            if retry_if is None or not retry_if(result) or attempt >= policy.attempts:
                return result
            last_exc = None
            retryable = True
        if not retryable or attempt >= policy.attempts:
            break
        wait = policy.interval_s
        if last_exc is not None and retry_after_s is not None:
            hinted = retry_after_s(last_exc)
            if hinted is not None:
                wait = hinted
        if deadline is not None:
            wait = max(0.0, min(wait, deadline - monotonic()))
        log.debug(
            "%s: attempt %d failed; retrying in %.1f s",
            label,
            attempt,
            wait,
            extra={
                "event": "retry.attempt",
                "data": {"label": label, "attempt": attempt, "wait_s": wait},
            },
        )
        if wait > 0:
            await sleep(wait)
    if last_exc is not None:
        raise last_exc
    return result  # type: ignore[possibly-undefined]
