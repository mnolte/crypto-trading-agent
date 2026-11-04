"""The bounded log queue.

A log call puts the record here and returns; it never waits for disk.
When the queue is full, DEBUG lines are dropped first, then INFO — a
deeper line takes a shallower line's slot. WARNING and up are never
dropped: the call waits up to 1 s for room, then the line goes to the
fallback file. Dropped lines are counted per level.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Literal

PutResult = Literal["queued", "dropped", "fallback"]

_WARN_WAIT_S = 1.0


class LogQueue:
    def __init__(self, maxsize: int = 20000) -> None:
        self._maxsize = max(1, maxsize)
        self._dq: deque[dict[str, Any]] = deque()
        self._cond = threading.Condition()
        self.dropped = {"DEBUG": 0, "INFO": 0}
        self.closed = False

    def configure(self, maxsize: int) -> None:
        with self._cond:
            self._maxsize = max(1, maxsize)

    def put(self, line: dict[str, Any]) -> PutResult:
        levelno = int(line["levelno"])
        deadline = time.monotonic() + _WARN_WAIT_S
        with self._cond:
            while True:
                if len(self._dq) < self._maxsize:
                    self._dq.append(line)
                    self._cond.notify()
                    return "queued"
                if self.closed:
                    return "fallback"
                if levelno <= logging.DEBUG:
                    self.dropped["DEBUG"] += 1
                    return "dropped"
                if levelno <= logging.INFO:
                    if self._drop_oldest(logging.DEBUG):
                        self.dropped["DEBUG"] += 1
                        continue
                    self.dropped["INFO"] += 1
                    return "dropped"
                # WARNING and up: sacrifice a shallower line, else wait.
                if self._drop_oldest(logging.DEBUG):
                    self.dropped["DEBUG"] += 1
                    continue
                if self._drop_oldest(logging.INFO):
                    self.dropped["INFO"] += 1
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "fallback"
                self._cond.wait(remaining)

    def _drop_oldest(self, levelno: int) -> bool:
        for i, line in enumerate(self._dq):
            if int(line["levelno"]) <= levelno:
                del self._dq[i]
                return True
        return False

    def drain(self, max_items: int, timeout: float) -> list[dict[str, Any]]:
        """Wait up to ``timeout`` for the first line, then take up to
        ``max_items``. An ERROR-or-deeper line ends the batch early so it
        commits at once with the lines before it."""
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._dq:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.closed:
                    return batch
                self._cond.wait(remaining)
            while self._dq and len(batch) < max_items:
                line = self._dq.popleft()
                batch.append(line)
                if int(line["levelno"]) >= logging.ERROR:
                    break
            self._cond.notify_all()
        return batch

    def __len__(self) -> int:
        with self._cond:
            return len(self._dq)

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()
