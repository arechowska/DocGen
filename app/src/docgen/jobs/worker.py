from __future__ import annotations

import os
import signal
from collections.abc import Mapping
from threading import Event
from types import FrameType
from typing import Protocol
from uuid import uuid4

from docgen.config import Settings
from docgen.db import build_session_factory, initialize_database
from docgen.documents.repository import DocumentRepository
from docgen.extraction.confluence import ConfluenceClient
from docgen.extraction.registry import ExtractorRegistry
from docgen.projects.models import Project  # noqa: F401
from docgen.projects.repository import ProjectRepository
from docgen.sources.repository import SourceRepository
from docgen.sources.storage import LocalStorage
from docgen.templates_catalog.loader import TemplateCatalog
from docgen.workflows.normalize import NormalizationWorkflow

from .repository import JobRepository
from .runner import JobRunner, WorkflowDependencies, build_workflows

_POLL_INTERVAL_SECONDS = 0.5


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


def install_signal_handlers(stop_event: Event) -> None:
    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def run_worker(
    runner: JobRunner,
    stop_event: StopEvent,
    *,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> None:
    while not stop_event.is_set():
        runner.recover_interrupted()
        if not runner.run_once() and stop_event.wait(poll_interval):
            break


def resolve_worker_id(environ: Mapping[str, str]) -> str:
    worker_id = environ.get("DOCGEN_WORKER_ID", "").strip()
    if not worker_id:
        raise RuntimeError("Для worker необходимо задать DOCGEN_WORKER_ID")
    return worker_id


def main() -> None:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    session_factory = build_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    initialize_database(engine)

    worker_id = resolve_worker_id(os.environ)
    worker_instance_token = str(uuid4())
    stop_event = Event()
    install_signal_handlers(stop_event)
    try:
        with session_factory() as session:
            storage = LocalStorage(settings.data_dir)
            normalization = NormalizationWorkflow(
                SourceRepository(session),
                storage,
                ExtractorRegistry.default(settings),
                ConfluenceClient.from_settings(settings),
            )
            workflows = build_workflows(
                settings,
                WorkflowDependencies(
                    projects=ProjectRepository(session),
                    normalization=normalization,
                    templates=TemplateCatalog(),
                    documents=DocumentRepository(session),
                ),
            )
            runner = JobRunner(
                JobRepository(
                    session,
                    worker_id=worker_id,
                    instance_token=worker_instance_token,
                    lease_seconds=settings.worker_lease_seconds,
                ),
                workflows,
                max_job_seconds=settings.max_job_seconds,
            )
            run_worker(runner, stop_event)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()


__all__ = ["install_signal_handlers", "main", "resolve_worker_id", "run_worker"]
