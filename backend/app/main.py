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
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app import repo
from app.agent.market.universe import (
    backfill_market,
    run_eligibility_check,
    warming_up,
)
from app.api import routes, ws
from app.api import sources as source_routes
from app.api.deps import (
    AppState,
    LocalOnlyMiddleware,
    build_market_client,
    generate_local_token,
)
from app.config import REPO_ROOT, get_env
from app.db import EngineLock, MarketRow, make_engine, make_session_factory, run_migrations, utcnow
from app.sources import SourceRegistry

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


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
    lock = EngineLock(env.ta_db_path)
    lock.acquire()  # raises EngineAlreadyRunningError if another instance holds it
    engine: Engine | None = None
    state: AppState | None = None
    try:
        run_migrations(env.ta_db_path)
        engine = make_engine(env.ta_db_path)
        session_factory = make_session_factory(engine)
        session = session_factory()
        try:
            settings = repo.load_settings(session)
            settings_version = repo.settings_version(session)
        finally:
            session.close()
        registry = SourceRegistry(settings.disabled_sources)
        market_client = build_market_client(settings, registry)
        state = AppState(
            local_token=generate_local_token(),
            engine=engine,
            session_factory=session_factory,
            settings=settings,
            settings_version=settings_version,
            market_client=market_client,
            sources=registry,
            status="paused",
        )
        app.state.app_state = state
        state.tasks.append(asyncio.create_task(refresh_market_data(state)))
        yield
    finally:
        if state is not None:
            for task in state.tasks:
                task.cancel()
            if state.tasks:
                await asyncio.gather(*state.tasks, return_exceptions=True)
            await state.market_client.aclose()
        if engine is not None:
            engine.dispose()
        lock.release()


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(LocalOnlyMiddleware)
    app.include_router(routes.router)
    app.include_router(source_routes.router)
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
