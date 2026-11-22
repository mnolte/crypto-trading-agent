"""FastAPI app: lifespan, security middleware, routers and the built UI.

The lifespan acquires the single-engine lock, runs migrations, loads settings
and puts the app into the Paused state. The scheduler and exit watcher join in
later PRs; exactly one engine instance may run per database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.engine import Engine as DbEngine
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app import logstore, repo, settings_service
from app import tasks as tasks_mod
from app.agent.broker.paper import PaperBroker
from app.agent.deciders.openai_decider import OpenAIDecider
from app.agent.deciders.rules import RulesDecider
from app.agent.engine import Engine
from app.agent.market.sync import SyncRuns, plain_error
from app.agent.market.universe import (
    run_eligibility_check,
    warming_up,
)
from app.agent.outcomes import outcome_loop
from app.api import context as context_routes
from app.api import dashboard as dashboard_routes
from app.api import deps, errors, evidence, inspector, notifications, positions, routes, screens, ws
from app.api import logs as logs_routes
from app.api import sources as source_routes
from app.api import understanding as understanding_routes
from app.api import universe as universe_routes
from app.api.deps import (
    AppState,
    LocalOnlyMiddleware,
    build_market_client,
    generate_local_token,
)
from app.config import REPO_ROOT, EnvConfig, get_env, get_secret
from app.context.collector import ContextCollector
from app.context.scheduler import context_loop
from app.db import (
    EngineLock,
    ErrorRow,
    MarketCheckRow,
    MarketRow,
    make_engine,
    make_session_factory,
    run_migrations,
    sqlite_quick_check,
    utcnow,
)
from app.features import briefings as briefings_mod
from app.features import fills as fills_mod
from app.features.shadows import ShadowRunner
from app.logstore.record import fingerprint
from app.operations import backup_loop
from app.sources import SOURCES, SourceRegistry

log = logging.getLogger(__name__)

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

SOURCE_RECOVERY_RESOLVE_S = 3600
EMAIL_SHUTDOWN_GRACE_S = 5.0
CHECKPOINT_RETRY_S = 5.0


class StartupRecoveryError(RuntimeError):
    """Startup cannot safely continue; the process must exit fail-closed."""


# Strong refs for in-flight shutdown-announce sends; done callbacks drop them.
_announce_tasks: set[asyncio.Task[None]] = set()


def _interrupt_running_runs(eng: DbEngine, *, reason: str = "restart") -> dict[str, int]:
    """Close persisted work that cannot continue on a new process.

    What-if rows still queued/running are marked interrupted, sync runs
    still running are failed with the reason appended, and an unfinished
    market check is failed. Each table is handled separately: a legacy
    database missing one of them must not block the others.
    """
    counts: dict[str, int] = {}
    try:
        with eng.begin() as conn:
            counts["whatif"] = (
                conn.execute(
                    text(
                        "UPDATE whatif_scenarios SET status = 'interrupted' "
                        "WHERE status IN ('queued', 'running')"
                    )
                ).rowcount
                or 0
            )
    except SQLAlchemyError:
        pass
    try:
        with eng.begin() as conn:
            rows = conn.execute(
                text("SELECT id, job, failures FROM sync_runs WHERE status = 'running'")
            ).fetchall()
            now = utcnow().replace(tzinfo=None)
            for row in rows:
                try:
                    failures = json.loads(row.failures or "[]")
                except (TypeError, ValueError):
                    failures = []
                if not isinstance(failures, list):
                    failures = []
                failures.append(
                    {
                        "what": row.job,
                        "error": f"sync run interrupted by {reason}",
                        "next_retry": None,
                    }
                )
                conn.execute(
                    text(
                        "UPDATE sync_runs SET status = 'failed', finished_at = :now, "
                        "updated_at = :now, failures = :failures WHERE id = :id"
                    ),
                    {
                        "now": now,
                        "failures": json.dumps(failures),
                        "id": row.id,
                    },
                )
            counts["sync_runs"] = len(rows)
    except SQLAlchemyError:
        pass
    try:
        with eng.begin() as conn:
            counts["market_checks"] = (
                conn.execute(
                    text(
                        "UPDATE market_checks SET status = 'failed', "
                        "error = COALESCE(error, :error), finished_at = :now "
                        "WHERE finished_at IS NULL"
                    ),
                    {
                        "error": f"market check interrupted by {reason}",
                        "now": utcnow().replace(tzinfo=None),
                    },
                ).rowcount
                or 0
            )
    except SQLAlchemyError:
        pass
    return counts


async def _resolve_source_error_after_recovery(
    state: AppState,
    source_id: str,
    generation: int,
    generations: dict[str, int],
) -> None:
    await asyncio.sleep(SOURCE_RECOVERY_RESOLVE_S)
    if (
        generations.get(source_id) != generation
        or state.sources.status(source_id).runtime.failures != 0
    ):
        return
    session = state.session_factory()
    try:
        repo.resolve_matching_errors(
            session,
            f"data source failing: {source_id}",
            "source recovered for 1 hour",
        )
        session.commit()
    finally:
        session.close()
    if state.log_store is not None:
        state.log_store.review_problem(
            fingerprint("app", "source.failing", f"data source failing: {source_id}"),
            "fixed",
            "source recovered for 1 hour",
            datetime.now(UTC),
        )


def _loop_of(coro: Awaitable[None]) -> Callable[[], Awaitable[None]]:
    """spawn() takes a coroutine function; job loops are built per
    restart so a crashed loop starts fresh."""

    async def run() -> None:
        await coro

    return run


def _heartbeat_loop(lock: EngineLock) -> Callable[[], Awaitable[None]]:
    """Touches the lock file every 30 s so a stale lock is obvious."""

    async def run() -> None:
        while True:
            await asyncio.sleep(30)
            lock.heartbeat()

    return run


def _unclean_start(env: EnvConfig) -> None:
    """A leftover lock file means the last run did not shut down cleanly:
    WARNING, persisted in-progress work is closed, and the agent restarts
    paused (AppState defaults). The trading database's integrity was
    verified before this runs."""
    log.warning(
        "previous run did not shut down cleanly",
        extra={"event": "app.unclean_shutdown"},
    )
    eng = make_engine(env.ta_db_path)
    interrupted = _interrupt_running_runs(eng, reason="unclean restart")
    eng.dispose()
    total = sum(interrupted.values())
    if total:
        log.warning(
            "%d interrupted run(s) marked after an unclean shutdown",
            total,
            extra={"event": "app.unclean_shutdown", "data": interrupted},
        )


def _verify_trading_database(db_path: Path) -> None:
    """Fail closed before migrations when the trading DB is not readable."""
    problem = sqlite_quick_check(db_path)
    if problem is None:
        return
    log.critical(
        "trading database failed startup integrity check: %s",
        problem,
        extra={"event": "db.corrupt", "data": {"path": str(db_path)}},
    )
    raise StartupRecoveryError(f"trading database at {db_path} failed integrity check: {problem}")


def _report_auxiliary_database_problems(env: EnvConfig) -> None:
    """Non-blocking diagnostics for the logs DB and newest backup."""
    logs_db = env.ta_data_dir / "logs" / "logs.db"
    problem = sqlite_quick_check(logs_db)
    if problem is not None:
        log.critical(
            "logs.db failed startup integrity check: %s",
            problem,
            extra={"event": "logs.corrupt", "data": {"path": str(logs_db)}},
        )
    backup_dir = env.ta_data_dir / "backups"
    backups = sorted(backup_dir.glob("agent-*.db"), reverse=True) if backup_dir.is_dir() else []
    if not backups:
        return
    problem = sqlite_quick_check(backups[0], read_only=True)
    if problem is not None:
        log.error(
            "newest backup failed startup integrity check: %s",
            problem,
            extra={"event": "backup.invalid", "data": {"path": str(backups[0])}},
        )


def _wire_log_push(state: AppState, store: Any) -> None:
    """WARNING-and-up lines go out live on the WS "logs" topic — the
    writer thread hands them to the loop thread-safely."""
    loop = asyncio.get_running_loop()

    def push(line: dict[str, Any]) -> None:
        if not state.ws_clients:
            return

        async def send() -> None:
            payload = '{"type":"logs","data":' + json.dumps(line, default=str) + "}"
            for client in list(state.ws_clients):
                try:
                    await client.send_text(payload)
                except Exception:  # noqa: BLE001 - a closed socket shows as any error
                    state.ws_clients.discard(client)

        asyncio.run_coroutine_threadsafe(send(), loop)

    store.on_line = push


def _announce_shutdown(app: FastAPI) -> None:
    """Push the shutdown notice to every tab over the snapshot stream.

    Called from the signal handler because uvicorn has already closed the
    sockets by the time the lifespan finally runs; also called there for
    non-signal shutdowns. The flag makes it once-only.
    """
    if getattr(app.state, "shutdown_announced", False):
        return
    app.state.shutdown_announced = True
    state = getattr(app.state, "app_state", None)
    if state is None or not state.ws_clients:
        return

    async def send() -> None:
        payload = (
            '{"type":"alert","data":{"event":"shutdown","severity":"critical",'
            '"title":"Trading Agent is shutting down",'
            '"body":"Running work finishes first; the app exits when it is done."}}'
        )
        for client in list(state.ws_clients):
            try:
                await client.send_text(payload)
            except Exception:  # noqa: BLE001 - a closed socket shows as any error
                state.ws_clients.discard(client)

    def schedule() -> None:
        task = asyncio.ensure_future(send())
        _announce_tasks.add(task)
        task.add_done_callback(_announce_tasks.discard)

    try:
        asyncio.get_running_loop().call_soon_threadsafe(schedule)
    except RuntimeError:
        pass


def _install_signal_handlers(app: FastAPI) -> None:
    """The first Ctrl+C or SIGTERM is a normal request: the tabs are told
    and the signal goes to uvicorn, which shuts down through the lifespan.

    A second Ctrl+C skips the grace wait but keeps the final
    commit/checkpoint/log steps — it is deliberately NOT handed on,
    because uvicorn's force-exit would skip the whole lifespan shutdown.
    A third one is handed over as a last-resort abort. Repeated SIGTERMs
    always forward; uvicorn never force-exits on SIGTERM.
    """
    previous: dict[int, Any] = {}
    for signo in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signo] = signal.getsignal(signo)
        except (ValueError, OSError):
            pass
    if not previous:
        return
    counts: dict[int, int] = dict.fromkeys(previous, 0)

    def _handler(signum: int, frame: FrameType | None) -> None:
        counts[signum] += 1
        if counts[signum] == 1:
            app.state.shutdown_requested = True
            if signum == signal.SIGINT:
                app.state.sigint_requested = True
            _announce_shutdown(app)
        elif signum == signal.SIGINT and counts[signum] == 2:
            logging.getLogger("app.main").warning(
                "second interrupt: skipping the graceful wait",
                extra={"event": "app.shutdown_forced"},
            )
            app.state.sigint_skip_wait = True
            return
        prev = previous[signum]
        if callable(prev):
            prev(signum, frame)

    for installed_signo in previous:
        try:
            signal.signal(installed_signo, _handler)
        except (ValueError, OSError):
            pass  # not the main thread (tests)


async def _final_checkpoint(eng: DbEngine) -> None:
    """Spec step 4: by now every session is committed or rolled back; the
    WAL checkpoint lands them in the main file. A connection can still be
    open briefly (a backup finishing on its own thread), so the attempt
    retries for a few seconds before warning."""
    deadline = time.monotonic() + CHECKPOINT_RETRY_S
    while True:
        busy = True
        try:
            with eng.connect() as conn:
                conn.execute(text("PRAGMA busy_timeout = 2000"))
                row = conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).fetchone()
                busy = bool(row and row[0])
        except SQLAlchemyError:
            pass
        if not busy:
            return
        if time.monotonic() >= deadline:
            log.warning(
                "final WAL checkpoint did not finish",
                extra={"event": "db.checkpoint_failed"},
            )
            return
        await asyncio.sleep(0.2)


async def _drain_tasks(tasks: list[asyncio.Task[Any]], forced: Callable[[], bool]) -> None:
    """Cancel every tracked task. In-flight email sends get a short
    window to finish first; a second Ctrl+C cancels them too."""
    emails = [t for t in tasks if t.get_name() == "notify-email"]
    others = [t for t in tasks if t.get_name() != "notify-email"]
    for task in others:
        task.cancel()
    if others:
        await asyncio.gather(*others, return_exceptions=True)
    if emails:
        _done, pending = await asyncio.wait(
            emails, timeout=0.0 if forced() else EMAIL_SHUTDOWN_GRACE_S
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _track_task(state: AppState, task: asyncio.Task[None]) -> None:
    state.tasks.append(task)
    task.add_done_callback(
        lambda completed: state.tasks.remove(completed) if completed in state.tasks else None
    )


async def _exchange_rules_job(state: AppState, run: Any = None) -> list[Any]:
    """The exchange_rules sync job: /markets + /assets + market names."""
    infos = await state.market_client.markets()
    state.market_rules.clear()
    state.market_rules.update({i.market: i for i in infos})
    try:
        names = await state.market_client.assets()
    except Exception as exc:  # noqa: BLE001 - names stay pending, rules still load
        if run is not None:
            run.fail("bitvavo", plain_error(exc))
    else:
        session = state.session_factory()
        try:
            for info in infos:
                if info.quote != "EUR":
                    continue
                row = session.get(MarketRow, info.market)
                if row is not None and names.get(info.base):
                    row.name = names[info.base]
            session.commit()
        finally:
            session.close()
    if run is not None:
        run.progress(len(infos), len(infos))
    return infos


async def refresh_market_data(
    state: AppState,
    notify: Callable[[str, str, str], Awaitable[None]] | None = None,
    *,
    trigger: str = "startup",
) -> None:
    """Market-data check: exchange rules, the eligibility check and
    backfills for active markets still warming up. Runs at startup and
    daily at 01:00 UTC (``daily_market_loop``); each part writes its own
    ``sync_runs`` row (item 026).

    Any failure marks the source in the registry; it never stops startup —
    a failing source must not fail cycles, and the app always starts paused.
    """
    session = state.session_factory()
    try:
        rules_run = state.syncs.begin("exchange_rules") if state.syncs else None
        try:
            await _exchange_rules_job(state, rules_run)
        except Exception as exc:
            if rules_run is not None:
                rules_run.abort(plain_error(exc))
            raise
        finally:
            if rules_run is not None:
                rules_run.finish()

        check_run = state.syncs.begin("daily_check") if state.syncs else None
        try:
            _rows, transitions = await run_eligibility_check(
                state.market_client,
                session,
                state.settings.market,
                trigger=trigger,
                on_progress=check_run.progress if check_run else None,
            )
            if check_run is not None:
                latest = session.scalar(
                    select(MarketCheckRow).order_by(MarketCheckRow.id.desc()).limit(1)
                )
                if latest is not None and latest.status == "failed":
                    check_run.abort(latest.error or "check failed")
                else:
                    eur = len(state.market_rules)
                    check_run.progress(eur, eur)
        except Exception as exc:
            if check_run is not None:
                check_run.abort(plain_error(exc))
            raise
        finally:
            if check_run is not None:
                check_run.finish()
        # Commit the activity rows before the sends: a session may never
        # stay checked out across the SMTP await.
        notices: list[tuple[str, str, str]] = []
        for market, change in transitions.items():
            text = f"market {market} eligibility check: {change}"
            repo.add_activity(session, "alert", text, at=utcnow(), pair=market)
            if notify is not None:
                notices.append(("risk_control", f"Market {change}: {market}", text))
        session.commit()
        if notify is not None:
            for kind, title, text in notices:
                await notify(kind, title, text)
        for market in state.settings.active_markets:
            row = session.get(MarketRow, market)
            if row is None:
                row = MarketRow(market=market, eligible=False)
            if not row.active:
                row.active = True
                row.activated_at = utcnow()
                repo.upsert_market_row(session, row)
            if warming_up(session, market, state.settings.market):
                deps.schedule_backfill(state, market)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - boundary: the market-data job
        state.sources.record("bitvavo", exc)
    finally:
        session.close()


async def daily_market_loop(
    state: AppState,
    notify: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> None:
    """The daily 01:00 UTC market-data job: refreshes exchange rules,
    re-runs the eligibility check (drives the anti-flapping streaks) and
    finishes any warming-up backfills."""
    while True:
        now = utcnow()
        due = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        await asyncio.sleep(max(60.0, (due - now).total_seconds()))
        try:
            await refresh_market_data(state, notify, trigger="daily")
        except Exception:
            log.exception("daily market-data refresh failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    env = get_env()
    # The log writer starts before anything else and stops last.
    logstore.install(env.ta_data_dir)
    # Every configured secret is registered for scrubbing before any line
    # can mention one; the frontend only ever sees "set" and the last 4.
    for source in SOURCES:
        logstore.register_secret(get_secret(source.id))
    logstore.register_secret(get_secret("smtp"))
    lock = EngineLock(env.ta_db_path)
    lock.acquire()  # raises EngineAlreadyRunningError if another instance holds it
    db_engine: DbEngine | None = None
    state: AppState | None = None
    try:
        _report_auxiliary_database_problems(env)
        _verify_trading_database(env.ta_db_path)
        if lock.preexisted:
            _unclean_start(env)
        try:
            run_migrations(env.ta_db_path)
        except Exception as exc:
            log.critical(
                "database migrations failed at startup: %s",
                exc,
                exc_info=True,
                extra={"event": "db.migration_failed"},
            )
            raise StartupRecoveryError(f"database migrations failed: {exc}") from exc
        db_engine = make_engine(env.ta_db_path)
        store = logstore.get_store()
        if store is not None:
            session_factory0 = make_session_factory(db_engine)
            with session_factory0() as session:
                legacy = list(session.scalars(select(ErrorRow)))
            imported = logstore.import_legacy_errors(store, legacy)
            if imported:
                log.info(
                    "imported %d legacy error row(s) into logs.db",
                    imported,
                    extra={"event": "legacy.error"},
                )
        session_factory = make_session_factory(db_engine)
        session = session_factory()
        try:
            settings = repo.load_settings(session)
            settings_service.ensure_base_version(session, settings)
            if env.ta_data_source is not None and settings.data_source != env.ta_data_source:
                # TA_DATA_SOURCE (tests, e2e, dev): stored like a manual change.
                settings.data_source = env.ta_data_source
                settings_service.save(
                    session,
                    settings,
                    reason=f"TA_DATA_SOURCE={env.ta_data_source} at start",
                    source="action",
                    confirm_looser=True,
                )
            settings_version = repo.settings_version(session)
        finally:
            session.close()
        logstore.configure(
            console_level=settings.logging.console_level,
            queue_size=settings.logging.queue_size,
            library_debug=settings.logging.library_debug,
            backtest_debug=settings.logging.backtest_debug,
        )
        registry = SourceRegistry(settings.disabled_sources)
        market_client = build_market_client(settings, registry)
        state = AppState(
            local_token=generate_local_token(),
            engine=db_engine,
            session_factory=session_factory,
            settings=settings,
            settings_version=settings_version,
            market_client=market_client,
            sources=registry,
            status="paused",
            syncs=SyncRuns(session_factory),
        )
        app.state.app_state = state
        state.log_store = logstore.get_store()
        if state.log_store is not None:
            _wire_log_push(state, state.log_store)
        # Paper account + engine: positions are restored from the database,
        # pending decisions from before the restart are marked expired, and
        # an unacknowledged kill restores the Killed state. Always paused.
        session = session_factory()
        try:
            account = repo.current_account(session, settings.paper.start_balance_eur)
            broker = PaperBroker(account, settings.paper)
            broker.load_positions(session)
        finally:
            session.close()
        state.broker = broker
        state.account_id = account.id
        # One-time rebuild of paper fills from trades and positions; the
        # meta marker makes it run once, on the first start after 0014.
        session = session_factory()
        try:
            fills_mod.rebuild_fills(session, settings.paper)
        finally:
            session.close()
        notification_dispatcher = notifications.NotificationDispatcher(state)
        source_recovery_generations: dict[str, int] = {}

        def source_failed(source_id: str, error: str) -> None:
            source_recovery_generations[source_id] = (
                source_recovery_generations.get(source_id, 0) + 1
            )
            log.error(
                "data source failing: %s",
                source_id,
                extra={"event": "source.failing", "data": {"source": source_id}},
            )
            notification_dispatcher.schedule(
                "source_failing",
                f"Data source failing · {source_id}",
                f"3 failures in a row; latest: {error}",
            )

        def source_recovered(source_id: str) -> None:
            generation = source_recovery_generations.get(source_id, 0) + 1
            source_recovery_generations[source_id] = generation
            tasks_mod.spawn(
                _resolve_source_error_after_recovery,
                state,
                source_id,
                generation,
                source_recovery_generations,
                name=f"source-recovery-{source_id}",
                restart=False,
                track=lambda t: _track_task(state, t),
            )

        state.sources.on_failure_threshold = source_failed
        state.sources.on_recovered = source_recovered
        agent_engine = Engine(
            session_factory=session_factory,
            market_client=market_client,
            market_rules=state.market_rules,
            settings=lambda: state.settings,
            settings_version=lambda: state.settings_version,
            broker=broker,
            decider=RulesDecider(),
            broadcast=lambda: routes.broadcast_snapshot(state),
            notify=notification_dispatcher.notify,
            syncs=state.syncs,
        )
        # The OpenAI decider reads the live broker and risk state through
        # the engine, so it always sees the current account even after a
        # paper reset swaps broker instances.
        agent_engine.openai_decider = OpenAIDecider(
            session_factory=session_factory,
            market_client=market_client,
            broker=lambda: agent_engine.broker,
            risk_view=agent_engine.risk_view,
            clock=agent_engine.clock,
            notify=notification_dispatcher.notify,
            market_rules=state.market_rules,
        )
        # Shadows run on the same cycle snapshot and watcher tick, on their
        # own paper books (PR 8).
        agent_engine.shadows = ShadowRunner(
            session_factory=session_factory,
            market_client=market_client,
            market_rules=state.market_rules,
            live_settings=lambda: state.settings,
            clock=agent_engine.clock,
            price_fn=agent_engine._price,
            notify=notification_dispatcher.notify,
            shock_brake_until=lambda: agent_engine.shock_brake_until,
        )
        state.agent_engine = agent_engine
        await agent_engine.start()
        state.status = agent_engine.status
        tasks_mod.spawn(
            refresh_market_data,
            state,
            notification_dispatcher.notify,
            name="job-market-refresh",
            restart=False,
            track=lambda t: _track_task(state, t),
        )
        for coro, job in (
            (daily_market_loop(state, notification_dispatcher.notify), "job-market-daily"),
            (
                context_loop(
                    ContextCollector(session_factory, registry, settings=lambda: state.settings)
                ),
                "job-collector",
            ),
            (outcome_loop(session_factory, asyncio.Event()), "job-outcomes"),
            (backup_loop(env.ta_db_path, env.ta_data_dir), "job-backup"),
            (
                briefings_mod.briefing_loop(
                    state, session_factory, notify=notification_dispatcher.notify
                ),
                "job-briefings",
            ),
        ):
            tasks_mod.spawn(
                _loop_of(coro),
                name=job,
                track=lambda t: _track_task(state, t),
            )
        tasks_mod.spawn(
            _heartbeat_loop(lock),
            name="job-lock-heartbeat",
            track=lambda t: _track_task(state, t),
        )
        tasks_mod.install_global_hooks()
        _install_signal_handlers(app)
        yield
    finally:
        shutdown_started = time.monotonic()
        if state is not None:
            state.shutting_down = True
        app.state.shutdown_requested = True
        app.state.shutting_down = True
        log.info("Shutdown requested", extra={"event": "app.shutdown"})
        _announce_shutdown(app)  # a no-op when a signal already announced
        if state is not None:
            agent = state.agent_engine
            if agent is not None:
                # No new cycles or watcher passes; in-flight work runs on.
                agent.begin_shutdown()
            # Running work gets its grace period; a fill and its position
            # update are one transaction and are never cut off. A second
            # Ctrl+C skips the wait, never the final steps.
            grace = state.settings.storage.shutdown_grace_seconds

            def forced() -> bool:
                return bool(getattr(app.state, "sigint_skip_wait", False))

            while (
                agent is not None
                and agent.cycle_in_progress
                and not forced()
                and time.monotonic() - shutdown_started < grace
            ):
                await asyncio.sleep(0.1)
            if agent is not None:
                await agent.stop()
            tasks = list(state.tasks)
            state.tasks.clear()
            await _drain_tasks(tasks, forced)
            for ws_client in list(state.ws_clients):
                state.ws_clients.discard(ws_client)
                try:
                    await ws_client.close(code=1012)
                except Exception as exc:  # noqa: BLE001 - a dead socket shows as any error
                    log.debug(
                        "closing a WebSocket client at shutdown failed: %s",
                        exc,
                        extra={"event": "ws.client_lost"},
                    )
            await state.market_client.aclose()
        if db_engine is not None:
            interrupted = _interrupt_running_runs(db_engine, reason="shutdown")
            total_interrupted = sum(interrupted.values())
            if total_interrupted:
                log.info(
                    "%d interrupted run(s) marked at shutdown",
                    total_interrupted,
                    extra={"event": "app.interrupted_run", "data": interrupted},
                )
            await _final_checkpoint(db_engine)
            db_engine.dispose()
        lock.release()
        log.info(
            "Shutdown complete in %.1f s",
            time.monotonic() - shutdown_started,
            extra={"event": "app.stop"},
        )
        # The log writer is flushed and closed last.
        logstore.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Agent", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(
        StarletteHTTPException,
        errors.http_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        RequestValidationError,
        errors.validation_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, errors.unhandled_handler)
    app.add_middleware(LocalOnlyMiddleware)
    app.add_middleware(errors.ShutdownGuard)
    app.add_middleware(deps.RequestIdMiddleware)
    app.include_router(routes.router)
    app.include_router(positions.router)
    app.include_router(screens.router)
    app.include_router(dashboard_routes.router)
    app.include_router(source_routes.router)
    app.include_router(context_routes.router)
    app.include_router(inspector.router)
    app.include_router(understanding_routes.router)
    app.include_router(universe_routes.router)
    app.include_router(evidence.router)
    app.include_router(notifications.router)
    app.include_router(logs_routes.router)
    app.include_router(ws.router)
    if FRONTEND_DIST.is_dir():
        app.mount("/", SpaStaticFiles(directory=FRONTEND_DIST, html=True), name="ui")
    return app


class SpaStaticFiles(StaticFiles):
    """Serve the built Vite app; unknown paths fall back to index.html."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Unknown API paths must answer 404 JSON, not the SPA's index.html.
        if path.startswith("api/"):
            return errors.api_error(404, "That page or record does not exist.")
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


app = create_app()
