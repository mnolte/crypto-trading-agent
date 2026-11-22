"""Sync-run records for the Universe → Data sync card (item 026).

Every sync job writes its own ``sync_runs`` row: at start, throttled to
at most one update a second while it works, and at the end with
``ok``, ``partial`` or ``failed`` plus the per-market/source failures
in plain words. ``failures`` is a JSON list of ``{what, tf?, error,
next_retry}`` where ``what`` is a market or a source name.

``SyncRuns.begin`` returns a ``SyncRun`` handle the job updates through
``progress`` / ``fail`` / ``finish``. Rows older than ``KEEP_DAYS`` are
pruned when a new run starts.

Backfill writes one run per market as ``backfill:<MARKET>`` so the API
can show per-market progress; the other jobs are single runs.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import MarketCheckRow, MarketRow, SyncRunRow, utcnow

logger = logging.getLogger(__name__)

SYNC_JOBS = ("exchange_rules", "daily_check", "candles", "backfill")
BACKFILL_PREFIX = "backfill:"

# sync_runs rows are kept this long (docs/SPEC.md, Universe screen).
KEEP_DAYS = 30

# market_checks rows are kept this long.
CHECK_KEEP_DAYS = 90

# A running job updates its row at most this often (docs/SPEC.md).
PROGRESS_INTERVAL_SECONDS = 1.0


def plain_error(exc: BaseException) -> str:
    """An exception's text in plain words — the only form an error may
    take into a market_checks / sync_runs row (hard rule 8)."""
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _prune_old(session: Session, model: type, column: Any, days: int, now: datetime) -> None:
    cutoff = now - timedelta(days=days)
    session.execute(delete(model).where(column < cutoff.replace(tzinfo=None)))


class SyncRun:
    """Handle for one in-flight ``sync_runs`` row.

    All methods are synchronous and cheap: progress writes are throttled
    to one per ``PROGRESS_INTERVAL_SECONDS`` (a dirty flag guarantees the
    last state still lands), failures accumulate in memory and land on
    the next write, and ``finish`` always writes the final row.
    """

    def __init__(self, syncs: SyncRuns, run_id: int, job: str) -> None:
        self._syncs = syncs
        self.id = run_id
        self.job = job
        self._done = 0
        self._total: int | None = None
        self._failures: list[dict[str, Any]] = []
        self._failed_whats: set[str] = set()
        self._aborted = False
        self._finished = False
        self._last_write = 0.0
        self._dirty = False

    # -- job-facing API -------------------------------------------------

    def progress(self, done: int, total: int | None = None) -> None:
        """Record ``done`` of ``total`` units finished; writes at most
        once a second, the rest is flushed by the next write."""
        self._done = done
        if total is not None:
            self._total = total
        self._write_if_due(force=False)

    def set_total(self, total: int) -> None:
        self._total = total
        self._write_if_due(force=False)

    def fail(
        self,
        what: str,
        error: str,
        next_retry: datetime | None = None,
        tf: str | None = None,
    ) -> None:
        """One market or source failed; ``error`` in plain words."""
        key = f"{what}|{tf or ''}"
        if key in self._failed_whats:
            return
        self._failed_whats.add(key)
        entry: dict[str, Any] = {
            "what": str(what),
            "error": str(error)[:500],
            "next_retry": _iso(next_retry),
        }
        if tf is not None:
            entry["tf"] = tf
        self._failures.append(entry)
        self._write_if_due(force=False)

    def abort(self, error: str) -> None:
        """The job itself failed — status ``failed`` at finish."""
        self._aborted = True
        self.fail(self.job, error)

    def finish(self) -> None:
        """Write the final row: ``failed`` on abort or when nothing
        succeeded, ``partial`` with failures, else ``ok``."""
        if self._finished:
            return
        self._finished = True
        if self._aborted:
            status = "failed"
        elif not self._failures:
            status = "ok"
        elif self._done == 0 or len(self._failures) >= max(self._done, 1):
            status = "failed"
        else:
            status = "partial"
        self._syncs._finish(
            self.id,
            status=status,
            done=self._done,
            total=self._total,
            failures=self._failures,
        )

    # -- internals ------------------------------------------------------

    def _write_if_due(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < PROGRESS_INTERVAL_SECONDS:
            self._dirty = True
            return
        self._syncs._write(
            self.id,
            done=self._done,
            total=self._total,
            failures=self._failures,
        )
        self._last_write = now
        self._dirty = False

    def flush(self) -> None:
        """Write pending progress now (e.g. before a long await)."""
        if self._dirty and not self._finished:
            self._write_if_due(force=True)


class SyncRuns:
    """The ``sync_runs`` table: begin/progress/finish plus the reads the
    API and the retry endpoint need."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sf = session_factory
        self._clock = clock

    # -- writes ---------------------------------------------------------

    def begin(self, job: str, total: int | None = None) -> SyncRun:
        """Open a new run row for ``job`` and return its handle."""
        if job != BACKFILL_PREFIX[:-1] and not job.startswith(BACKFILL_PREFIX):
            family = job.split(":", 1)[0]
            if family not in SYNC_JOBS:
                raise ValueError(f"unknown sync job {job!r}")
        now = self._clock()
        with self._sf() as session:
            _prune_old(session, SyncRunRow, SyncRunRow.started_at, KEEP_DAYS, now)
            _prune_old(session, MarketCheckRow, MarketCheckRow.started_at, CHECK_KEEP_DAYS, now)
            row = SyncRunRow(
                job=job,
                started_at=now.replace(tzinfo=None),
                status="running",
                done=0,
                total=total,
                failures=None,
                updated_at=now.replace(tzinfo=None),
            )
            session.add(row)
            session.commit()
            run_id = row.id
        return SyncRun(self, run_id, job)

    def _write(
        self,
        run_id: int,
        *,
        done: int,
        total: int | None,
        failures: list[dict[str, Any]],
    ) -> None:
        with self._sf() as session:
            row = session.get(SyncRunRow, run_id)
            if row is None:
                return
            row.done = done
            row.total = total
            row.failures = json.dumps(failures) if failures else None
            row.updated_at = self._clock().replace(tzinfo=None)
            session.commit()

    def _finish(
        self,
        run_id: int,
        *,
        status: str,
        done: int,
        total: int | None,
        failures: list[dict[str, Any]],
    ) -> None:
        with self._sf() as session:
            row = session.get(SyncRunRow, run_id)
            if row is None:
                return
            row.done = done
            row.total = total
            row.failures = json.dumps(failures) if failures else None
            row.status = status
            row.finished_at = self._clock().replace(tzinfo=None)
            row.updated_at = row.finished_at
            session.commit()
        logger.info(
            "sync run %s finished: %s (%s, %s/%s, %d failure(s))",
            run_id,
            row.job,
            status,
            done,
            total if total is not None else "?",
            len(failures),
            extra={"event": "sync.run_finished", "job": row.job, "status": status},
        )

    # -- reads ----------------------------------------------------------

    def latest_rows(self) -> dict[str, SyncRunRow]:
        """The newest run per job; ``backfill:*`` rows collapse to the
        newest per market under the ``backfill`` key list instead."""
        with self._sf() as session:
            rows = (
                session.execute(select(SyncRunRow).order_by(SyncRunRow.id.desc())).scalars().all()
            )
        latest: dict[str, SyncRunRow] = {}
        for row in rows:
            latest.setdefault(row.job, row)
        return latest

    def latest(self, job: str) -> SyncRunRow | None:
        """Newest run for a job; for ``backfill`` the newest of any
        ``backfill:*`` run."""
        with self._sf() as session:
            if job == "backfill":
                stmt = (
                    select(SyncRunRow)
                    .where(SyncRunRow.job.like(f"{BACKFILL_PREFIX}%"))
                    .order_by(SyncRunRow.id.desc())
                    .limit(1)
                )
            else:
                stmt = (
                    select(SyncRunRow)
                    .where(SyncRunRow.job == job)
                    .order_by(SyncRunRow.id.desc())
                    .limit(1)
                )
            return session.execute(stmt).scalar_one_or_none()

    def runs_for(self, job: str) -> list[SyncRunRow]:
        """Newest-first runs for a job family (``backfill`` → all
        ``backfill:*``)."""
        with self._sf() as session:
            if job == "backfill":
                stmt = (
                    select(SyncRunRow)
                    .where(SyncRunRow.job.like(f"{BACKFILL_PREFIX}%"))
                    .order_by(SyncRunRow.id.desc())
                )
            else:
                stmt = (
                    select(SyncRunRow).where(SyncRunRow.job == job).order_by(SyncRunRow.id.desc())
                )
            return list(session.execute(stmt).scalars().all())

    def running(self, job: str) -> bool:
        row = self.latest(job)
        return row is not None and row.status == "running"

    @staticmethod
    def row_payload(row: SyncRunRow) -> dict[str, Any]:
        started = row.started_at
        finished = row.finished_at
        duration = (
            (finished - started).total_seconds()
            if finished is not None and started is not None
            else None
        )
        try:
            failures = json.loads(row.failures) if row.failures else []
        except (TypeError, ValueError):
            failures = []
        payload: dict[str, Any] = {
            "job": row.job,
            "status": row.status,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_seconds": duration,
            "done": row.done,
            "total": row.total,
            "failures": failures,
        }
        if row.job.startswith(BACKFILL_PREFIX):
            payload["market"] = row.job[len(BACKFILL_PREFIX) :]
        return payload


