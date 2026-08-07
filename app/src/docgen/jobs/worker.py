from __future__ import annotations

import os
import signal
from collections.abc import Mapping
from threading import Event
from types import FrameType
from typing import Protocol

from docgen.config import Settings
from docgen.db import Base, build_session_factory
from docgen.projects.models import Project  # noqa: F401

from .models import JobKind
from .repository import JobRepository
from .runner import JobRunner, Workflow

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
    runner.recover_interrupted()
    while not stop_event.is_set():
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
    Base.metadata.create_all(engine)

    worker_id = resolve_worker_id(os.environ)
    stop_event = Event()
    install_signal_handlers(stop_event)
    try:
        with session_factory() as session:
            workflows: dict[JobKind, Workflow] = {}
            runner = JobRunner(
                JobRepository(session, worker_id=worker_id),
                workflows,
            )
            run_worker(runner, stop_event)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()


__all__ = ["install_signal_handlers", "main", "resolve_worker_id", "run_worker"]
