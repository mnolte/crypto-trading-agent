"""FastAPI app: lifespan, security middleware, routers and the built UI.

The lifespan acquires the single-engine lock, runs migrations, loads settings
and puts the app into the Paused state. The scheduler and exit watcher join in
later PRs; exactly one engine instance may run per database.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.engine import Engine as DbEngine
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app import logstore, repo
from app import tasks as tasks_mod
from app.agent.broker.paper import PaperBroker
from app.agent.deciders.openai_decider import OpenAIDecider
from app.agent.deciders.rules import RulesDecider
from app.agent.engine import Engine
from app.agent.market.universe import (
    backfill_market,
    run_eligibility_check,
    warming_up,
)
from app.agent.outcomes import outcome_loop
from app.api import context as context_routes
from app.api import dashboard as dashboard_routes
from app.api import deps, errors, evidence, inspector, notifications, routes, screens, ws
from app.api import sources as source_routes
from app.api import understanding as understanding_routes
from app.api.deps import (
    AppState,
    LocalOnlyMiddleware,
    build_market_client,
    generate_local_token,
)
from app.config import REPO_ROOT, EnvConfig, get_env, get_secret
from app.context.collector import ContextCollector
from app.context.scheduler import context_loop
from app.db import EngineLock, MarketRow, make_engine, make_session_factory, run_migrations, utcnow
from app.features import briefings as briefings_mod
from app.features.shadows import ShadowRunner
from app.operations import backup_loop
from app.sources import SOURCES, SourceRegistry

log = logging.getLogger(__name__)

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

SOURCE_RECOVERY_RESOLVE_S = 3600


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
    WARNING, a quick_check on the trading database, running cycles marked
    interrupted, and the agent restarts paused (AppState defaults)."""
    log.warning(
        "previous run did not shut down cleanly",
        extra={"event": "app.unclean_shutdown"},
    )
    try:
        conn = sqlite3.connect(str(env.ta_db_path), timeout=5.0)
        verdict = conn.execute("PRAGMA quick_check").fetchone()
        conn.close()
        if verdict and verdict[0] != "ok":
            log.critical(
                "trading database failed PRAGMA quick_check: %s",
                verdict[0],
                extra={"event": "db.unwritable"},
            )
    except (sqlite3.Error, OSError) as exc:
        log.critical(
            "trading database could not be opened for a clean-start check: %s",
            exc,
            extra={"event": "db.unwritable"},
        )
    try:
        eng = make_engine(env.ta_db_path)
        with eng.begin() as conn:
            marked = conn.execute(
                text(
                    "UPDATE whatif_scenarios SET status = 'interrupted' "
                    "WHERE status = 'running'"
                )
            ).rowcount
        eng.dispose()
        if marked:
            log.warning(
                "%d interrupted run(s) marked after an unclean shutdown",
                marked,
                extra={"event": "app.unclean_shutdown"},
            )
    except SQLAlchemyError:
        pass  # a missing/legacy whatif table is not worth blocking startup


def _install_sigint(app: FastAPI) -> None:
    """The first Ctrl+C is a normal request; a second one means the user
    does not want to wait — log it and let uvicorn force-exit."""
    try:
        previous = signal.getsignal(signal.SIGINT)
    except (ValueError, OSError):
        return  # not the main thread (tests)

    count = 0

    def _handler(signum: int, frame: object) -> None:
        nonlocal count
        count += 1
        if count > 1:
            logging.getLogger("app.main").warning(
                "second interrupt: skipping the graceful wait",
                extra={"event": "app.shutdown_forced"},
            )
        app.state.sigint_requested = True
        if callable(previous):
            previous(signum, frame)

    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        pass


def _track_task(state: AppState, task: asyncio.Task[None]) -> None:
    state.tasks.append(task)
    task.add_done_callback(
        lambda completed: state.tasks.remove(completed) if completed in state.tasks else None
    )


