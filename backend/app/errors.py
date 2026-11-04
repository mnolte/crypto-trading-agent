"""The app's exception classes.

``AgentError`` carries the event code it logs under and whether it is
retryable. Library exceptions are converted at the client that calls the
library (Bitvavo, OpenAI, the collector sources, SMTP, keyring,
SQLAlchemy), keeping the original as the cause, so code above the client
catches these classes and never ``Exception``.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class: an event code and a retryable flag."""

    event = "app.error"
    retryable = False

    def __init__(self, message: str, *, event: str | None = None, **data: Any) -> None:
        super().__init__(message)
        if event is not None:
            self.event = event
        self.data = data


class DataError(AgentError):
    """Stale, missing or invalid data."""


class SourceError(AgentError):
    """An external source: HTTP status, timeout or rate limit."""

    event = "candles.fetch_failed"
    retryable = True

    def __init__(self, message: str, *, status: int | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.status = status


class ModelError(AgentError):
    """The decision model: timeout, refused, invalid output, cost cap."""

    event = "model.call_failed"
    retryable = True


class BrokerError(AgentError):
    """A paper-broker invariant was violated."""


class StorageError(AgentError):
    """The trading database failed to write."""

    event = "db.locked"


class ConfigError(AgentError):
    """Config or secrets are invalid or unavailable."""

    event = "config.invalid"


class SafetyError(AgentError):
    """Safety at risk: kill switch, unpriced position, unwritable store."""
