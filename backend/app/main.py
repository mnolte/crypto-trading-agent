"""FastAPI app: lifespan, security middleware, routers and the built UI.

The lifespan acquires the single-engine lock, runs migrations, loads settings
and puts the app into the Paused state. The scheduler and exit watcher join in
later PRs; exactly one engine instance may run per database.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine as DbEngine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app import repo
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
from app.api import evidence, inspector, notifications, routes, screens, ws
from app.api import sources as source_routes
from app.api import understanding as understanding_routes
from app.api.deps import (
    AppState,
    LocalOnlyMiddleware,
    build_market_client,
    generate_local_token,
)
from app.config import REPO_ROOT, get_env
from app.db import EngineLock, MarketRow, make_engine, make_session_factory, run_migrations, utcnow
from app.features import briefings as briefings_mod
from app.features.shadows import ShadowRunner
from app.operations import backup_loop, configure_logging
from app.sources import SourceRegistry

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


def _track_task(state: AppState, task: asyncio.Task[None]) -> None:
    state.tasks.append(task)
    task.add_done_callback(
        lambda completed: state.tasks.remove(completed) if completed in state.tasks else None
    )


async def refresh_market_data(state: AppState) -> None:
    """Startup market-data check: exchange rules, the eligibility check and
    backfills for active markets still warming up.

    Any failure marks the source in the registry; it never stops startup —
    a failing source must not fail cycles, and the app always starts paused.
    """
    session = state.session_factory()
    try:
        infos = await state.market_client.markets()
        state.market_rules = {i.market: i for i in infos}
        await run_eligibility_check(state.market_client, session, state.settings.market)
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
    except Exception as exc:
        state.sources.record("bitvavo", exc)
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    env = get_env()
    configure_logging(env.ta_data_dir)
    lock = EngineLock(env.ta_db_path)
    lock.acquire()  # raises EngineAlreadyRunningError if another instance holds it
    db_engine: DbEngine | None = None
    state: AppState | None = None
    try:
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
            _track_task(
                state,
                asyncio.create_task(
                    _resolve_source_error_after_recovery(
                        state,
                        source_id,
                        generation,
                        source_recovery_generations,
                    )
                ),
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
        )
        state.agent_engine = agent_engine
        await agent_engine.start()
        state.status = agent_engine.status
        state.tasks.append(asyncio.create_task(refresh_market_data(state)))
        state.tasks.append(asyncio.create_task(outcome_loop(session_factory, asyncio.Event())))
        state.tasks.append(asyncio.create_task(backup_loop(env.ta_db_path, env.ta_data_dir)))
        state.tasks.append(
            asyncio.create_task(
                briefings_mod.briefing_loop(
                    state, session_factory, notify=notification_dispatcher.notify
                )
            )
        )
        yield
    finally:
        if state is not None:
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


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(LocalOnlyMiddleware)
    app.include_router(routes.router)
    app.include_router(screens.router)
    app.include_router(source_routes.router)
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
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


app = create_app()
