from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.context.collector import ContextCollector

log = logging.getLogger(__name__)


def next_run(now: datetime) -> datetime:
    due = now.astimezone(UTC).replace(minute=5, second=0, microsecond=0)
    return due if due > now else due + timedelta(hours=1)


async def context_loop(
    collector: ContextCollector,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    try:
        await collector.collect()
        while True:
            delay = max(0.0, (next_run(clock()) - clock()).total_seconds())
            await sleep(delay)
            await collector.collect()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("market context scheduler failed")
    finally:
        await collector.aclose()