def next_cycle_start(now: datetime, offset_s: int = 0, cycle_s: int = 900) -> datetime:
    """The next 15-minute cycle boundary plus ``offset_s`` — the
    ``next_retry`` a failed job gets (docs/SPEC.md, Universe screen)."""
    base = now.replace(second=0, microsecond=0)
    base = base.replace(minute=(base.minute // 15) * 15)
    due = base + timedelta(seconds=offset_s)
    if due <= now:
        due += timedelta(seconds=cycle_s)
    while due <= now:
        due += timedelta(seconds=cycle_s)
    return due


def backfill_retry_due(
    session: Session,
    now: datetime,
) -> dict[str, set[str]]:
    """``{market: {tf, ...}}`` for failed/partial backfill runs whose
    recorded ``next_retry`` has passed — the cycle retries them for
    those timeframes only (docs/SPEC.md, Universe screen)."""
    cutoff = now - timedelta(days=KEEP_DAYS)
    rows = (
        session.execute(
            select(SyncRunRow)
            .where(
                SyncRunRow.job.like(f"{BACKFILL_PREFIX}%"),
                SyncRunRow.status.in_(("failed", "partial")),
                SyncRunRow.started_at >= cutoff.replace(tzinfo=None),
            )
            .order_by(SyncRunRow.id)
        )
        .scalars()
        .all()
    )
    due: dict[str, set[str]] = {}
    for row in rows:
        try:
            failures = json.loads(row.failures) if row.failures else []
        except (TypeError, ValueError):
            continue
        market = row.job[len(BACKFILL_PREFIX) :]
        for f in failures:
            if not isinstance(f, dict):
                continue
            retry = _parse_iso(f.get("next_retry"))
            tf = f.get("tf")
            if retry is not None and retry <= now and tf:
                due.setdefault(market, set()).add(str(tf))
    return due


def check_retention(session: Session, now: datetime | None = None) -> None:
    """Delete market_checks older than 90 days and sync_runs older than
    30 days (the only rows retention may touch)."""
    now = now or utcnow()
    _prune_old(session, MarketCheckRow, MarketCheckRow.started_at, CHECK_KEEP_DAYS, now)
    _prune_old(session, SyncRunRow, SyncRunRow.started_at, KEEP_DAYS, now)


def market_display_name(session: Session, market: str) -> str | None:
    row = session.get(MarketRow, market)
    return row.name if row is not None else None


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
