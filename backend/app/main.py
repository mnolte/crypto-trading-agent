"""FastAPI app: lifespan, security middleware, routers and the built UI.

The lifespan acquires the single-engine lock, runs migrations, loads settings
and puts the app into the Paused state. The scheduler and exit watcher join in
later PRs; exactly one engine instance may run per database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app import repo
from app.api import routes, ws
from app.api.deps import AppState, LocalOnlyMiddleware, generate_local_token
from app.config import REPO_ROOT, get_env
from app.db import EngineLock, make_engine, make_session_factory, run_migrations

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    env = get_env()
    lock = EngineLock(env.ta_db_path)
    lock.acquire()  # raises EngineAlreadyRunningError if another instance holds it
    engine: Engine | None = None
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
        app.state.app_state = AppState(
            local_token=generate_local_token(),
            engine=engine,
            session_factory=session_factory,
            settings=settings,
            settings_version=settings_version,
            status="paused",
        )
        yield
    finally:
        if engine is not None:
            engine.dispose()
        lock.release()


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Agent", version="0.1.0", lifespan=lifespan)
    app.add_middleware(LocalOnlyMiddleware)
    app.include_router(routes.router)
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
