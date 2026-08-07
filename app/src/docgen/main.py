from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .db import Base, build_session_factory
from .projects.models import Project  # noqa: F401
from .projects.routes import router as projects_router
from .sources.models import Source  # noqa: F401


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        app_settings.data_dir.mkdir(parents=True, exist_ok=True)
        session_factory = build_session_factory(app_settings.database_url)
        Base.metadata.create_all(session_factory.kw["bind"])
        application.state.session_factory = session_factory
        yield

    app = FastAPI(title="DocGen", lifespan=lifespan)
    app.state.settings = app_settings
    app.include_router(projects_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
