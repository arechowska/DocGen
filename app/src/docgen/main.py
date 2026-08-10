from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from .config import Settings
from .db import build_session_factory, initialize_database
from .generation.routes import router as generation_router
from .jobs.models import Job  # noqa: F401
from .projects.models import Project  # noqa: F401
from .projects.routes import router as projects_router
from .sources.models import Source  # noqa: F401
from .sources.routes import router as sources_router
from .web import static_directory

_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        app_settings.data_dir.mkdir(parents=True, exist_ok=True)
        session_factory = build_session_factory(app_settings.database_url)
        engine = session_factory.kw["bind"]
        try:
            initialize_database(engine)
            application.state.session_factory = session_factory
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="DocGen", lifespan=lifespan)
    app.state.settings = app_settings
    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.include_router(sources_router)
    app.include_router(generation_router)
    app.include_router(projects_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