async def refresh_market_data(
    state: AppState,
    notify: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> None:
    """Market-data check: exchange rules, the eligibility check and
    backfills for active markets still warming up. Runs at startup and
    daily at 01:00 UTC (``daily_market_loop``).

    Any failure marks the source in the registry; it never stops startup —
    a failing source must not fail cycles, and the app always starts paused.
    """
    session = state.session_factory()
    try:
        infos = await state.market_client.markets()
        # The engine and shadow runner hold this dict by reference —
        # update it in place so they see the fetched rules too.
        state.market_rules.clear()
        state.market_rules.update({i.market: i for i in infos})
        _rows, transitions = await run_eligibility_check(
            state.market_client, session, state.settings.market
        )
        for market, change in transitions.items():
            text = f"market {market} eligibility check: {change}"
            repo.add_activity(session, "alert", text, at=utcnow(), pair=market)
            if notify is not None:
                await notify("risk_control", f"Market {change}: {market}", text)
        session.commit()
        for market in state.settings.active_markets:
            row = session.get(MarketRow, market)
            if row is None:
                row = MarketRow(market=market, eligible=False)
            if not row.active:
                row.active = True
                row.activated_at = utcnow()
                repo.upsert_market_row(session, row)
            if warming_up(session, market, state.settings.market):
                await backfill_market(state.market_client, session, market, state.settings.market)
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
            await refresh_market_data(state, notify)
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
        if lock.preexisted:
            _unclean_start(env)
        run_migrations(env.ta_db_path)
        db_engine = make_engine(env.ta_db_path)
        session_factory = make_session_factory(db_engine)
        session = session_factory()
        try:
            settings = repo.load_settings(session)
            if env.ta_data_source is not None and settings.data_source != env.ta_data_source:
                # TA_DATA_SOURCE (tests, e2e, dev): stored like a manual change.
                settings.data_source = env.ta_data_source
                repo.save_settings(session, settings)
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
        )
        app.state.app_state = state
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
        notification_dispatcher = notifications.NotificationDispatcher(state)
        source_recovery_generations: dict[str, int] = {}

        def source_failed(source_id: str, error: str) -> None:
            source_recovery_generations[source_id] = (
                source_recovery_generations.get(source_id, 0) + 1
            )
            session = state.session_factory()
            try:
                repo.add_activity(
                    session,
                    "error",
                    f"data source failing: {source_id}",
                )
                session.commit()
            finally:
                session.close()
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
            (context_loop(ContextCollector(session_factory, registry)), "job-collector"),
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
        _install_sigint(app)
        yield
    finally:
        shutdown_started = time.monotonic()
        if state is not None:
            state.shutting_down = True
        app.state.shutting_down = True
        log.info("Shutdown requested", extra={"event": "app.shutdown"})
        if state is not None:
            # Running work gets its grace period; a fill and its position
            # update are one transaction and are never cut off.
            grace = state.settings.storage.shutdown_grace_seconds
            while (
                state.agent_engine is not None
                and state.agent_engine.cycle_in_progress
                and time.monotonic() - shutdown_started < grace
            ):
                await asyncio.sleep(0.1)
            if state.agent_engine is not None:
                await state.agent_engine.stop()
            tasks = list(state.tasks)
            state.tasks.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await state.market_client.aclose()
        if db_engine is not None:
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
    app.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)
    app.add_exception_handler(RequestValidationError, errors.validation_handler)
    app.add_exception_handler(Exception, errors.unhandled_handler)
    app.add_middleware(LocalOnlyMiddleware)
    app.add_middleware(errors.ShutdownGuard)
    app.add_middleware(deps.RequestIdMiddleware)
    app.include_router(routes.router)
    app.include_router(screens.router)
    app.include_router(dashboard_routes.router)
    app.include_router(source_routes.router)
    app.include_router(context_routes.router)
    app.include_router(inspector.router)
    app.include_router(understanding_routes.router)
    app.include_router(evidence.router)
    app.include_router(notifications.router)
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
